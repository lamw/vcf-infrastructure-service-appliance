from functools import reduce
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timedelta
from errno import ENOENT
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import Any, Literal, Self

import validators
from dataclasses_json import DataClassJsonMixin, config, dataclass_json

from .helpers import empty, env_prefix, xor



_path_config_metadata = config(decoder=lambda s: Path(s))
_int_config_metadata = config(decoder=lambda s: int(s, base=10))
_ns_timedelta_config_metadata = config(
    decoder=lambda s: timedelta(microseconds=int(s) // 1000), encoder=lambda td: (td.total_seconds() * 1e9) + (td.microseconds * 1000)
)

_valid_true_strings = ["1", "y", "yes", "t", "true"]
_bool_config_metadata = config(decoder=lambda s: s.lower() in _valid_true_strings)

_datetime_metadata = config(
    decoder=lambda s: datetime.fromisoformat(s) if s else None, encoder=lambda d: datetime.isoformat(d) if d else None
)
_ip_config_metadata = config(decoder=lambda s: ip_address(s))


@dataclass
class ContentLibraryConfig:
    root: Path = field(metadata=_path_config_metadata, default=Path("/opt/vis/data/content-library"))
    source_url: str = "https://wp-content.broadcom.com/v2/latest/lib.json"
    source_user: str | None = None
    source_password: str | None = None
    host: IPv4Address | IPv6Address = field(metadata=_ip_config_metadata, default=ip_address(0))
    port: int = field(metadata=_int_config_metadata, default=9091)
    protocol: str = "http"
    auth_user: str | None = None
    auth_password: str | None = None
    tls_cert: Path | None = field(metadata=_path_config_metadata, default=None)
    tls_key: Path | None = field(metadata=_path_config_metadata, default=None)
    auto_source_sync_enabled: bool = field(metadata=_bool_config_metadata, default=True)
    worker_pool_size: int = field(metadata=_int_config_metadata, default=25)
    sync_schedule: str = "Sun 8:06"

    def __post_init__(self):
        # validate inputs
        if not (1 < self.port < 65536):
            raise ValueError(f"Port {self.port} must be an unsigned 16-bit integer greater than 0")

        # source_url has to be a valid URL
        result = validators.url(self.source_url)
        if type(result) is validators.ValidationError:
            raise ValueError(result)

        # user/password combos must be totally set or totally unset
        if xor(empty(self.source_user), empty(self.source_password)):
            raise ValueError("either source_user and source_password must both be set or neither must be set")

        if xor(empty(self.auth_user), empty(self.auth_password)):
            raise ValueError("either auth_user and auth_password must both be set or neither must be set")

        # cert and key files must be both be set or both be unset, and if set, they must be readable files
        if xor(empty(self.tls_cert), empty(self.tls_key)):
            raise ValueError("either tls_cert and tls_key must both be set or neither must be set")

        self.protocol = self.protocol.lower()
        if self.protocol not in ["http", "https"]:
            raise ValueError("protocol must be http or https")

        try:
            subprocess.run(
                ["systemd-analyze", "calendar", self.sync_schedule],
                check=True,
                capture_output=True,
                timeout=2,
            )
        except (PermissionError, subprocess.TimeoutExpired):
            pass
        except subprocess.CalledProcessError as e:
            raise ValueError(f"sync schedule is invalid: {e}")

        if self.tls_key is not None:
            # if we're here we can check the cert too
            if not self.tls_key.is_file():
                raise FileNotFoundError(ENOENT, os.strerror(ENOENT), self.tls_key)
            if not self.tls_cert.is_file():  # ty: ignore[unresolved-attribute]
                raise FileNotFoundError(ENOENT, os.strerror(ENOENT), self.tls_cert)

        # Ensure root_dir is absolute
        if not self.root.is_absolute():
            raise ValueError("root must be an absolute path")

        self.lib_path = self.root / "lib"
        self.cache_path = self.root / "cache"
        

    def lib_size(self) -> int:
        def __size(p: Path) -> int:
            if p.is_dir():
                return 0
            try:
                return p.stat().st_size
            except:
                return 0

        sizes = [__size(p) for p in self.lib_path.rglob('*.*')]

        return reduce(lambda acc, s: acc + s, sizes, 0)

        
    def lib_counts(self) -> tuple[int, int]:
        paths = self.lib_path.rglob("*")
        return len([p for p in paths if not p.is_dir()]), len([p for p in paths if p.is_dir()])
        
    @classmethod
    def from_env(cls, environ: dict[str, str] = dict(os.environ)) -> Self:
        to_field_name = lambda k: k[len(env_prefix) :].lower()

        field_map = {f.name: f for f in fields(cls)}

        def _process(k: str, v: str) -> tuple[str, Any] | None:
            decode = lambda s: s
            field_name = to_field_name(k)
            field = field_map.get(field_name, None)

            if field:
                env_meta = field.metadata.get("env_config", {})
                decoder = env_meta.get("decoder", None)
                if callable(decoder):
                    decode = decoder

            return (field_name, decode(v))

        data = {}
        for k, v in environ.items():
            if k.startswith(env_prefix):
                name, decoded = _process(k, v)
                data[name] = decoded

        print(data)

        return cls(**data)


@dataclass
class ContentLibrarySyncTask:
    lib_path: Path
    action: Literal["add", "delete"]
    cache_path: Path | None = None
    remote_uri: str | None = None
    etag: str | None = None
    size: int | None = None
    dry_run: bool = False

    def __repr__(self) -> str:
        data = asdict(self)
        return json.dumps(data)

    def is_cached(self) -> bool:
        if self.cache_path is None or self.etag is None:
            return False

        try:
            cached_etag = self.cache_path.read_text().strip()
        except BaseException:
            return False
        else:
            return cached_etag == self.etag

    def can_cache(self) -> bool:
        if self.cache_path is None or self.etag is None:
            return False

    def cache(self) -> None:
        if self.can_cache():
            os.makedirs(self.cache_path.parent, exist_ok=True)  # ty: ignore
            self.cache_path.write_text(self.etag)  # ty: ignore
            return

        raise ValueError("cannot cache this task file")


@dataclass
class ContentLibrarySyncTaskResult(ContentLibrarySyncTask):
    result: Literal["success", "failure"] | None = None
    reason: BaseException | None = None
    cache_hit: bool | None = None

    @classmethod
    def from_task(cls, task: ContentLibrarySyncTask) -> Self:
        return ContentLibrarySyncTaskResult(
            remote_uri=task.remote_uri,
            lib_path=task.lib_path,
            etag=task.etag,
            size=task.size,
            action=task.action,
        )


@dataclass_json
@dataclass
class ContentLibraryFile:
    name: str | None = None
    size: int | None = None
    etag: str | None = None
    hrefs: list[str] = field(default_factory=list)

    def __post_init__(self):
        if len(self.hrefs) != 1 or self.hrefs[0].strip() == "":
            raise ValueError("Expected hrefs to contain exactly one non-empty string")


@dataclass_json
@dataclass
class ContentLibraryItem:
    selfHref: str
    files: list[ContentLibraryFile]


@dataclass_json
@dataclass
class ContentLibraryItemsList:
    items: list[ContentLibraryItem]


@dataclass_json
@dataclass
class ContentLibrarySpec:
    itemsHref: str


@dataclass_json
@dataclass
class ContentLibrarySyncStats(DataClassJsonMixin):
    sync_in_progress: bool = False
    last_sync_time: datetime | None = field(metadata=_datetime_metadata, default=None)
    next_sync_time: datetime | None = field(metadata=_datetime_metadata, default=None)
    last_sync_result: Literal["SUCCESS", "FAILURE"] | None = None
    total_sync_count: int = 0
    last_sync_duration: timedelta = field(metadata=_datetime_metadata, default=timedelta())
    mean_sync_duration: timedelta = field(metadata=_datetime_metadata, default=timedelta())
    files_already_cached: int = 0
    files_downloaded: int = 0
    files_deleted: int = 0
    download_size_bytes: int = 0
    lib_size_bytes: int = 0
    lib_file_count: int = 0
    lib_dir_count: int = 0

    def marshal(self, omit_empty: bool = True) -> str:
        return json.dumps(self.marshal_to_dict(omit_empty), indent=4)

    def marshal_to_dict(self, omit_empty: bool = True) -> dict[str, Any]:
        d = self.to_dict(encode_json=False)

        if omit_empty:
            for k in [f.name for f in fields(self)]:
                if d.get(k, None) is None:
                    d.pop(k, None)
        return d