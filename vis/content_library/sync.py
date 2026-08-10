import json
import logging
import multiprocessing
import os
import subprocess
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from functools import reduce
from os import makedirs
from timeit import default_timer as timer
from urllib.parse import urljoin, urlparse

import requests
from dataclasses_json import DataClassJsonMixin
from requests.auth import HTTPBasicAuth

from .dataclasses import (
    ContentLibraryConfig,
    ContentLibraryFile,
    ContentLibraryItemsList,
    ContentLibrarySpec,
    ContentLibrarySyncStats,
    ContentLibrarySyncTask,
    ContentLibrarySyncTaskResult,
)

logger: logging.Logger = logging.Logger("", logging.CRITICAL)

_CHUNK_SIZE = 5 * (1 << 20)  # 5 MiB

_SYNC_STATS_FILE = ".sync-stats.json"


def load_items(config: ContentLibraryConfig, source_auth: HTTPBasicAuth | None) -> list[ContentLibraryFile]:
    logger.debug("loading list of content library files")

    all_files: list[ContentLibraryFile] = []
    with requests.get(config.source_url, auth=source_auth) as resp:
        resp.raise_for_status()
        spec: ContentLibrarySpec = ContentLibrarySpec.from_json(resp.content)  # ty: ignore[unresolved-attribute]
        # add the url itself so we download it
        parsed = urlparse(config.source_url)
        name = parsed.path.split("/")[-1]
        all_files.append(ContentLibraryFile(hrefs=[name]))
        all_files.append(ContentLibraryFile(hrefs=[spec.itemsHref]))

    with requests.get(urljoin(config.source_url, spec.itemsHref), auth=source_auth) as resp:
        resp.raise_for_status()
        itemlist: ContentLibraryItemsList = ContentLibraryItemsList.from_json(resp.content)  # ty: ignore[unresolved-attribute]
        # flatten the file lists into a single list
        for item in itemlist.items:
            all_files.append(ContentLibraryFile(name=item.selfHref, hrefs=[item.selfHref]))
            all_files.extend(item.files)

    return all_files


class SyncManager:
    def __init__(self, config: ContentLibraryConfig):
        self._config = config
        self._dry_run = False
        self._source_auth: HTTPBasicAuth | None = (
            HTTPBasicAuth(username=config.source_user, password=config.source_password)
            if config.source_user and config.source_password
            else None
        )

    def __run_task(self, task: ContentLibrarySyncTask) -> ContentLibrarySyncTaskResult:
        relative_path = task.lib_path.relative_to(self._config.lib_path)
        logging.debug(msg=f"Processing task [action: {task.action} file: {relative_path}]")

        result = ContentLibrarySyncTaskResult.from_task(task)
        try:
            if task.action == "delete":
                try:
                    os.remove(task.lib_path)
                    os.remove(self._config.cache_path / relative_path)
                except FileNotFoundError:
                    pass

                result.result = "success"
                return result

            if self.__file_is_cached(task):
                result.cache_hit = True
                result.result = "success"
                return result

            result.cache_hit = False

            if self._dry_run:
                result.result = "success"
                logging.debug("dry run is enabled so skip actual download")
                return result

            if task.remote_uri is None:
                result.result = "failure"
                result.reason = ValueError("remote_url is missing")
                return result

            cache_path = self._config.cache_path / relative_path
            makedirs(task.lib_path.parent, exist_ok=True)
            makedirs(cache_path.parent, exist_ok=True)

            file_resp: requests.Response = self.__fetch_http_resource(relative_uri=task.remote_uri, stream=True)
            with file_resp:
                file_resp.raise_for_status()

                with open(task.lib_path, "wb") as local_file:
                    local_file.writelines(file_resp.iter_content(chunk_size=_CHUNK_SIZE))

                if task.etag:
                    with open(cache_path, "wt") as cache_file:
                        cache_file.write(task.etag)

                result.result = "success"
                return result
        except BaseException as e:
            result.result = "failure"
            result.reason = e
            return result

    def __file_is_cached(self, task: ContentLibrarySyncTask) -> bool:
        if task.cache_path is None or task.etag is None:
            return False

        try:
            cached_etag = task.cache_path.read_text().strip()
        except BaseException:
            return False
        else:
            return cached_etag == task.etag

    def __load_sync_stats(self) -> ContentLibrarySyncStats:
        stats_file = self._config.cache_path / _SYNC_STATS_FILE
        return (
            ContentLibrarySyncStats.from_json(stats_file.read_bytes())
            if stats_file.is_file()
            else ContentLibrarySyncStats()
        )

    def __collect_delete_tasks(self) -> Iterable[ContentLibrarySyncTask]:
        """
        This collects all files under self._config.lib_dir and creates a delete task
        for it. It will be overwritten by an add task if it still exists in the remote
        content library
        """
        return [
            ContentLibrarySyncTask(action="delete", lib_path=f) for f in self._config.lib_path.rglob("*") if f.is_file()
        ]

    def __fetch_http_resource(
        self, relative_uri: str, fetch_json_type: type[DataClassJsonMixin] | None = None, **request_get_options
    ):
        url = urljoin(self._config.source_url, relative_uri)
        if fetch_json_type is None:
            return requests.get(url=url, auth=self._source_auth, **request_get_options)

        with requests.get(url=url, auth=self._source_auth, **request_get_options) as resp:
            return fetch_json_type.from_json(resp.content)

    def __collect_add_tasks(self) -> Iterable[ContentLibrarySyncTask]:
        """
        This creates a list of all files to add to the library by parsing the root items
        directory and adding every file it finds
        """
        root_url_prefix = urljoin(self._config.source_url, ".")
        source_url_file = self._config.source_url[len(root_url_prefix) :]

        tasks = [ContentLibrarySyncTask(action="add", lib_path=self._config.lib_path / source_url_file, remote_uri="")]

        spec: ContentLibrarySpec = self.__fetch_http_resource(relative_uri="", fetch_json_type=ContentLibrarySpec)
        tasks.append(
            ContentLibrarySyncTask(
                action="add", lib_path=self._config.lib_path / spec.itemsHref, remote_uri=spec.itemsHref
            )
        )
        itemList: ContentLibraryItemsList = self.__fetch_http_resource(
            relative_uri=spec.itemsHref, fetch_json_type=ContentLibraryItemsList
        )

        for item in itemList.items:
            tasks.append(
                ContentLibrarySyncTask(
                    action="add", lib_path=self._config.lib_path / item.selfHref, remote_uri=item.selfHref
                )
            )
            tasks.extend(
                [
                    ContentLibrarySyncTask(
                        action="add",
                        lib_path=self._config.lib_path / f.hrefs[0],
                        remote_uri=f.hrefs[0],
                        size=f.size,
                        etag=f.etag,
                    )
                    for f in item.files
                ]
            )

        return tasks

    def __collect_all_tasks(self) -> Iterable[ContentLibrarySyncTask]:
        add_tasks = self.__collect_add_tasks()
        delete_tasks = self.__collect_delete_tasks()

        files_to_add = [f.lib_path for f in add_tasks]

        all_tasks = list(add_tasks)
        all_tasks.extend([d for d in delete_tasks if d.lib_path not in files_to_add])

        return all_tasks

    def print_sync_stats(self) -> None:
        stats: ContentLibrarySyncStats = self.__load_sync_stats()
        print(stats.marshal(omit_empty=False))

    def run(self, dry_run: bool = False) -> ContentLibrarySyncStats:
        self._dry_run = dry_run
        stats = self.__load_sync_stats()
        stats.last_sync_time = datetime.now(tz=timezone.utc)

        start_execution = timer()
        try:
            all_tasks = self.__collect_all_tasks()
            logging.debug(f"preparing to process {len(all_tasks)} synchronization tasks")
            logging.debug(f"tasks will {'' if self._config.parallel_source_sync else 'not '}be processed in parallel")
            if self._config.parallel_source_sync:
                logging.debug(
                    f"maximum parallel work queues are set to 4 per cpu for a total of {4 * multiprocessing.cpu_count()}"
                )
                with multiprocessing.Pool(maxtasksperchild=4) as pool:
                    proc_mgr = pool.map_async(func=self.__run_task, iterable=all_tasks)
                    proc_mgr.wait()
                    results = proc_mgr.get()
                    pool.terminate()
            else:
                results = [result for result in map(self.__run_task, all_tasks)]
        except BaseException as e:
            logging.error(msg="An unexpected error was raised in the sync process", exc_info=e)
            stats.last_sync_result = "FAILURE"
        else:
            stats.last_sync_result = "FAILURE" if any([r.reason is not None for r in results]) else "SUCCESS"
        finally:
            end_execution = timer()
            stats.last_sync_duration = end_execution - start_execution
            total_sync_duration = stats.mean_sync_duration * stats.total_sync_count
            stats.total_sync_count = stats.total_sync_count + 1
            stats.mean_sync_duration = (total_sync_duration + stats.last_sync_duration) / stats.total_sync_count

            if stats.last_sync_result == "SUCCESS":
                reducer: Callable[[int, ContentLibrarySyncTaskResult], int] = lambda acc, r: acc + (r.size or 0)

                stats.files_already_cached = len([r for r in results if r.action == "add" and r.cache_hit])
                stats.files_downloaded = len([r for r in results if r.action == "add"]) - stats.files_already_cached
                stats.files_deleted = len([r for r in results if r.action == "delete"])
                stats.download_size_bytes = reduce(
                    reducer, [r for r in results if r.action == "add" and r.size is not None], 0
                )

            stats.next_sync_time = _get_next_sync_time()

            with open(self._config.cache_path / _SYNC_STATS_FILE, "wt") as fp:
                fp.write(stats.marshal())

            return stats


def _get_next_sync_time() -> datetime | None:
    try:
        result = subprocess.run(
            ["systemctl", "list-timers", "vis-content-library-sync.timer", "--output=pretty-json"],
            capture_output=True,
            check=True,
        )
    except (PermissionError, subprocess.CalledProcessError):
        return None

    timer_list = json.loads(result.stdout)
    return (
        None
        if len(timer_list) == 0 or not hasattr(timer_list[0], "next")
        else datetime.fromtimestamp(
            float(timer_list[0].get("next", 0) / 1e6), tz=timezone.utc
        )  # fromtimestamp expects a value in seconds but systemctl returns microseconds, so divide by 1,000,000
    )
