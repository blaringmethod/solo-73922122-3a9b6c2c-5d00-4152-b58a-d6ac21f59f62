"""纸质图录短码：整表发码、同馆原码沿用、最短可用前缀分配与网页查回。

业务规则：
- 短码是图像完整 SHA-256 摘要的小写十六进制前缀，长度限 6～12 位，取最短可用者。
- 同馆一码只发给一个完整摘要；同馆同图（完整摘要）已发过码则沿用原码。
- 发码生效时，该前缀在同馆台账中须只对应这一个完整摘要；已撤回来源同样参与查重，
  完整摘要比较不区分大小写。
- 查重只按相关前缀探测（6 位锚定前缀索引），不扫描全馆台账。
- 遇到非 64 位十六进制摘要、6～12 位前缀全部冲突或写入失败：整表不发新码，原码仍可查。
"""
from __future__ import annotations

import re
import sqlite3

from .storage import (
    SHORT_CODE_MAX_LEN,
    SHORT_CODE_MIN_LEN,
    find_short_code,
    insert_short_code,
    scan_registry_prefix,
    scan_sources_prefix,
)

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CODE_RE = re.compile(r"^[0-9a-f]{6,12}$")


class CodeError(ValueError):
    """发码/查回的业务错误；消息可直接展示给编目员。"""


def is_canonical_sha256(digest: str) -> bool:
    """是否为 64 位十六进制摘要（大小写均可，发码时统一转小写）。"""
    return bool(isinstance(digest, str) and _HEX64_RE.fullmatch(digest))


def issue_codes_for_table(conn: sqlite3.Connection, payload: dict, table_id: int) -> dict:
    """对一份已确认表整表发码，按图序返回每张图的短码。

    全程在调用方的事务内执行：任一图不满足条件即抛 CodeError，不写入任何新码；
    SQLite 写入失败时异常上抛，由 session 上下文回滚，已发原码不受影响。
    """
    entries = payload.get("entries", [])
    if not entries:
        raise CodeError("该表没有图片条目，无法发码")

    # 1. 整表前置校验：任何一个摘要不是 64 位十六进制 → 整表不发新码
    bad = [
        (idx + 1, e.get("image_sha256", ""))
        for idx, e in enumerate(entries)
        if not is_canonical_sha256(e.get("image_sha256", "")) or not e.get("library_no")
    ]
    if bad:
        detail = "；".join(f"第 {idx} 图摘要 {digest[:16] or '（空）'}…" for idx, digest in bad[:3])
        raise CodeError(
            f"存在非 64 位十六进制的图像摘要（或缺少馆号）：{detail}。"
            "短码只能取自合法 SHA-256 摘要的前缀，本次整表不发新码，已发原码仍可查。"
        )

    results: list[dict] = []
    # 同批内已分配的码（理论上同表摘要不重复，仍做一层防护）
    batch: dict[tuple[str, str], str] = {}
    new_count = reused_count = 0

    for order, entry in enumerate(entries, start=1):
        library_no = entry["library_no"]
        digest_lower = entry["image_sha256"].lower()
        key = (library_no, digest_lower)

        # 2. 已发过的图沿用同馆原码（注册表唯一索引保证同馆同图至多一条）
        existing = conn.execute(
            "SELECT short_code FROM short_codes WHERE library_no = ? "
            "AND image_sha256 = ? COLLATE NOCASE LIMIT 1",
            (library_no, digest_lower),
        ).fetchone()
        if existing is not None:
            code = existing["short_code"]
            source = "reused"
            reused_count += 1
        elif key in batch:
            code = batch[key]
            source = "reused"
            reused_count += 1
        else:
            code = _allocate_prefix(conn, library_no, digest_lower)
            source = "new"
            new_count += 1
            batch[key] = code

        # 3. 新码立即落库，后续同表图片能感知它，保证同馆一码一摘要
        if source == "new":
            try:
                insert_short_code(conn, library_no, code, digest_lower, table_id)
            except sqlite3.Error as exc:
                raise CodeError(f"短码写入失败（馆号 {library_no}、图序 {order}）：{exc}。本次整表不发新码。") from exc

        results.append(
            {
                "order": order,
                "image_sha256": entry["image_sha256"],
                "library_no": library_no,
                "short_code": code,
                "source": source,
            }
        )

    return {
        "results": results,
        "new_count": new_count,
        "reused_count": reused_count,
        "total": len(results),
    }


def _allocate_prefix(conn: sqlite3.Connection, library_no: str, digest_lower: str) -> str:
    """在 6～12 位区间内找最短可用前缀；全部冲突则抛 CodeError。"""
    # 只取与本摘要 6 位锚定前缀相关的记录，不扫描全馆台账
    registry_rows = scan_registry_prefix(conn, library_no, digest_lower)
    source_rows = scan_sources_prefix(conn, library_no, digest_lower)

    other_digests: set[str] = set()
    taken_codes: set[str] = set()
    for r in source_rows:
        d = r["image_sha256"].lower()
        if d != digest_lower:
            other_digests.add(d)  # 含已撤回来源；非规范摘要也视为别的摘要
    for r in registry_rows:
        d = r["image_sha256"].lower()
        taken_codes.add(r["short_code"])
        if d != digest_lower:
            other_digests.add(d)

    for length in range(SHORT_CODE_MIN_LEN, SHORT_CODE_MAX_LEN + 1):
        candidate = digest_lower[:length]
        if candidate in taken_codes:
            continue  # 该码在同馆注册表已发给某个摘要
        # 生效条件：同馆台账中没有别的完整摘要以该前缀开头
        if not any(d.startswith(candidate) for d in other_digests):
            return candidate

    raise CodeError(
        f"馆号 {library_no} 的图 {digest_lower[:12]}… 与同馆其他摘要在 6～12 位前缀上全部冲突，"
        "找不到只对应本摘要的短码；本次整表不发新码，已发原码仍可查。"
    )


def lookup_short_code(conn: sqlite3.Connection, library_no: str, code_input: str) -> dict:
    """网页查回：馆号 + 已发短码 → 原完整摘要。

    只查短码注册表，不扫来源台账；台账后来补入同前缀的图也照样查回注册表中的原摘要。
    """
    library_no = (library_no or "").strip()
    code = (code_input or "").strip().lower()
    if not library_no:
        raise CodeError("请填写馆号")
    if not _CODE_RE.fullmatch(code):
        raise CodeError("短码须为 6～12 位小写十六进制字符（0-9a-f）")
    row = find_short_code(conn, library_no, code)
    if row is None:
        raise CodeError(f"馆号 {library_no} 的短码台账中查不到 {code}，无法查回")
    return {
        "library_no": row["library_no"],
        "short_code": row["short_code"],
        "image_sha256": row["image_sha256"],
        "table_id": row["table_id"],
        "created_at": row["created_at"],
    }
