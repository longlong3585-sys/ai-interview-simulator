"""T-02 测试：覆盖 T-01 交付的备份校验逻辑。

要点：**正向与负向都测**。只测"好备份通过"不足以证明校验器有效——
一个恒返回 True 的校验器也能通过正向用例。因此负向用例是必需的。
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from scripts.make_backup import EXPECTED_TABLES as BACKUP_TABLES
from scripts.verify_backup import verify

_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, is_active BOOLEAN);
CREATE TABLE interview_records (id INTEGER PRIMARY KEY, user_id INTEGER, report TEXT);
CREATE TABLE notifications (id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT);
"""


def _make_valid_db(path, users=2, records=3):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        for i in range(users):
            conn.execute("INSERT INTO users (username, is_active) VALUES (?, 1)", ("u%d" % i,))
        for i in range(records):
            conn.execute(
                "INSERT INTO interview_records (user_id, report) VALUES (1, ?)", ('{"x":1}',)
            )
        conn.commit()
    finally:
        conn.close()
    return path


class BackupVerifyTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-verify-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _p(self, name):
        return os.path.join(self.tmp, name)

    # ---------- 正向 ----------

    def test_valid_backup_passes(self):
        db = _make_valid_db(self._p("good.db"))
        ok, problems, info = verify(db)
        self.assertTrue(ok, "有效备份应通过，实际问题：%s" % problems)
        self.assertEqual(info["integrity_check"], "ok")
        self.assertTrue(BACKUP_TABLES.issubset(set(info["tables"])))
        self.assertEqual(info["row_counts"]["users"], 2)
        self.assertEqual(info["row_counts"]["interview_records"], 3)

    def test_manifest_sha256_and_counts_match(self):
        db = _make_valid_db(self._p("good.db"))
        with open(db, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        manifest = self._p("manifest.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "database": {
                        "sha256": digest,
                        "row_counts": {"users": 2, "interview_records": 3, "notifications": 0},
                    }
                },
                f,
            )
        ok, problems, _ = verify(db, manifest)
        self.assertTrue(ok, "manifest 一致时应通过，实际问题：%s" % problems)

    # ---------- 负向（关键） ----------

    def test_missing_file_fails(self):
        ok, problems, _ = verify(self._p("nope.db"))
        self.assertFalse(ok)
        self.assertTrue(any("not found" in p for p in problems))

    def test_empty_file_fails(self):
        p = self._p("empty.db")
        open(p, "wb").close()
        ok, problems, _ = verify(p)
        self.assertFalse(ok)
        self.assertTrue(any("empty" in p for p in problems))

    def test_truncated_file_fails(self):
        """截断的 SQLite 文件必须被判为不可用。"""
        db = _make_valid_db(self._p("good.db"), users=5, records=5)
        with open(db, "r+b") as f:
            f.truncate(200)
        ok, problems, _ = verify(db)
        self.assertFalse(ok, "截断文件不应通过校验")
        self.assertTrue(problems)

    def test_garbage_file_fails(self):
        p = self._p("garbage.db")
        with open(p, "wb") as f:
            f.write(b"this is definitely not a sqlite database" * 10)
        ok, problems, _ = verify(p)
        self.assertFalse(ok)
        self.assertTrue(any("cannot open" in x or "integrity" in x for x in problems))

    def test_missing_expected_table_fails(self):
        """缺少业务表的库必须被判为不合格。"""
        p = self._p("partial.db")
        conn = sqlite3.connect(p)
        conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY);")
        conn.commit()
        conn.close()
        ok, problems, _ = verify(p)
        self.assertFalse(ok)
        self.assertTrue(any("missing expected tables" in x for x in problems))

    def test_manifest_sha_mismatch_fails(self):
        db = _make_valid_db(self._p("good.db"))
        manifest = self._p("bad_manifest.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"database": {"sha256": "0" * 64}}, f)
        ok, problems, _ = verify(db, manifest)
        self.assertFalse(ok, "sha256 不匹配时必须失败")
        self.assertTrue(any("sha256 mismatch" in p for p in problems))

    def test_manifest_row_count_mismatch_fails(self):
        db = _make_valid_db(self._p("good.db"), users=2)
        manifest = self._p("count_manifest.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"database": {"row_counts": {"users": 999}}}, f)
        ok, problems, _ = verify(db, manifest)
        self.assertFalse(ok, "行数不匹配时必须失败")
        self.assertTrue(any("row_count mismatch" in p for p in problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
