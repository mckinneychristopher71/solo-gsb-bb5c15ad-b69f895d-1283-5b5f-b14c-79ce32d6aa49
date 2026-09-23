"""SQLite 持久层：启动时建库建表，提供审计记录的存取。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_BASE_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = Path(os.environ.get("DATABASE_URL", str(_BASE_DIR / "audit.db")))
# 允许 DATABASE_URL=sqlite:////path 风格
if str(DB_PATH).startswith("sqlite:///"):
    DB_PATH = Path(str(DB_PATH).replace("sqlite:///", "", 1))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS manifests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    package_count INTEGER NOT NULL,
    edge_count    INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL,
    warning_count INTEGER NOT NULL,
    result_json   TEXT    NOT NULL,
    manifest_json TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_id INTEGER NOT NULL REFERENCES manifests(id) ON DELETE CASCADE,
    conflict_uid TEXT NOT NULL,
    severity    TEXT NOT NULL,
    status      TEXT NOT NULL,
    ancestor    TEXT NOT NULL,
    node        TEXT NOT NULL,
    distance    INTEGER NOT NULL,
    path_json   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conflicts_manifest ON conflicts(manifest_id);
CREATE INDEX IF NOT EXISTS idx_conflicts_node ON conflicts(node);
CREATE INDEX IF NOT EXISTS idx_manifests_created ON manifests(created_at);
"""


def get_db_path() -> Path:
    return DB_PATH


def init_db() -> None:
    """启动时调用：确保目录与数据库文件存在并完成建表。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def save_audit(result: Dict[str, Any], raw_manifest: Any) -> int:
    """持久化一次审计，返回自增主键。"""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = dict(result)
    result["created_at"] = created_at
    summary = result["summary"]
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO manifests
                (name, status, package_count, edge_count, conflict_count,
                 warning_count, result_json, manifest_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["manifest_name"],
                result["status"],
                summary["packages"],
                summary["edges"],
                summary["conflicts"],
                summary["license_warnings"] + summary["warnings"],
                json.dumps(result, ensure_ascii=False),
                json.dumps(raw_manifest, ensure_ascii=False),
                created_at,
            ),
        )
        manifest_id = int(cur.lastrowid)
        # audit_id 写回结果后再持久化，保证导出 JSON 自带主键
        result["audit_id"] = manifest_id
        conn.execute(
            "UPDATE manifests SET result_json = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), manifest_id),
        )
        rows = [
            (
                manifest_id,
                c["id"],
                c["severity"],
                c["status"],
                c["ancestor"],
                c["node"],
                c["distance"],
                json.dumps(c["path"], ensure_ascii=False),
                c["reason"],
                created_at,
            )
            for c in result.get("conflicts", [])
        ]
        conn.executemany(
            """
            INSERT INTO conflicts
                (manifest_id, conflict_uid, severity, status, ancestor, node,
                 distance, path_json, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    return manifest_id


def _manifest_brief(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "package_count": row["package_count"],
        "edge_count": row["edge_count"],
        "conflict_count": row["conflict_count"],
        "warning_count": row["warning_count"],
        "created_at": row["created_at"],
    }


def list_audits(limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM manifests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_manifest_brief(r) for r in rows]


def get_audit(manifest_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT result_json FROM manifests WHERE id = ?", (manifest_id,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["result_json"])


def audit_exists(manifest_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM manifests WHERE id = ?", (manifest_id,)
        ).fetchone()
    return row is not None


def delete_audit(manifest_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM manifests WHERE id = ?", (manifest_id,))
        conn.commit()
        return cur.rowcount > 0
