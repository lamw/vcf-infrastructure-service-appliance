import json
import os
import sqlite3
from copy import deepcopy
from typing import Iterable, List, Optional

from .definitions import INITIAL_SERVICES
from .models import ServiceDefinition, ValidationResult


DEFAULT_DB_PATH = "/opt/vis/state/vis.db"
CATALOG_ID_ALIASES = {
    "directory-identity-provider": "ldap-provider",
    "oidc-identity-provider": "oidc-provider",
}
RETIRED_SERVICE_IDS = {"identity-provider", "directory-identity-provider", "oidc-identity-provider"}


class ServiceStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.environ.get("VIS_DB_PATH", DEFAULT_DB_PATH)

    def initialize(self) -> None:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS services (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    configured INTEGER NOT NULL,
                    health_status TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    filesystem_root TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    validation_ok INTEGER NOT NULL,
                    validation_message TEXT NOT NULL,
                    validation_checked_at TEXT NOT NULL,
                    last_health_check_time TEXT,
                    quick_actions_json TEXT NOT NULL
                )
                """
            )
            count = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
            if count == 0:
                self.upsert_many(deepcopy(INITIAL_SERVICES), conn=conn)
            else:
                self.reconcile_catalog(conn=conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'admin',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_login_at TEXT
                )
                """
            )

    def ensure_initial_admin(self, username: str, password_hash: str) -> None:
        username = username.strip()
        if not username or not password_hash:
            return
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count == 0:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, active) VALUES (?, ?, 'admin', 1)",
                    (username, password_hash),
                )

    def list_users(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, username, role, active, created_at, updated_at, last_login_at FROM users ORDER BY username"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user_by_username(self, username: str):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def get_user(self, user_id: int):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def save_user(self, username: str, password_hash: str, role: str = "admin", active: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, active)
                VALUES (?, ?, ?, ?)
                """,
                (username.strip(), password_hash, role, int(active)),
            )

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (password_hash, user_id),
            )

    def update_user_login_time(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))

    def reconcile_catalog(self, conn=None) -> None:
        owns_connection = conn is None
        conn = conn or self._connect()
        try:
            for old_id, new_id in CATALOG_ID_ALIASES.items():
                old = conn.execute("SELECT * FROM services WHERE id = ?", (old_id,)).fetchone()
                new = conn.execute("SELECT * FROM services WHERE id = ?", (new_id,)).fetchone()
                if old and not new:
                    conn.execute("UPDATE services SET id = ? WHERE id = ?", (new_id, old_id))
                elif old and new:
                    old_service = self._row_to_service(old)
                    new_service = self._row_to_service(new)
                    merged_settings = dict(new_service.settings)
                    merged_settings.update(old_service.settings)
                    new_service.settings = merged_settings
                    new_service.enabled = old_service.enabled
                    new_service.configured = old_service.configured
                    new_service.health_status = old_service.health_status
                    new_service.last_validation_result = old_service.last_validation_result
                    new_service.last_health_check_time = old_service.last_health_check_time
                    self.upsert_many([new_service], conn=conn)
            for service in deepcopy(INITIAL_SERVICES):
                existing = conn.execute("SELECT * FROM services WHERE id = ?", (service.id,)).fetchone()
                if existing:
                    current = self._row_to_service(existing)
                    merged_settings = dict(service.settings)
                    if service.id == "web-depot" and "auth" in current.settings:
                        current.settings.pop("auth", None)
                        current.settings.pop("protocol", None)
                        current.settings.pop("port", None)
                        current.settings.pop("path", None)
                    merged_settings.update(current.settings)
                    service.settings = merged_settings
                    service.enabled = current.enabled
                    service.configured = current.configured
                    service.health_status = current.health_status
                    service.last_validation_result = current.last_validation_result
                    service.last_health_check_time = current.last_health_check_time
                self.upsert_many([service], conn=conn)
            for service_id in RETIRED_SERVICE_IDS:
                conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
            if owns_connection:
                conn.commit()
        finally:
            if owns_connection:
                conn.close()

    def list_services(self) -> List[ServiceDefinition]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM services
                ORDER BY CASE id
                    WHEN 'web-depot' THEN 1
                    WHEN 'sftp-backup' THEN 2
                    WHEN 'harbor-registry' THEN 3
                    WHEN 'ldap-provider' THEN 4
                    WHEN 'oidc-provider' THEN 5
                    WHEN 'unbound-dns' THEN 6
                    WHEN 'time-server' THEN 7
                    WHEN 'dhcp-server' THEN 8
                    WHEN 'kms-service' THEN 9
                    WHEN 'content-library' THEN 10
                    ELSE 99
                END, id
                """
            ).fetchall()
        return [self._row_to_service(row) for row in rows]

    def get_service(self, service_id: str) -> Optional[ServiceDefinition]:
        service_id = CATALOG_ID_ALIASES.get(service_id, service_id)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        return self._row_to_service(row) if row else None

    def save_service(self, service: ServiceDefinition) -> None:
        with self._connect() as conn:
            self.upsert_many([service], conn=conn)

    def upsert_many(self, services: Iterable[ServiceDefinition], conn=None) -> None:
        owns_connection = conn is None
        conn = conn or self._connect()
        try:
            conn.executemany(
                """
                INSERT OR REPLACE INTO services (
                    id, name, description, enabled, configured, health_status, endpoint,
                    filesystem_root, settings_json, validation_ok, validation_message,
                    validation_checked_at, last_health_check_time, quick_actions_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._service_to_tuple(service) for service in services],
            )
            if owns_connection:
                conn.commit()
        finally:
            if owns_connection:
                conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _service_to_tuple(self, service: ServiceDefinition):
        return (
            service.id,
            service.name,
            service.description,
            int(service.enabled),
            int(service.configured),
            service.health_status,
            service.endpoint,
            service.filesystem_root,
            json.dumps(service.settings, sort_keys=True),
            int(service.last_validation_result.ok),
            service.last_validation_result.message,
            service.last_validation_result.checked_at,
            service.last_health_check_time,
            json.dumps(service.quick_actions),
        )

    def _row_to_service(self, row) -> ServiceDefinition:
        return ServiceDefinition(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            enabled=bool(row["enabled"]),
            configured=bool(row["configured"]),
            health_status=row["health_status"],
            endpoint=row["endpoint"],
            filesystem_root=row["filesystem_root"],
            settings=json.loads(row["settings_json"]),
            last_validation_result=ValidationResult(
                ok=bool(row["validation_ok"]),
                message=row["validation_message"],
                checked_at=row["validation_checked_at"],
            ),
            last_health_check_time=row["last_health_check_time"],
            quick_actions=json.loads(row["quick_actions_json"]),
        )
