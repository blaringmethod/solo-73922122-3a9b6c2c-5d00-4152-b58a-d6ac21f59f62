"""短码发码 / 沿用 / 查回的单元与 Web 集成测试。

运行（仓库根目录）：
    PYTHONPATH=/tmp/pylibs python3 -s -m unittest -v tests.test_codes
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest

from app import create_app
from app.codes import CodeError, is_canonical_sha256, issue_codes_for_table, lookup_short_code
from app.ledger import import_ledger
from app.storage import (
    connect,
    find_short_code,
    init_db,
    scan_sources_prefix,
)

CSV_HEADER = "image_sha256,library_no,status,credit,summary\n"

# 构造两组确定的 64 位十六进制“摘要”，用于控制前缀冲突
H1 = "a" * 64                                   # aaaaaa…
H2 = "a" * 6 + "b" + "a" * 57                   # 前 6 位与 H1 相同，第 7 位不同
H3 = "a" * 12 + "c" + "a" * 51                  # 前 12 位与 H1 相同
H4 = "b" * 64                                   # 完全不同的前缀
H1_UP = "A" * 64                                # H1 的大写形式（同一摘要，大小写不敏感）
NON_HEX = "z" * 64                              # 64 位但非十六进制
SHORT_BAD = "abc"                               # 长度不足


def csv_bytes(rows: list[tuple]) -> bytes:
    out = CSV_HEADER
    for r in rows:
        out += ",".join(f'"{v}"' for v in r) + "\n"
    return out.encode("utf-8")


def payload(entries: list[tuple[str, str]]) -> dict:
    """entries: [(image_sha, library_no), ...]，图序即列表顺序。"""
    return {
        "feasible": True,
        "entries": [
            {"image_sha256": h, "library_no": lib, "credit": f"署名-{lib}", "summary": f"摘要-{h[:6]}"}
            for h, lib in entries
        ],
        "libraries": sorted({lib for _, lib in entries}),
        "library_count": len({lib for _, lib in entries}),
    }


class CodeUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = connect(self.tmp.name)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _load(self, rows):
        import_ledger(self.conn, csv_bytes(rows))

    def test_canonical_sha256_validation(self):
        self.assertTrue(is_canonical_sha256(H1))
        self.assertTrue(is_canonical_sha256(H1_UP))
        self.assertFalse(is_canonical_sha256(NON_HEX))
        self.assertFalse(is_canonical_sha256(SHORT_BAD))
        self.assertFalse(is_canonical_sha256(""))

    def test_first_code_is_shortest_six_lowercase(self):
        self._load([(H1, "L01", "approved", "c", "s")])
        out = issue_codes_for_table(self.conn, payload([(H1, "L01")]), table_id=1)
        self.conn.commit()
        self.assertEqual(out["results"][0]["short_code"], "a" * 6)
        self.assertEqual(out["new_count"], 1)
        self.assertEqual(out["results"][0]["source"], "new")
        row = find_short_code(self.conn, "L01", "a" * 6)
        self.assertIsNotNone(row)
        self.assertEqual(row["image_sha256"], H1)  # 落库统一小写

    def test_prefix_collision_uses_longer_prefix(self):
        # 同馆两张图前 6 位相同：两图的 6 位前缀在同馆台账都不唯一，均须取 7 位
        self._load([
            (H1, "L01", "approved", "c1", "s1"),
            (H2, "L01", "approved", "c2", "s2"),
        ])
        out = issue_codes_for_table(self.conn, payload([(H1, "L01"), (H2, "L01")]), table_id=1)
        self.conn.commit()
        codes = {r["image_sha256"]: r["short_code"] for r in out["results"]}
        self.assertEqual(codes[H1], "a" * 7)
        self.assertEqual(codes[H2], "a" * 6 + "b")
        self.assertTrue(all(len(c) == 7 for c in codes.values()))
        # 先到的图在同馆无冲突时仍取最短 6 位（下例 H4 独占 b 前缀）
        out_alone = issue_codes_for_table(self.conn, payload([(H4, "L01")]), table_id=2)
        self.conn.commit()
        self.assertEqual(out_alone["results"][0]["short_code"], "b" * 6)

    def test_revoked_source_participates_in_collision_check(self):
        # H2 在 L01 只有一条撤回记录，仍须参与查重：H1 不能取 6 位码
        self._load([
            (H1, "L01", "approved", "c1", "s1"),
            (H2, "L01", "revoked", "c2", "s2"),
        ])
        out = issue_codes_for_table(self.conn, payload([(H1, "L01")]), table_id=1)
        self.conn.commit()
        self.assertEqual(out["results"][0]["short_code"], "a" * 7)
        # 现在给 H2 发码（哪怕它在该馆仅撤回）：取自己的 7 位前缀，与 H1 不撞
        out2 = issue_codes_for_table(self.conn, payload([(H2, "L01")]), table_id=2)
        self.conn.commit()
        self.assertEqual(out2["results"][0]["short_code"], "a" * 6 + "b")

    def test_digest_comparison_is_case_insensitive(self):
        # 台账里存大写 H1_UP；给大写摘要发码后，小写查询应命中同一条
        self._load([(H1_UP, "L01", "approved", "c", "s")])
        out = issue_codes_for_table(self.conn, payload([(H1_UP, "L01")]), table_id=1)
        self.conn.commit()
        self.assertEqual(out["results"][0]["short_code"], "a" * 6)
        row = find_short_code(self.conn, "L01", "a" * 6)
        self.assertEqual(row["image_sha256"], H1)
        # 同馆同图再次发码（这次用小写）：沿用原码，不新增
        out2 = issue_codes_for_table(self.conn, payload([(H1, "L01")]), table_id=2)
        self.conn.commit()
        self.assertEqual(out2["results"][0]["source"], "reused")
        self.assertEqual(out2["new_count"], 0)
        n = self.conn.execute("SELECT COUNT(*) AS n FROM short_codes").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_same_image_different_library_gets_separate_code(self):
        self._load([
            (H4, "L01", "approved", "c1", "s"),
            (H4, "L02", "approved", "c2", "s"),
        ])
        out = issue_codes_for_table(self.conn, payload([(H4, "L01"), (H4, "L02")]), table_id=1)
        self.conn.commit()
        self.assertEqual([r["short_code"] for r in out["results"]], ["b" * 6, "b" * 6])
        n = self.conn.execute("SELECT COUNT(*) AS n FROM short_codes").fetchone()["n"]
        self.assertEqual(n, 2)  # 同码可在不同馆各发一次

    def test_reuse_same_library_original_code_in_new_table(self):
        self._load([(H1, "L01", "approved", "c", "s"), (H4, "L01", "approved", "c4", "s4")])
        issue_codes_for_table(self.conn, payload([(H1, "L01")]), table_id=1)
        self.conn.commit()
        out = issue_codes_for_table(self.conn, payload([(H4, "L01"), (H1, "L01")]), table_id=2)
        self.conn.commit()
        by_img = {r["image_sha256"]: r for r in out["results"]}
        self.assertEqual(by_img[H1]["short_code"], "a" * 6)
        self.assertEqual(by_img[H1]["source"], "reused")
        self.assertEqual(by_img[H4]["source"], "new")
        # 图序保留：H4 在第 1 位
        self.assertEqual(out["results"][0]["image_sha256"], H4)

    def test_non_hex_digest_aborts_whole_table_no_new_codes(self):
        self._load([(H1, "L01", "approved", "c1", "s1")])
        # 先给 H1 发码（原码）
        issue_codes_for_table(self.conn, payload([(H1, "L01")]), table_id=1)
        self.conn.commit()
        before = self.conn.execute("SELECT COUNT(*) AS n FROM short_codes").fetchone()["n"]
        # 新表里混有非法摘要：整表不发新码
        with self.assertRaises(CodeError) as ctx:
            issue_codes_for_table(
                self.conn,
                payload([(H4, "L01"), (NON_HEX, "L01")]),
                table_id=2,
            )
        self.conn.rollback()
        self.assertIn("64 位十六进制", str(ctx.exception))
        after = self.conn.execute("SELECT COUNT(*) AS n FROM short_codes").fetchone()["n"]
        self.assertEqual(before, after)  # 没有任何新码写入
        # 原码仍可查
        self.assertIsNotNone(find_short_code(self.conn, "L01", "a" * 6))

    def test_all_prefixes_conflicted_aborts_whole_table(self):
        # 先发 H1（此时同馆无冲突，得 6 位码），再补入前 12 位完全相同的 H3：
        # H3 在 6～12 位内的每个前缀都与 H1 撞，整表发码失败
        self._load([(H1, "L01", "approved", "c1", "s1")])
        issue_codes_for_table(self.conn, payload([(H1, "L01")]), table_id=1)
        self.conn.commit()
        self._load([(H3, "L01", "approved", "c3", "s3")])
        with self.assertRaises(CodeError) as ctx:
            issue_codes_for_table(self.conn, payload([(H3, "L01")]), table_id=2)
        self.conn.rollback()
        self.assertIn("全部冲突", str(ctx.exception))
        # H1 原码不受影响
        self.assertIsNotNone(find_short_code(self.conn, "L01", "a" * 6))

    def test_registry_only_digest_still_blocks_prefix(self):
        # 注册表里已有同前缀摘要（防御性查重）：即使来源台账没有该图，也不能重码
        from app.storage import insert_short_code
        insert_short_code(self.conn, "L01", "a" * 6, H1, table_id=99)
        self.conn.commit()
        # H2 与注册表中的 H1 前 6 位相同：必须让到 7 位
        self._load([(H2, "L01", "approved", "c2", "s2")])
        out = issue_codes_for_table(self.conn, payload([(H2, "L01")]), table_id=1)
        self.conn.commit()
        self.assertEqual(out["results"][0]["short_code"], "a" * 6 + "b")
        row = find_short_code(self.conn, "L01", "a" * 6 + "b")
        self.assertEqual(row["image_sha256"], H2)

    def test_lookup_returns_full_digest_and_survives_later_ledger_addition(self):
        self._load([(H1, "L01", "approved", "c1", "s1")])
        issue_codes_for_table(self.conn, payload([(H1, "L01")]), table_id=1)
        self.conn.commit()
        found = lookup_short_code(self.conn, "L01", "a" * 6)
        self.assertEqual(found["image_sha256"], H1)
        # 台账后来补入同前缀（前 6 位相同）的另一张图：查回仍返回注册表原摘要
        self._load([(H2, "L01", "approved", "c2", "s2")])
        found2 = lookup_short_code(self.conn, "L01", "a" * 6)
        self.assertEqual(found2["image_sha256"], H1)

    def test_lookup_errors(self):
        with self.assertRaises(CodeError):
            lookup_short_code(self.conn, "L01", "xyz")          # 长度/字符非法
        with self.assertRaises(CodeError):
            lookup_short_code(self.conn, "", "a" * 6)           # 缺馆号
        with self.assertRaises(CodeError):
            lookup_short_code(self.conn, "L99", "a" * 6)        # 查无此码

    def test_prefix_scans_are_anchor_scoped_not_full_scan(self):
        # EXPLAIN 验证：来源台账前缀探测走索引，且 SQL 带 6 位锚定前缀
        self._load([(H1, "L01", "approved", "c", "s"), (H4, "L01", "approved", "c4", "s4")])
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT image_sha256, status FROM sources WHERE library_no = ? "
            "AND image_sha256 LIKE ? ESCAPE '\\' COLLATE NOCASE",
            ("L01", H1[:6] + "%"),
        ).fetchall()
        joined = " ".join(r["detail"] for r in plan)
        self.assertIn("ix_sources_lib_image", joined)
        self.assertNotIn("SCAN sources", joined)
        # 实际只回同前缀的行，H4（b 开头）不会被扫到
        rows = scan_sources_prefix(self.conn, "L01", H1)
        self.assertEqual({r["image_sha256"] for r in rows}, {H1})


class CodeWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.app = create_app(db_path=self.tmp.name)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self._upload([
            (H1, "L01", "approved", "甲馆署名", "青铜鼎"),
            (H2, "L01", "approved", "甲馆署名二", "壁画"),
            (H4, "L02", "approved", "乙馆署名", "金冠"),
        ])

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _upload(self, rows):
        data = {"ledger": (io.BytesIO(csv_bytes(rows)), "ledger.csv")}
        return self.client.post("/ledger/upload", data=data,
                                content_type="multipart/form-data", follow_redirects=True)

    def _confirm_table(self, picks):
        from werkzeug.datastructures import MultiDict
        resp = self.client.post("/solve", data=MultiDict([("selected", h) for h in picks]),
                                follow_redirects=False)
        token = resp.headers["Location"].rsplit("/", 1)[-1]
        resp = self.client.post(f"/confirm/{token}", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        return int(resp.headers["Location"].rsplit("/", 1)[-1])

    def test_issue_codes_on_confirmed_table_page(self):
        table_id = self._confirm_table([H1, H2, H4])
        page = self.client.post(f"/tables/{table_id}/issue-codes", follow_redirects=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("短码发放完成".encode(), page.data)
        # 详情页按图序展示短码：H1/H2 同前缀各取 7 位，H4 独占前缀取 6 位
        self.assertIn(("a" * 7), page.data.decode())
        self.assertIn(("a" * 6 + "b"), page.data.decode())
        self.assertIn("b" * 6, page.data.decode())
        self.assertIn("排版用短码清单".encode(), page.data)
        # 再次发码：全部沿用，不新发
        again = self.client.post(f"/tables/{table_id}/issue-codes", follow_redirects=True)
        self.assertIn("新发 0 个".encode(), again.data)
        self.assertIn("沿用同馆原码 3 个".encode(), again.data)

    def test_invalid_digest_flashes_and_keeps_existing_codes(self):
        # 先确认一张合法表并发码
        table_id = self._confirm_table([H4])
        self.client.post(f"/tables/{table_id}/issue-codes", follow_redirects=True)
        # 直接构造一张含非法摘要的已确认表（绕过求解，模拟脏数据入库）
        from app.storage import save_confirmed
        with self.app.app_context():
            from app.storage import session
            with session(self.tmp.name) as conn:
                bad_id = save_confirmed(conn, payload([(NON_HEX, "L01")]))
        page = self.client.post(f"/tables/{bad_id}/issue-codes", follow_redirects=True)
        self.assertIn("整表未发新码".encode(), page.data)
        self.assertIn("64 位十六进制".encode(), page.data)
        # 原码仍可查
        lookup = self.client.get(f"/codes/lookup?library_no=L02&code={'b' * 6}")
        self.assertIn(H4.encode(), lookup.data)

    def test_lookup_page_roundtrip(self):
        table_id = self._confirm_table([H1])
        issue_page = self.client.post(f"/tables/{table_id}/issue-codes", follow_redirects=True)
        # H1 与台账中的 H2 同 6 位前缀，发出的码是 7 位 aaaaaaa
        self.assertIn(("a" * 7), issue_page.data.decode())
        # 大写输入也能查（输入归一为小写）
        page = self.client.get(f"/codes/lookup?library_no=L01&code={'A' * 7}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("查回成功".encode(), page.data)
        self.assertIn(H1.encode(), page.data)
        # 查无此码
        miss = self.client.get("/codes/lookup?library_no=L01&code=ffffff")
        self.assertIn("查不到".encode(), miss.data)
        # 非法格式
        bad = self.client.get("/codes/lookup?library_no=L01&code=zz")
        self.assertIn("6～12 位".encode(), bad.data)

    def test_nav_contains_lookup_entry(self):
        page = self.client.get("/")
        self.assertIn("/codes/lookup".encode(), page.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
