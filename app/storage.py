"""SQLite 存储层：来源台账、临时草稿、已确认表。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager

# 短码取值区间：SHA-256（64 位小写十六进制）的最短可用前缀，限 6～12 位
SHORT_CODE_MIN_LEN = 6
SHORT_CODE_MAX_LEN = 12
SHA256_HEX_LEN = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    image_sha256 TEXT NOT NULL,
    library_no   TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('approved', 'revoked')),
    credit       TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    updated_at   REAL NOT NULL,
    UNIQUE (image_sha256, library_no)
);
CREATE INDEX IF NOT EXISTS idx_sources_image ON sources (image_sha256);

CREATE TABLE IF NOT EXISTS confirmed_tables (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmed_at REAL NOT NULL,
    image_count  INTEGER NOT NULL,
    libraries    TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    token       TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    hashes_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);

-- 短码台账：同馆一码只对应一个完整摘要；同馆同图（完整摘要）只发一个码。
-- 完整摘要以小写归一存储，查重/查回一律按大小写不敏感处理。
CREATE TABLE IF NOT EXISTS short_codes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    library_no   TEXT NOT NULL,
    short_code   TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    table_id     INTEGER,
    created_at   REAL NOT NULL
);
-- 同馆短码唯一：保证“同馆一码只发给一个摘要”
CREATE UNIQUE INDEX IF NOT EXISTS ux_short_codes_lib_code
    ON short_codes (library_no, short_code);
-- 同馆同图唯一：保证已发过的图沿用同馆原码
CREATE UNIQUE INDEX IF NOT EXISTS ux_short_codes_lib_image
    ON short_codes (library_no, image_sha256 COLLATE NOCASE);
-- 发码查重只按相关前缀探测，走该索引，不扫描全馆台账
CREATE INDEX IF NOT EXISTS ix_short_codes_lib_image_prefix
    ON short_codes (library_no, image_sha256 COLLATE NOCASE);
-- 来源台账按馆+图的前缀探测索引（含已撤回记录，撤回归档不删除）
CREATE INDEX IF NOT EXISTS ix_sources_lib_image
    ON sources (library_no, image_sha256 COLLATE NOCASE);
"""


def default_db_path() -> str:
    return os.environ.get(
        "CREDIT_DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "credit.db"),
    )


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def session(db_path: str | None = None):
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_source(conn: sqlite3.Connection, image_sha256: str, library_no: str,
                  status: str, credit: str, summary: str) -> None:
    """按 (图像 SHA-256, 馆号) 增补或更正一条台账记录。"""
    conn.execute(
        """
        INSERT INTO sources (image_sha256, library_no, status, credit, summary, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (image_sha256, library_no) DO UPDATE SET
            status     = excluded.status,
            credit     = excluded.credit,
            summary    = excluded.summary,
            updated_at = excluded.updated_at
        """,
        (image_sha256, library_no, status, credit, summary, time.time()),
    )


def fetch_sources(conn: sqlite3.Connection, image_hashes: list[str]) -> dict[str, list[sqlite3.Row]]:
    if not image_hashes:
        return {}
    placeholders = ",".join("?" for _ in image_hashes)
    rows = conn.execute(
        f"SELECT * FROM sources WHERE image_sha256 IN ({placeholders}) ORDER BY image_sha256, library_no",
        image_hashes,
    ).fetchall()
    out: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        out.setdefault(row["image_sha256"], []).append(row)
    return out


def all_images(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """每张图一行：取该图最新摘要（按 updated_at）。"""
    return conn.execute(
        """
        SELECT s.image_sha256,
               (SELECT summary FROM sources s2
                WHERE s2.image_sha256 = s.image_sha256
                ORDER BY s2.updated_at DESC, s2.id DESC LIMIT 1) AS summary,
               COUNT(*) AS source_count,
               SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved_count
        FROM sources s
        GROUP BY s.image_sha256
        ORDER BY s.image_sha256
        """
    ).fetchall()


def save_draft(conn: sqlite3.Connection, token: str, hashes: list[str], result: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO drafts (token, created_at, hashes_json, result_json) VALUES (?, ?, ?, ?)",
        (token, time.time(), json.dumps(hashes, ensure_ascii=False), json.dumps(result, ensure_ascii=False)),
    )


def get_draft(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM drafts WHERE token = ?", (token,)).fetchone()


def delete_draft(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM drafts WHERE token = ?", (token,))


def prune_drafts(conn: sqlite3.Connection, max_age_seconds: int = 86400) -> None:
    conn.execute("DELETE FROM drafts WHERE created_at < ?", (time.time() - max_age_seconds,))


def save_confirmed(conn: sqlite3.Connection, result: dict) -> int:
    cur = conn.execute(
        "INSERT INTO confirmed_tables (confirmed_at, image_count, libraries, payload_json) VALUES (?, ?, ?, ?)",
        (
            time.time(),
            len(result["entries"]),
            json.dumps(result["libraries"], ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
        ),
    )
    return int(cur.lastrowid)


def list_confirmed(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, confirmed_at, image_count, libraries, payload_json FROM confirmed_tables ORDER BY id DESC"
    ).fetchall()


def get_confirmed(conn: sqlite3.Connection, table_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM confirmed_tables WHERE id = ?", (table_id,)).fetchone()


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(DISTINCT image_sha256) AS images, COUNT(*) AS rows, "
        "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved FROM sources"
    ).fetchone()
    tables = conn.execute("SELECT COUNT(*) AS n FROM confirmed_tables").fetchone()["n"]
    return {
        "images": row["images"] or 0,
        "rows": row["rows"] or 0,
        "approved": row["approved"] or 0,
        "tables": tables,
    }


# ---------- 纸质图录短码 ----------

def get_short_codes_for_pairs(
    conn: sqlite3.Connection, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], sqlite3.Row]:
    """按 (馆号, 完整摘要) 批量取已发短码；摘要大小写不敏感。"""
    if not pairs:
        return {}
    out: dict[tuple[str, str], sqlite3.Row] = {}
    # 单条参数化查询，避免 IN 行值在不同 SQLite 版本的兼容问题；每表 ≤100 图
    for library_no, image_sha in pairs:
        row = conn.execute(
            "SELECT * FROM short_codes WHERE library_no = ? "
            "AND image_sha256 = ? COLLATE NOCASE LIMIT 1",
            (library_no, image_sha),
        ).fetchone()
        if row is not None:
            out[(library_no, image_sha.lower())] = row
    return out


def find_short_code(conn: sqlite3.Connection, library_no: str, short_code: str) -> sqlite3.Row | None:
    """网页查回：按馆号 + 已发短码精确取码记录（短码本身即小写）。"""
    return conn.execute(
        "SELECT * FROM short_codes WHERE library_no = ? AND short_code = ? LIMIT 1",
        (library_no, short_code.lower()),
    ).fetchone()


def scan_registry_prefix(
    conn: sqlite3.Connection, library_no: str, digest_lower: str
) -> list[sqlite3.Row]:
    """只探测该馆台账中与本摘要相关前缀的已发码记录。

    条件 lower(image) LIKE 'abcdef%'：以候选码最小长度 6 位做前缀锚定，
    走 (library_no, image_sha256 COLLATE NOCASE) 索引，不扫描全馆。
    """
    anchor = digest_lower[:SHORT_CODE_MIN_LEN]
    return conn.execute(
        "SELECT * FROM short_codes WHERE library_no = ? "
        "AND image_sha256 LIKE ? ESCAPE '\\' COLLATE NOCASE",
        (library_no, anchor + "%"),
    ).fetchall()


def scan_sources_prefix(
    conn: sqlite3.Connection, library_no: str, digest_lower: str
) -> list[sqlite3.Row]:
    """只探测该馆来源台账（含已撤回）中同前缀的完整摘要，大小写不敏感。"""
    anchor = digest_lower[:SHORT_CODE_MIN_LEN]
    return conn.execute(
        "SELECT image_sha256, status FROM sources WHERE library_no = ? "
        "AND image_sha256 LIKE ? ESCAPE '\\' COLLATE NOCASE",
        (library_no, anchor + "%"),
    ).fetchall()


def insert_short_code(
    conn: sqlite3.Connection, library_no: str, short_code: str,
    image_sha256: str, table_id: int | None,
) -> None:
    conn.execute(
        "INSERT INTO short_codes (library_no, short_code, image_sha256, table_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (library_no, short_code, image_sha256.lower(), table_id, time.time()),
    )
