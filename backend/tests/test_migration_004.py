"""T-19 测试：迁移 004 —— 给 `interview_sessions` 补 `report` 列。

背景（这是一次**发现文档矛盾**后的补救）：
  ADR-004 明确要求 `UPDATE interview_sessions SET report=:json, ...`，
  但 §6.2 的建表语句里没有 `report` 列，而 T-16 的协议已经声明了
  `finish(..., report_json, ...)`。实现 T-19 时矛盾无法再回避。

本文件验证 004 是**安全的纯加列**：既有会话行不受影响、索引与外键存活、
其它表一个字节都不动。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from migrations.runner import load_migrations, run  # noqa: E402
from tests.support import versions_dir_up_to  # noqa: E402

VERSIONS_DIR = os.path.join(BACKEND_DIR, "migrations", "versions")


def noop(*args, **kwargs):
    pass


def columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return [d[1] for d in conn.execute("PRAGMA table_info([%s])" % table)]
    finally:
        conn.close()


def column_detail(path, table, name):
    conn = sqlite3.connect(path)
    try:
        for d in conn.execute("PRAGMA table_info([%s])" % table):
            if d[1] == name:
                return {"type": d[2], "notnull": d[3], "default": d[4], "pk": d[5]}
        return None
    finally:
        conn.close()


def rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def make_pre_004_db(path, tmp_root):
    """造一个"已到 003、并且**已经有一条会话行**"的库。

    刻意先塞一行会话数据：004 是给既有表加列，必须证明既有行不受影响。
    """
    run(path, backup=False, log=noop,
        versions_dir=versions_dir_up_to("003", tmp_root))
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO users (username, email, role, is_active) "
                     "VALUES ('u19', 'u19@x.y', 'user', 1)")
        conn.execute(
            "INSERT INTO interview_sessions "
            "(session_id, user_id, role, questions, question_status, user_answers, "
            " current_index, last_seq, last_reply, version, status, created_at, "
            " updated_at, expires_at, ended_reason) "
            "VALUES ('sess-pre', 1, '后端', '[\"q1\"]', '[\"answered\"]', "
            " '[\"答\"]', 1, 3, '上次回复', 2, 'active', '2026-09-29T00:00:00', "
            " '2026-09-29T00:10:00', '2026-09-29T02:00:00', NULL)")
        conn.commit()
    finally:
        conn.close()
    return path


class Migration004MetadataTests(unittest.TestCase):

    def setUp(self):
        self.migs = {m.revision: m for m in load_migrations(VERSIONS_DIR)}

    def test_is_wellformed(self):
        self.assertIn("004", self.migs)
        mig = self.migs["004"]
        self.assertEqual(mig.down_revision, "003")
        self.assertTrue(mig.description)

    def test_is_pure_add_column_no_rebuild(self):
        """004 必须是**纯加列**：不声明重建、语句里没有 DROP。

        这是它有别于 003 的关键性质 —— 加列在原表上原地完成，无需搬运数据。
        """
        mig = self.migs["004"]
        self.assertFalse(mig.allows_table_rebuild,
                         "004 只是加列，不该申请重建例外")
        for stmt in mig.upgrade:
            self.assertNotIn("DROP", stmt.upper())
            self.assertNotIn("DELETE", stmt.upper())

    def test_declares_verifications(self):
        self.assertGreaterEqual(len(self.migs["004"].verify), 6)


class Migration004EffectTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t19-004-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "pre004.db")
        make_pre_004_db(self.db, self.tmp)

        # 迁移前的快照（用于比对"其它表一个字节没动"）
        self.before = {
            t: columns(self.db, t)
            for t in ("users", "interview_records", "notifications",
                      "captcha_store", "auth_attempts", "token_blacklist")
        }
        self.before_session = rows(
            self.db,
            "SELECT session_id, questions, question_status, user_answers, "
            "current_index, last_seq, last_reply, version, status, created_at, "
            "updated_at, expires_at, ended_reason FROM interview_sessions")[0]

        run(self.db, backup=False, log=noop)

    def test_report_column_added(self):
        self.assertIn("report", columns(self.db, "interview_sessions"))
        self.assertEqual(len(columns(self.db, "interview_sessions")), 16)

    def test_report_is_nullable_without_default(self):
        """必须可为 NULL 且无默认值 —— 否则既有行会被塞进一个假报告。"""
        d = column_detail(self.db, "interview_sessions", "report")
        self.assertIsNotNone(d)
        self.assertEqual(d["notnull"], 0, "report 不该是 NOT NULL")
        self.assertIsNone(d["default"], "report 不该有默认值")

    def test_existing_session_row_untouched(self):
        """既有会话行必须逐列不变，只有新列为 NULL。"""
        after = rows(
            self.db,
            "SELECT session_id, questions, question_status, user_answers, "
            "current_index, last_seq, last_reply, version, status, created_at, "
            "updated_at, expires_at, ended_reason FROM interview_sessions")[0]
        self.assertEqual(after, self.before_session,
                         "004 改动了既有会话行 —— 加列不该这样")
        self.assertEqual(
            rows(self.db, "SELECT report FROM interview_sessions WHERE "
                          "session_id='sess-pre'")[0][0],
            None, "既有行的 report 应当是 NULL")

    def test_other_tables_untouched(self):
        for table, cols in self.before.items():
            self.assertEqual(columns(self.db, table), cols,
                             "004 改动了不该动的表 %s" % table)

    def test_indexes_and_foreign_key_survive(self):
        """`ALTER TABLE ADD COLUMN` 不该影响索引与外键。"""
        conn = sqlite3.connect(self.db)
        try:
            idx = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            fks = conn.execute(
                "PRAGMA foreign_key_list(interview_sessions)").fetchall()
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        self.assertIn("idx_sessions_active", idx)
        self.assertIn("idx_sessions_user", idx)
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0][2], "users")
        self.assertEqual(violations, [])
        self.assertEqual(integrity, "ok")

    def test_partial_unique_index_still_enforced(self):
        """004 之后部分唯一索引必须仍然生效（同用户第二个 active 被拒）。"""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO interview_sessions "
                    "(session_id, user_id, role, questions, question_status, "
                    " user_answers, current_index, status, created_at, updated_at, "
                    " expires_at) VALUES ('s2', 1, 'r', '[]', '[]', '[]', 0, "
                    " 'active', 't', 't', 't')")
        finally:
            conn.close()

    def test_finished_session_can_coexist(self):
        """终态不占锁：把既有会话置 finished 后可以再开一个。"""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("UPDATE interview_sessions SET status='finished' "
                         "WHERE session_id='sess-pre'")
            conn.execute(
                "INSERT INTO interview_sessions "
                "(session_id, user_id, role, questions, question_status, "
                " user_answers, current_index, status, created_at, updated_at, "
                " expires_at) VALUES ('s2', 1, 'r', '[]', '[]', '[]', 0, "
                " 'active', 't', 't', 't')")
            conn.commit()
            n = conn.execute("SELECT count(*) FROM interview_sessions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 2)

    def test_is_idempotent(self):
        self.assertEqual(run(self.db, backup=False, log=noop), 0)

    def test_declared_verifications_pass_on_result(self):
        mig = {m.revision: m for m in load_migrations(VERSIONS_DIR)}["004"]
        conn = sqlite3.connect(self.db)
        try:
            for sql, expected in mig.verify:
                actual = conn.execute(sql).fetchone()[0]
                self.assertEqual(actual, expected,
                                 "自检未通过：期望 %r 实得 %r\n  SQL: %s"
                                 % (expected, actual, " ".join(sql.split())))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
