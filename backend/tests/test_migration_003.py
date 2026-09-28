"""T-18 测试：迁移 003 —— 改动既有表结构。

003 是本项目**第一次改动既有表**（加列 / 重建表 / 删列），因此测试的重点
不是"结构对不对"（那个好验证），而是**数据有没有被悄悄弄坏**：

  * 重建表会 DROP 旧表再 RENAME 新表 —— 少了行、串了列，都会**静默成功**；
  * 因此这里逐行比对新旧数据内容，而不只是比行数；
  * 并验证唯一约束真的生效、外键子句在重建中存活、索引被重建回来。

所有用例都在临时库里跑，**不触碰 backend/interview.db**。
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

VERSIONS_DIR = os.path.join(BACKEND_DIR, "migrations", "versions")
MIG_003 = os.path.join(VERSIONS_DIR, "003_existing_tables.py")

USER_COLUMNS_AFTER = [
    "id", "username", "hashed_password", "created_at", "role", "email",
    "is_active", "nickname", "avatar", "bio", "gender", "birthday",
    "must_change_password",
]
RECORD_COLUMNS_AFTER = [
    "id", "user_id", "role", "messages", "report", "created_at", "status",
    "admin_comment", "client_token",
]
NOTIFICATION_COLUMNS_AFTER = [
    "id", "user_id", "type", "message", "target_type", "target_id", "is_read",
    "created_at",
]


def noop(*args, **kwargs):
    pass


def connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def columns(conn, table):
    return [d[1] for d in conn.execute("PRAGMA table_info([%s])" % table)]


def make_pre_003_db(path, tmp_root):
    """造一个"已到 002、且带真实业务数据"的库。

    做法：先用**只含 001/002 的临时版本目录**跑一遍，插入有代表性的数据，
    再换回完整版本目录 —— 这样 003 才会在有数据的表上执行。
    这正是它的真实使用场景：真库已有 3 个用户、5 条面试记录。
    """
    partial = os.path.join(tmp_root, "versions_pre003")
    os.makedirs(partial, exist_ok=True)
    for name in ("001_baseline.py", "002_sessions_and_stores.py"):
        shutil.copy(os.path.join(VERSIONS_DIR, name), os.path.join(partial, name))
    run(path, backup=False, versions_dir=partial, log=noop)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        # 全部用**绑定参数**，不手工拼 SQL —— 下面的测试数据里含单引号，
        # 手拼会把字符串字面量提前截断（首版就是这么写错的）。
        conn.executemany(
            "INSERT INTO users (username, email, role, is_active, nickname, "
            "avatar, bio, gender) VALUES (?,?,?,?,?,?,?,?)",
            [
                ("alice", "a@x.y", "user", 1, "爱丽丝", "a.jpg", "简介", "female"),
                ("bob", "b@x.y", "user", 1, None, None, None, None),
                ("admin", "adm@x.y", "admin", 1, None, None, None, None),
            ],
        )
        # 含中文、单引号、JSON、NULL 的值 —— 重建表最容易在这些地方出错
        conn.executemany(
            "INSERT INTO interview_records (user_id, role, messages, report, "
            "status, admin_comment) VALUES (?,?,?,?,?,?)",
            [
                (1, "后端", '[{"role":"user","content":"你好，\'单引号\' 与 \\"双引号\\""}]',
                 '{"score":8}', "approved", "不错"),
                (1, "前端", "[]", None, "pending", None),
                (2, "数据", None, None, "rejected", None),
            ],
        )
        conn.executemany(
            "INSERT INTO notifications (user_id, type, message, target_type, "
            "target_id, is_read, link_url) VALUES (?,?,?,?,?,?,?)",
            [
                (1, "system", "欢迎", "interview_record", 1, 0, "/old/link"),
                (2, "new_comment", "有人评论了你的面试", None, None, 1, None),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return path


class Migration003MetadataTests(unittest.TestCase):

    def setUp(self):
        self.migs = {m.revision: m for m in load_migrations(VERSIONS_DIR)}

    def test_is_wellformed(self):
        self.assertIn("003", self.migs)
        mig = self.migs["003"]
        self.assertEqual(mig.down_revision, "002")
        self.assertTrue(mig.description)

    def test_declares_rebuild_with_reason(self):
        mig = self.migs["003"]
        self.assertTrue(mig.allows_table_rebuild, "003 需要重建表，必须声明")
        self.assertTrue(mig.rebuild_reason.strip(), "重建必须写明理由")

    def test_has_declarative_verifications(self):
        mig = self.migs["003"]
        self.assertGreaterEqual(
            len(mig.verify), 10,
            "003 是结构变更，应有足够多的声明式自检；当前 %d 项" % len(mig.verify),
        )
        for sql, expected in mig.verify:
            self.assertIsInstance(sql, str)
            self.assertTrue(sql.strip().upper().startswith("SELECT"))

    def test_creates_no_new_scratch_table_leftover_check_present(self):
        """必须有一条"不得残留 _new 临时表"的自检。"""
        blob = " ".join(sql for sql, _ in self.migs["003"].verify).lower()
        self.assertIn("_new", blob, "缺少'不得残留 _new 表'的自检")


class Migration003EffectTests(unittest.TestCase):
    """在带数据的库上实际执行 003，验证结构变更与数据保全。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t18-003-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "pre003.db")
        make_pre_003_db(self.db, self.tmp)

        # 迁移前的数据快照（用于逐行比对）
        conn = sqlite3.connect(self.db)
        try:
            self.before_users = conn.execute(
                "SELECT id, username, hashed_password, created_at, role, email, "
                "is_active, nickname, avatar, bio, gender, birthday "
                "FROM users ORDER BY id").fetchall()
            self.before_records = conn.execute(
                "SELECT id, user_id, role, messages, report, created_at, status, "
                "admin_comment FROM interview_records ORDER BY id").fetchall()
            self.before_notifications = conn.execute(
                "SELECT id, user_id, type, message, target_type, target_id, "
                "is_read, created_at FROM notifications ORDER BY id").fetchall()
        finally:
            conn.close()

        run(self.db, backup=False, log=noop)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()

    # ---------- 结构 ----------

    def test_users_gains_column(self):
        self.assertEqual(columns(self.conn, "users"), USER_COLUMNS_AFTER)

    def test_interview_records_gains_column(self):
        self.assertEqual(columns(self.conn, "interview_records"), RECORD_COLUMNS_AFTER)

    def test_notifications_loses_dead_column(self):
        self.assertEqual(columns(self.conn, "notifications"), NOTIFICATION_COLUMNS_AFTER)
        self.assertNotIn("link_url", columns(self.conn, "notifications"))

    def test_no_scratch_tables_left(self):
        left = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_new'")]
        self.assertEqual(left, [], "重建后残留了临时表：%s" % left)

    def test_indexes_are_rebuilt(self):
        """DROP TABLE 会连带删掉索引 —— 重建后必须补回来，否则是静默的性能回归。"""
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for idx in ("ix_interview_records_id", "ix_notifications_id",
                    "ix_users_id", "ix_users_username", "ix_users_email"):
            self.assertIn(idx, names, "索引 %s 在迁移后消失了" % idx)

    def test_foreign_keys_survive_rebuild(self):
        for table in ("interview_records", "notifications"):
            fks = self.conn.execute("PRAGMA foreign_key_list([%s])" % table).fetchall()
            self.assertEqual(len(fks), 1, "表 %s 的外键在重建中丢失" % table)
            self.assertEqual(fks[0][2], "users")
            self.assertEqual(fks[0][3], "user_id")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    # ---------- 数据（最关键） ----------

    def test_users_data_preserved_row_for_row(self):
        after = self.conn.execute(
            "SELECT id, username, hashed_password, created_at, role, email, "
            "is_active, nickname, avatar, bio, gender, birthday "
            "FROM users ORDER BY id").fetchall()
        self.assertEqual(after, self.before_users, "users 的数据内容发生了变化")

    def test_interview_records_data_preserved_row_for_row(self):
        """逐行比对，不只比行数 —— 重建时列错位是"行数对但内容错"的典型。"""
        after = self.conn.execute(
            "SELECT id, user_id, role, messages, report, created_at, status, "
            "admin_comment FROM interview_records ORDER BY id").fetchall()
        self.assertEqual(
            after, self.before_records,
            "interview_records 的数据内容发生了变化（列错位或丢行）",
        )

    def test_notifications_data_preserved_row_for_row(self):
        after = self.conn.execute(
            "SELECT id, user_id, type, message, target_type, target_id, "
            "is_read, created_at FROM notifications ORDER BY id").fetchall()
        self.assertEqual(after, self.before_notifications,
                         "notifications 的数据内容发生了变化")

    def test_null_and_unicode_values_survive(self):
        """NULL、中文、单引号、JSON 必须原样保留。"""
        row = self.conn.execute(
            "SELECT messages, report FROM interview_records WHERE id=1").fetchone()
        self.assertIn("单引号", row[0])
        self.assertEqual(row[1], '{"score":8}')
        self.assertIsNone(self.conn.execute(
            "SELECT report FROM interview_records WHERE id=2").fetchone()[0])
        self.assertEqual(self.conn.execute(
            "SELECT nickname FROM users WHERE id=1").fetchone()[0], "爱丽丝")

    def test_existing_rows_get_zero_not_null(self):
        """ADR-017：既有用户的 must_change_password 必须是 0（不是 NULL）。"""
        nulls = self.conn.execute(
            "SELECT count(*) FROM users WHERE must_change_password IS NULL").fetchone()[0]
        self.assertEqual(nulls, 0, "既有行被置成了 NULL")
        values = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT must_change_password FROM users")}
        self.assertEqual(values, {0})

    def test_row_counts_unchanged(self):
        self.assertEqual(self.conn.execute("SELECT count(*) FROM users").fetchone()[0],
                         len(self.before_users))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM interview_records").fetchone()[0],
            len(self.before_records))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM notifications").fetchone()[0],
            len(self.before_notifications))

    # ---------- client_token 唯一约束 ----------

    def test_client_token_starts_null_for_existing_rows(self):
        nulls = self.conn.execute(
            "SELECT count(*) FROM interview_records WHERE client_token IS NULL"
        ).fetchone()[0]
        self.assertEqual(nulls, len(self.before_records))

    def test_multiple_nulls_are_allowed(self):
        """唯一约束必须允许多个 NULL —— 否则既有数据会直接违约。"""
        self.conn.execute(
            "INSERT INTO interview_records (user_id, role, status) VALUES (1,'r','pending')")
        self.conn.execute(
            "INSERT INTO interview_records (user_id, role, status) VALUES (1,'r','pending')")
        n = self.conn.execute("SELECT count(*) FROM interview_records "
                              "WHERE client_token IS NULL").fetchone()[0]
        self.assertEqual(n, len(self.before_records) + 2)

    def test_duplicate_client_token_is_rejected(self):
        self.conn.execute("UPDATE interview_records SET client_token='tok-1' WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE interview_records SET client_token='tok-1' WHERE id=2")

    def test_unique_constraint_is_a_real_index(self):
        """约束应是**索引级**的（幂等键要靠它做 O(log n) 去重）。"""
        found = self.conn.execute(
            "SELECT count(*) FROM pragma_index_list('interview_records') il "
            "JOIN pragma_index_info(il.name) ii "
            "WHERE il.[unique]=1 AND ii.name='client_token'").fetchone()[0]
        self.assertEqual(found, 1, "没有唯一索引覆盖 client_token")

    # ---------- 迁移语义 ----------

    def test_is_idempotent(self):
        applied = run(self.db, backup=False, log=noop)
        self.assertEqual(applied, 0, "003 已应用，重复运行不应再做任何事")

    def test_002_tables_untouched(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("interview_sessions", "captcha_store", "auth_attempts",
                  "token_blacklist"):
            self.assertIn(t, names, "003 不该动 002 建的表，但 %s 不见了" % t)
        self.assertEqual(columns(self.conn, "interview_sessions")[-1], "ended_reason")

    def test_declared_verifications_all_pass(self):
        """把 003 自己声明的自检 SQL 在迁移后的库上再跑一遍。

        runner 已强制过一遍；这里独立复算，确保"自检本身"没有被写成恒真。
        """
        mig = {m.revision: m for m in load_migrations(VERSIONS_DIR)}["003"]
        for sql, expected in mig.verify:
            actual = self.conn.execute(sql).fetchone()[0]
            self.assertEqual(actual, expected,
                             "自检未通过：期望 %r 实得 %r\n  SQL: %s"
                             % (expected, actual, " ".join(sql.split())))


if __name__ == "__main__":
    unittest.main()
