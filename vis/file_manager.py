import os
import pwd
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class RepositoryFileManager:
    def __init__(self, root: str, owner: Optional[str] = None, readonly: bool = False):
        self.root = Path(root).resolve()
        self.owner = owner
        self.readonly = readonly

    def list_dir(self, relative_path: str = "") -> Dict[str, object]:
        current = self._resolve(relative_path)
        if not current.exists():
            raise FileNotFoundError(str(current))
        if not current.is_dir():
            raise NotADirectoryError(str(current))

        entries: List[Dict[str, object]] = []
        for child in sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if self._is_hidden_repository_entry(child.name):
                continue
            stat = child.stat()
            created = getattr(stat, "st_birthtime", stat.st_ctime)
            entries.append(
                {
                    "name": child.name,
                    "path": self._relative(child),
                    "kind": "Directory" if child.is_dir() else "File",
                    "size": stat.st_size,
                    "created": int(created),
                    "created_display": self._format_timestamp(created),
                    "modified": int(stat.st_mtime),
                    "modified_display": self._format_timestamp(stat.st_mtime),
                    "deletable": child != self.root,
                }
            )

        return {
            "root": str(self.root),
            "current": self._relative(current),
            "parent": self._parent(current),
            "entries": entries,
        }

    def mkdir(self, relative_path: str, name: str) -> None:
        if self.readonly:
            raise ValueError("Read-only File Manager") 
            
        if not name or "/" in name or name in (".", ".."):
            raise ValueError("Folder name must be a single path segment")
        target = self._resolve(os.path.join(relative_path, name))
        target.mkdir(mode=0o750, exist_ok=False)
        self._set_owner(target)

    def delete(self, relative_path: str) -> None:
        if self.readonly:
            raise ValueError("Read-only File Manager") 
            
        target = self._resolve(relative_path)
        if target == self.root:
            raise ValueError("Refusing to delete repository root")
        if target.is_dir():
            shutil.rmtree(str(target))
        else:
            target.unlink()

    def save_upload(self, relative_path: str, relative_file_path: str, file_storage, overwrite: bool = False) -> Dict[str, object]:
        if self.readonly:
            raise ValueError("Read-only File Manager") 

        target = self._upload_target(relative_path, relative_file_path, overwrite)
        target_parent = target.parent
        target_parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._set_owner(target_parent)
        temp_target = target_parent / ".uploading.{}.{}".format(target.name, uuid.uuid4().hex)
        try:
            with temp_target.open("wb") as handle:
                while True:
                    chunk = file_storage.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            self._set_owner(temp_target)
            os.replace(str(temp_target), str(target))
            self._set_owner(target)
        except Exception:
            if temp_target.exists():
                temp_target.unlink()
            raise

        return {
            "name": target.name,
            "path": self._relative(target),
            "size": target.stat().st_size,
        }

    def save_upload_chunk(
        self,
        relative_path: str,
        relative_file_path: str,
        upload_id: str,
        chunk_stream,
        chunk_index: int,
        total_chunks: int,
        total_size: int,
        offset: int,
        overwrite: bool = False,
        temp_root: Optional[str] = None,
    ) -> Dict[str, object]:
        if self.readonly:
            raise ValueError("Read-only File Manager") 

        if total_chunks < 1:
            raise ValueError("Total chunks must be greater than zero")
        if chunk_index < 0 or chunk_index >= total_chunks:
            raise ValueError("Chunk index is outside the upload range")
        if total_size < 0 or offset < 0:
            raise ValueError("Chunk size metadata is invalid")
        try:
            clean_upload_id = uuid.UUID(upload_id).hex
        except (TypeError, ValueError):
            raise ValueError("Upload identifier is invalid")

        target = self._upload_target(relative_path, relative_file_path, overwrite)
        target_parent = target.parent
        target_parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._set_owner(target_parent)

        temp_dir = self._resolve_temp_root(temp_root)
        temp_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._set_owner(temp_dir)
        temp_target = temp_dir / "{}.part".format(clean_upload_id)

        try:
            with temp_target.open("r+b" if temp_target.exists() else "wb") as handle:
                handle.seek(offset)
                while True:
                    chunk = chunk_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            self._set_owner(temp_target)

            if chunk_index != total_chunks - 1:
                return {"complete": False, "path": self._relative(target)}

            if temp_target.stat().st_size != total_size:
                raise ValueError("Uploaded chunks did not match expected file size")

            os.replace(str(temp_target), str(target))
            self._set_owner(target)
        except Exception:
            if chunk_index == total_chunks - 1 and temp_target.exists():
                temp_target.unlink()
            raise

        return {
            "complete": True,
            "name": target.name,
            "path": self._relative(target),
            "size": target.stat().st_size,
        }

    def _resolve(self, relative_path: str) -> Path:
        candidate = (self.root / (relative_path or "")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Path escapes repository root")
        return candidate

    def _relative(self, path: Path) -> str:
        if path == self.root:
            return ""
        return str(path.relative_to(self.root))

    def _parent(self, path: Path) -> Optional[str]:
        if path == self.root:
            return None
        return self._relative(path.parent)

    def _set_owner(self, path: Path) -> None:
        if not self.owner:
            return
        try:
            user = pwd.getpwnam(self.owner)
        except KeyError:
            return
        os.chown(str(path), user.pw_uid, user.pw_gid)

    def _format_timestamp(self, timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def _is_hidden_repository_entry(self, name: str) -> bool:
        return name in ("lost+found", ".vis-upload-tmp") or name.startswith(".uploading.")

    def _upload_target(self, relative_path: str, relative_file_path: str, overwrite: bool) -> Path:
        if self.readonly:
            raise ValueError("Read-only File Manager") 

        clean_file_path = self._clean_upload_path(relative_file_path)
        target = self._resolve(os.path.join(relative_path or "", clean_file_path))
        target_parent = target.parent
        if target_parent != self.root and self.root not in target_parent.parents:
            raise ValueError("Path escapes repository root")
        if target.exists() and target.is_dir():
            raise IsADirectoryError(str(target))
        if target.exists() and not overwrite:
            raise FileExistsError(self._relative(target))
        return target

    def _resolve_temp_root(self, temp_root: Optional[str]) -> Path:
        if temp_root:
            candidate = Path(temp_root).resolve()
        else:
            candidate = (self.root / ".vis-upload-tmp").resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Upload staging path escapes repository root")
        return candidate

    def _clean_upload_path(self, relative_file_path: str) -> str:
        path = (relative_file_path or "").replace("\\", "/").strip("/")
        if not path:
            raise ValueError("Upload file path is required")
        parts = [part for part in path.split("/") if part]
        if any(part in (".", "..") for part in parts):
            raise ValueError("Upload path contains invalid segments")
        if not parts[-1]:
            raise ValueError("Upload file name is required")
        return "/".join(parts)
