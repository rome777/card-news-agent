"""프로젝트·실행·요청 기록을 SQLite 한 파일에 저장한다.

웹 요청과 작업 스레드가 같은 연결을 쓰므로 모든 DB 접근을 하나의 잠금으로 직렬화한다.
각 레코드는 JSON 문서 하나로 저장하고, 프로젝트는 바뀔 때마다 version을 올린다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  updated_at TEXT NOT NULL,
  doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_project ON runs (project_id, updated_at);
CREATE TABLE IF NOT EXISTS requests (
  scope TEXT NOT NULL,
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (scope, request_id)
);
"""

MAX_RUN_EVENTS = 300


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class NotFound(Exception):
    pass


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        (data_dir / "projects").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(data_dir / "app.db", check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def project_dir(self, project_id: str) -> Path:
        path = self.data_dir / "projects" / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # 프로젝트

    def create_project(self, doc: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            ts = now_iso()
            doc = {**doc, "created_at": ts, "updated_at": ts, "version": 1}
            self._db.execute(
                "INSERT INTO projects (id, updated_at, doc) VALUES (?, ?, ?)",
                (doc["id"], ts, _dump(doc)),
            )
            self.project_dir(doc["id"])
            return doc

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT doc FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFound(project_id)
        return json.loads(row[0])

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT doc FROM projects ORDER BY updated_at DESC").fetchall()
        return [json.loads(r[0]) for r in rows]

    def update_project(self, project_id: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """change가 문서를 고친 뒤 version을 하나 올려 저장한다."""
        with self._lock:
            doc = self.get_project(project_id)
            change(doc)
            doc["version"] += 1
            doc["updated_at"] = now_iso()
            self._db.execute(
                "UPDATE projects SET updated_at = ?, doc = ? WHERE id = ?",
                (doc["updated_at"], _dump(doc), project_id),
            )
            return doc

    # 실행

    def create_run(self, doc: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            ts = now_iso()
            doc = {"events": [], "error": None, **doc, "created_at": ts, "updated_at": ts}
            self._db.execute(
                "INSERT INTO runs (id, project_id, status, updated_at, doc) VALUES (?, ?, ?, ?, ?)",
                (doc["id"], doc["project_id"], doc["status"], ts, _dump(doc)),
            )
            return doc

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT doc FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(run_id)
        return json.loads(row[0])

    def list_runs(self, project_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT doc FROM runs WHERE project_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def runs_with_status(self, *statuses: str) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._db.execute(f"SELECT doc FROM runs WHERE status IN ({marks})", statuses).fetchall()
        return [json.loads(r[0]) for r in rows]

    def update_run(self, run_id: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            doc = self.get_run(run_id)
            change(doc)
            doc["events"] = doc["events"][-MAX_RUN_EVENTS:]
            doc["updated_at"] = now_iso()
            self._db.execute(
                "UPDATE runs SET status = ?, updated_at = ?, doc = ? WHERE id = ?",
                (doc["status"], doc["updated_at"], _dump(doc), run_id),
            )
            return doc

    # 중복 요청 식별

    def get_request(self, scope: str, request_id: str) -> tuple[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT fingerprint, response FROM requests WHERE scope = ? AND request_id = ?",
                (scope, request_id),
            ).fetchone()
        return None if row is None else (row[0], json.loads(row[1]))

    def save_request(self, scope: str, request_id: str, fingerprint: str, response: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO requests (scope, request_id, fingerprint, response, created_at) VALUES (?, ?, ?, ?, ?)",
                (scope, request_id, fingerprint, _dump(response), now_iso()),
            )

    @property
    def lock(self) -> threading.RLock:
        return self._lock


def _dump(doc: Any) -> str:
    return json.dumps(doc, ensure_ascii=False)
