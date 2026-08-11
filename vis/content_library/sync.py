import json
import logging
import os
import subprocess
from asyncio import Queue, create_task, gather, Lock, run
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import reduce
from os import makedirs
from time import monotonic_ns
from urllib.parse import urljoin

from dataclasses_json import DataClassJsonMixin
from requests import Request, Session

from .dataclasses import (
    ContentLibraryConfig,
    ContentLibraryItemsList,
    ContentLibrarySpec,
    ContentLibrarySyncStats,
    ContentLibrarySyncTask,
    ContentLibrarySyncTaskResult,
)

_CHUNK_SIZE = 10 * (1 << 20)  # 10 MiB

_SYNC_STATS_FILE = ".sync-stats.json"

_session: Session | None = None

now = lambda: datetime.now(tz=timezone.utc)

log: logging.Logger = logging.root
write_lock = Lock()

def __get_global_session(config: ContentLibraryConfig) -> Session:
    global _session

    if not _session:
        _session = Session()
        _session.verify = False
        if config.source_password and config.source_user:
            _session.auth = (config.source_url, config.source_password)

    return _session


def __fetch_remote_item(config: ContentLibraryConfig, task: ContentLibrarySyncTask) -> None:
    log.debug("fetching remote item for task", extra={"task": task})
    request = Request(method="GET", url=urljoin(config.source_url, task.remote_uri))
    with __get_global_session(config) as s:
        p = s.prepare_request(request)

        makedirs(task.lib_path.parent, exist_ok=True)

        with s.send(p, stream=True) as file_resp:
            file_resp.raise_for_status()

            with task.lib_path.open("wb") as local_file:
                local_file.writelines(file_resp.iter_content(chunk_size=_CHUNK_SIZE))

            if task.can_cache():
                task.cache()


def __fetch_remote_json[T: DataClassJsonMixin](
    config: ContentLibraryConfig, task: ContentLibrarySyncTask, fetch_type: type[T]
) -> T:
    log.debug("fetching remote json for task", extra={"task": task})
    request = Request(method="GET", url=urljoin(config.source_url, task.remote_uri))
    with __get_global_session(config) as s:
        p = s.prepare_request(request)

        with s.send(p, stream=True) as resp:
            resp.raise_for_status()
            return fetch_type.from_json(resp.content)


def __collect_tasks(config: ContentLibraryConfig, dry_run: bool = False) -> list[ContentLibrarySyncTask]:
    log.debug("collecting all tasks to be processed")
    delete_tasks = [
        ContentLibrarySyncTask(action="delete", lib_path=f, dry_run=dry_run)
        for f in config.lib_path.rglob("*")
        if f.is_file()
    ]

    root_url_prefix = urljoin(config.source_url, ".")
    source_url_file = config.source_url[len(root_url_prefix) :]

    add_tasks = [ContentLibrarySyncTask(action="add", lib_path=config.lib_path / source_url_file, remote_uri="")]
    spec: ContentLibrarySpec = __fetch_remote_json(config, add_tasks[0], ContentLibrarySpec)

    add_tasks.append(
        ContentLibrarySyncTask(action="add", lib_path=config.lib_path / spec.itemsHref, remote_uri=spec.itemsHref)
    )
    itemsList: ContentLibraryItemsList = __fetch_remote_json(config, add_tasks[1], ContentLibraryItemsList)

    for item in itemsList.items:
        add_tasks.append(
            ContentLibrarySyncTask(action="add", lib_path=config.lib_path / item.selfHref, remote_uri=item.selfHref)
        )
        add_tasks.extend(
            [
                ContentLibrarySyncTask(
                    action="add",
                    lib_path=config.lib_path / f.hrefs[0],
                    cache_path=config.cache_path / f.hrefs[0],
                    remote_uri=f.hrefs[0],
                    size=f.size,
                    etag=f.etag,
                )
                for f in item.files
            ]
        )

    files_to_add = [f.lib_path for f in add_tasks]

    all_tasks = list(add_tasks)
    all_tasks.extend([d for d in delete_tasks if d.lib_path not in files_to_add])

    return all_tasks


def get_sync_stats(config: ContentLibraryConfig = ContentLibraryConfig.from_env()) -> ContentLibrarySyncStats | None:
    global write_lock

    try:
        run(write_lock.acquire())
        stats_file = config.cache_path / _SYNC_STATS_FILE
        return ContentLibrarySyncStats.from_json(stats_file.read_bytes()) if stats_file.is_file() else None
    finally:
        write_lock.release()

def __store_sync_stats(config: ContentLibraryConfig, stats: ContentLibrarySyncStats) -> None:
    global write_lock

    try:
        run(write_lock.acquire())
        stats_file = config.cache_path / _SYNC_STATS_FILE
        stats_file.write_text(stats.marshal())
    finally:
        write_lock.release()

async def __sync_worker(config: ContentLibraryConfig, work_queue: Queue, results_queue: Queue):
    log.debug("starting sync worker")
    while True:
        task: ContentLibrarySyncTask = await work_queue.get()
        relative_path = task.lib_path.relative_to(config.lib_path)

        log.debug("processing sync task", extra={"task": task})

        result = ContentLibrarySyncTaskResult.from_task(task)
        try:
            if task.dry_run:
                result.result = "success"
                log.debug("dry run is enabled so skip actual IO operation")
                results_queue.put_nowait(result)
                continue

            if task.action == "delete":
                log.debug(f"attempting to delete file {task.lib_path}")
                try:
                    os.remove(task.lib_path)
                    os.remove(config.cache_path / relative_path)
                except FileNotFoundError as e:
                    log.warning(f"error deleting {task.lib_path}", exc_info=e)

                result.result = "success"
                results_queue.put_nowait(result)
                continue

            if task.is_cached():
                log.debug(f"{task.lib_path} is already cached, do nothing")
                result.cache_hit = True
                result.result = "success"
                results_queue.put_nowait(result)
                continue

            result.cache_hit = False

            if task.remote_uri is None:
                log.error("remote_uri is missing", extra={"task": task})
                result.result = "failure"
                result.reason = ValueError("remote_url is missing")
                results_queue.put_nowait(result)
                continue

            __fetch_remote_item(config, task)
            result.result = "success"
            results_queue.put_nowait(result)
            continue
        except BaseException as e:
            log.error("process failed", exc_info=e, extra={"task": task})
            result.result = "failure"
            result.reason = e
            results_queue.put_nowait(result)
            continue
        finally:
            log.debug("finished processing task", extra={"task": task})
            work_queue.task_done()


async def run_sync(
    config: ContentLibraryConfig, logger: logging.Logger | None = None, dry_run: bool = False
) -> ContentLibrarySyncStats:
    global log
    if logger:
        log = logger

    stats = get_sync_stats(config) or ContentLibrarySyncStats()
    if stats.sync_in_progress:
        # this failure is ephemeral, intentionally do not save it
        stats.last_sync_result = "FAILURE"
        log.error("another sync is currently in progress")
        return stats

    log.debug(f"Beginning sync with upstream library at {config.source_url}")
    start_time = now()
    stats.sync_in_progress = True
    __store_sync_stats(config, stats)

    start_execution = monotonic_ns()
    try:
        all_tasks = __collect_tasks(config, dry_run)
        work_queue = Queue()
        results_queue = Queue()
        logging.debug(f"preparing to process {len(all_tasks)} synchronization tasks")
        logging.debug(f"worker pool size = {config.worker_pool_size}")

        for t in all_tasks:
            work_queue.put_nowait(t)

        worker_pool = []
        for i in range(max(config.worker_pool_size, 1)):
            worker_pool.append(
                create_task(__sync_worker(config, work_queue, results_queue), name=f"sync-pool-worker-{i}")
            )

        await work_queue.join()
        for worker in worker_pool:
            worker.cancel()

        await gather(*worker_pool, return_exceptions=False)
    except BaseException as e:
        log.error("an unexpected error was raised in the sync process", exc_info=e)
        stats.last_sync_result = "FAILURE"
    finally:
        end_execution = monotonic_ns()
        stats.last_sync_time = start_time
        stats.sync_in_progress = False
        stats.last_sync_duration = timedelta(microseconds=(end_execution - start_execution) // 1000)
        total_sync_duration = stats.mean_sync_duration * stats.total_sync_count
        stats.total_sync_count = stats.total_sync_count + 1
        stats.mean_sync_duration = (total_sync_duration + stats.last_sync_duration) / stats.total_sync_count

        results = []
        while not results_queue.empty():
            results.append(results_queue.get_nowait())

        stats.last_sync_result = "FAILURE" if any([r.reason is not None for r in results]) else "SUCCESS"

        if stats.last_sync_result == "SUCCESS":
            reducer: Callable[[int, ContentLibrarySyncTaskResult], int] = lambda acc, r: acc + (r.size or 0)

            stats.files_already_cached = len([r for r in results if r.action == "add" and r.cache_hit])
            stats.files_downloaded = len([r for r in results if r.action == "add"]) - stats.files_already_cached
            stats.files_deleted = len([r for r in results if r.action == "delete"])
            stats.download_size_bytes = reduce(
                reducer, [r for r in results if r.action == "add" and r.size is not None], 0
            )

        stats.next_sync_time = _get_next_sync_time()
        stats.lib_size_bytes = config.lib_size()
        stats.lib_file_count, stats.lib_dir_count = config.lib_counts()

        __store_sync_stats(config, stats)

        return stats


def is_sync_in_progress(config: ContentLibraryConfig = ContentLibraryConfig.from_env()) -> bool:
    stats = get_sync_stats(config)
    return stats.sync_in_progress if stats else False


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
