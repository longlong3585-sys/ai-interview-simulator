"""T-17 测试：迁移 002 —— 四张新表的结构与语义。

本文件建**自己的临时库**（不触碰 backend/interview.db），跑完整的迁移链
（001 + 002），然后验证：

1. 四张表的列 / 约束与架构 §6.2 + §4 一致；
2. **跨层一致性**：`interview_sessions` 的列必须恰好等于 T-16 的
   `SessionSnapshot` 字段集合（协议的"图纸"与 DDL 的"实物"对得上）；
3. 部分唯一索引的真实语义（同用户第二个 active 被拒；终态可并存）；
4. `ON DELETE CASCADE` 真的级联（**必须显式开 foreign_keys** —— 原始
   sqlite3 连接默认是关的，这是 T-09/T-15 都踩过的坑）；
5. 002 **不触碰**任何既有表；
6. DDL 级幂等。
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

from dataclasses import fields  # noqa: E402

from migrations.runner import load_migrations, run  # noqa: E402
from services.stores.base import SessionSnapshot  # noqa: E402
from tests.support import versions_dir_up_to  # noqa: E402

VERSIONS_DIR = os.path.join(BACKEND_DIR, "migrations", "versions")
MIG_002 = os.path.join(VERSIONS_DIR, "002_sessions_and_stores.py")

EXPECTED_COLUMNS = {
    "interview_sessions": ["session_id", "user_id", "role", "questions",
                           "question_status", "user_answers", "current_index",
                           "last_seq", "last_reply", "version", "status",
                           "created_at", "updated_at", "expires_at",
                           "ended_reason"],
    "captcha_store": ["captcha_id", "code", "expires_at", "used"],
    "auth_attempts": ["id", "ip", "attempted_at"],
    "token_blacklist": ["jti", "expires_at"],
}

EXPECTED_INDEXES = ["idx_sessions_user", "idx_sessions_active",
                    "idx_captcha_expires", "idx_attempts_ip_time",
                    "idx_blacklist_expires"]

LEGACY_TABLES = ["users", "interview_records", "notifications"]


def noop(*args, **kwargs):
    pass


def connect(path, foreign_keys=False):
    """打开一条连接。**显式**控制 foreign_keys —— 原始 sqlite3 默认是关的。"""
    conn = sqlite3.connect(path)
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def column_names(conn, table):
    return [d[1] for d in conn.execute("PRAGMA table_info(%s)" % table)]


def table_names(conn):
    return sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )


def index_names(conn):
    return sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    )


def add_user(conn, username="u1", email="u1@x.y"):
    conn.execute(
        "INSERT INTO users (username, email, role, is_active) VALUES (?, ?, 'user', 1)",
        (username, email),
    )
    return conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]


def add_session(conn, session_id, user_id, status="active",
                created="2026-09-29T00:00:00", expires="2026-09-29T02:00:00"):
    conn.execute(
        "INSERT INTO interview_sessions "
        "(session_id, user_id, role, questions, question_status, user_answers,"
        " current_index, created_at, updated_at, expires_at, status) "
        "VALUES (?, ?, 'backend', '[]', '[]', '[]', 0, ?, ?, ?, ?)",
        (session_id, user_id, created, created, expires, status),
    )


class Migration002MetadataTests(unittest.TestCase):

    def test_migration_is_wellformed(self):
        migs = {m.revision: m for m in load_migrations(VERSIONS_DIR)}
        self.assertIn("002", migs, "002 迁移未被加载")
        mig = migs["002"]
        self.assertEqual(mig.down_revision, "001")
        self.assertTrue(mig.description)
        self.assertTrue(mig.upgrade, "002 不能是空迁移")

    def test_statements_are_all_create_if_not_exists(self):
        mig = {m.revision: m for m in load_migrations(VERSIONS_DIR)}["002"]
        creates = 0
        for stmt in mig.upgrade:
            head = stmt.strip().upper()
            self.assertTrue(head.startswith("CREATE"), "非 CREATE 语句：%s" % head[:60])
            self.assertIn("IF NOT EXISTS", head, "缺 IF NOT EXISTS：%s" % head[:60])
            creates += 1
        self.assertEqual(creates, len(mig.upgrade))


class SchemaShapeTests(unittest.TestCase):
    """只跑到 002：断言的是**002 建出来的形状**。

    刻意用 `versions_dir_up_to("002")` 而不是完整链条 —— 后续迁移（003/004）
    会合法地改动既有表，跑完整链会让本组用例把"后续版本的既定改动"
    误判成"002 建错了"。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t17-shape-")
        self.db = os.path.join(self.tmp, "fresh.db")
        run(self.db, backup=False, log=noop,
            versions_dir=versions_dir_up_to("002", self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_four_tables_created(self):
        conn = connect(self.db)
        try:
            present = set(table_names(conn))
        finally:
            conn.close()
        for table in EXPECTED_COLUMNS:
            self.assertIn(table, present, "缺少表 %s" % table)

    def test_columns_match_specification(self):
        conn = connect(self.db)
        try:
            for table, expected in EXPECTED_COLUMNS.items():
                self.assertEqual(
                    column_names(conn, table), expected,
                    "表 %s 的列与架构声明不一致" % table,
                )
        finally:
            conn.close()

    def test_session_columns_are_002_scope(self):
        """本用例只断言 002 建出来的列。

        「协议 ↔ DDL 一一对应」那条**跨层断言**已移到 `test_migrations.py`
        （跑完整链条再比对）—— 因为协议对应的是**最终**结构，
        而本文件刻意只跑到 002。放在这里会在每次新增迁移时误报。
        """
        conn = connect(self.db)
        try:
            self.assertEqual(column_names(conn, "interview_sessions"),
                             EXPECTED_COLUMNS["interview_sessions"])
        finally:
            conn.close()

    def test_all_indexes_created(self):
        conn = connect(self.db)
        try:
            present = set(index_names(conn))
        finally:
            conn.close()
        for idx in EXPECTED_INDEXES:
            self.assertIn(idx, present, "缺少索引 %s" % idx)

    def test_active_index_is_unique_and_partial(self):
        """`idx_sessions_active` 必须是**部分**唯一索引。

        若它退化成普通唯一索引（丢掉 WHERE），同一用户连开两次面试会被拒 ——
        即"用户被永久锁死"，正是 ADR-022R 要消除的风险。
        """
        conn = connect(self.db)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_sessions_active'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "idx_sessions_active 不存在")
        sql = (row[0] or "").upper()
        self.assertIn("UNIQUE", sql, "idx_sessions_active 不是唯一索引")
        self.assertIn("WHERE", sql, "idx_sessions_active 不是部分索引（缺 WHERE）")
        self.assertIn("STATUS = 'ACTIVE'", sql.replace('"', "'"))

    def test_defaults_and_not_null(self):
        conn = connect(self.db)
        try:
            uid = add_user(conn)
            # 只给 NOT NULL 且无默认值的列提供值，其余走默认
            add_session(conn, "s-defaults", uid)
            row = conn.execute(
                "SELECT current_index, last_seq, version, status, last_reply,"
                " ended_reason FROM interview_sessions WHERE session_id='s-defaults'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (0, 0, 0, "active", None, None))

    def test_status_default_from_ddl_is_active(self):
        """**不显式给 status** 时必须落到 `'active'`。

        这条用例是破坏性验证补出来的：原先所有插入辅助函数都显式传了
        `status='active'`，于是 DDL 的 `DEFAULT 'active'` **根本没被执行到** ——
        把它改成 `'finished'` 时全部测试依然通过。

        为什么这个默认值必须钉死：`interview_sessions` 的插入若落到
        `finished`，该行就**不占用** `UNIQUE(user_id) WHERE status='active'`
        的部分唯一索引 —— 用户可以在已有"完成"会话的同时反复开新会话，
        部分唯一索引的并发保护**静默失效**。
        """
        conn = connect(self.db)
        try:
            uid = add_user(conn)
            conn.execute(
                "INSERT INTO interview_sessions "
                "(session_id, user_id, role, questions, question_status,"
                " user_answers, created_at, updated_at, expires_at) "
                "VALUES ('s-nostatus', ?, 'backend', '[]', '[]', '[]',"
                " '2026-09-29T00:00:00', '2026-09-29T00:00:00',"
                " '2026-09-29T02:00:00')",
                (uid,),
            )
            effective = conn.execute(
                "SELECT status FROM interview_sessions WHERE session_id='s-nostatus'"
            ).fetchone()[0]

            declared = dict(
                (d[1], d[4]) for d in conn.execute("PRAGMA table_info(interview_sessions)")
            )
        finally:
            conn.close()

        self.assertEqual(effective, "active", "不传 status 时未落到 active")
        self.assertEqual(
            declared.get("status"), "'active'",
            "DDL 声明的 status 默认值不是 'active'：%r" % declared.get("status"),
        )

    def test_numeric_defaults_declared_in_ddl(self):
        """`current_index` / `last_seq` / `version` 的 DDL 默认值必须是 0。

        与上一条同理：不能只靠"插入时省略所以看起来是 0"，
        要把 PRAGMA 里声明的默认值也钉住。
        """
        conn = connect(self.db)
        try:
            declared = dict(
                (d[1], d[4]) for d in conn.execute("PRAGMA table_info(interview_sessions)")
            )
        finally:
            conn.close()
        for col in ("current_index", "last_seq", "version"):
            self.assertEqual(declared.get(col), "0",
                             "%s 的 DDL 默认值应为 0，实为 %r" % (col, declared.get(col)))

    def test_captcha_used_defaults_to_zero(self):
        """新建验证码默认未使用 —— 若默认成 1，所有验证码会立刻失效。"""
        conn = connect(self.db)
        try:
            conn.execute("INSERT INTO captcha_store (captcha_id, code, expires_at)"
                         " VALUES ('c-def', '1234', '2026-09-29T00:05:00')")
            used = conn.execute(
                "SELECT used FROM captcha_store WHERE captcha_id='c-def'"
            ).fetchone()[0]
            declared = dict(
                (d[1], d[4]) for d in conn.execute("PRAGMA table_info(captcha_store)")
            )
        finally:
            conn.close()
        self.assertEqual(used, 0)
        self.assertEqual(declared.get("used"), "0",
                         "captcha_store.used 的 DDL 默认值应为 0，实为 %r"
                         % declared.get("used"))

    def test_not_null_is_enforced(self):
        conn = connect(self.db)
        try:
            uid = add_user(conn)
            # 故意漏掉 NOT NULL 且无默认值的 role
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO interview_sessions (session_id, user_id,"
                    " questions, question_status, user_answers, created_at,"
                    " updated_at, expires_at) VALUES ('s-bad', ?, '[]', '[]',"
                    " '[]', 'x', 'x', 'x')",
                    (uid,),
                )
        finally:
            conn.close()

    def test_primary_keys_are_enforced(self):
        conn = connect(self.db)
        try:
            conn.execute("INSERT INTO captcha_store (captcha_id, code, expires_at)"
                         " VALUES ('c1', '1234', '2026-09-29T00:05:00')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO captcha_store (captcha_id, code, expires_at)"
                             " VALUES ('c1', '9999', '2026-09-29T00:05:00')")
            conn.execute("INSERT INTO token_blacklist (jti, expires_at)"
                         " VALUES ('j1', '2026-09-29T08:00:00')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO token_blacklist (jti, expires_at)"
                             " VALUES ('j1', '2026-09-29T09:00:00')")
        finally:
            conn.close()

    def test_auth_attempts_autoincrements(self):
        conn = connect(self.db)
        try:
            conn.execute("INSERT INTO auth_attempts (ip, attempted_at) VALUES ('1.1.1.1','t')")
            conn.execute("INSERT INTO auth_attempts (ip, attempted_at) VALUES ('1.1.1.1','t')")
            ids = [r[0] for r in conn.execute("SELECT id FROM auth_attempts ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(ids, [1, 2])


class PartialUniqueIndexSemanticsTests(unittest.TestCase):
    """`UNIQUE(user_id) WHERE status='active'` 的真实语义（ADR-023）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t17-unique-")
        self.db = os.path.join(self.tmp, "u.db")
        run(self.db, backup=False, log=noop)
        self.conn = connect(self.db)
        self.uid = add_user(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_active_for_same_user_is_rejected(self):
        """同用户第二个 active 必须被拒 —— 这是把"静默覆盖"改成"显式 409"的关键。"""
        add_session(self.conn, "s1", self.uid)
        with self.assertRaises(sqlite3.IntegrityError):
            add_session(self.conn, "s2", self.uid)

    def test_finished_releases_the_lock(self):
        add_session(self.conn, "s1", self.uid)
        self.conn.execute("UPDATE interview_sessions SET status='finished' WHERE session_id='s1'")
        add_session(self.conn, "s2", self.uid)  # 不应抛
        rows = self.conn.execute(
            "SELECT status, count(*) FROM interview_sessions GROUP BY status"
        ).fetchall()
        self.assertEqual(dict(rows), {"finished": 1, "active": 1})

    def test_abandoned_releases_the_lock(self):
        """超时/放弃置 abandoned 后用户**立刻**能重开（ADR-022R 的核心承诺）。"""
        add_session(self.conn, "s1", self.uid)
        self.conn.execute("UPDATE interview_sessions SET status='abandoned' WHERE session_id='s1'")
        add_session(self.conn, "s2", self.uid)
        active = self.conn.execute(
            "SELECT count(*) FROM interview_sessions WHERE status='active'"
        ).fetchone()[0]
        self.assertEqual(active, 1)

    def test_many_terminal_sessions_can_coexist(self):
        """终态行不占锁，可无限并存 —— 否则用户的历史会话会被索引挡住。"""
        for i in range(5):
            add_session(self.conn, "f%d" % i, self.uid, status="finished")
        for i in range(5):
            add_session(self.conn, "a%d" % i, self.uid, status="abandoned")
        total = self.conn.execute("SELECT count(*) FROM interview_sessions").fetchone()[0]
        self.assertEqual(total, 10)

    def test_different_users_do_not_block_each_other(self):
        other = add_user(self.conn, username="u2", email="u2@x.y")
        add_session(self.conn, "s1", self.uid)
        add_session(self.conn, "s2", other)  # 不应抛

    def test_any_terminal_but_active_mix_is_fine(self):
        add_session(self.conn, "s1", self.uid, status="finished")
        add_session(self.conn, "s2", self.uid, status="abandoned")
        add_session(self.conn, "s3", self.uid, status="active")
        with self.assertRaises(sqlite3.IntegrityError):
            add_session(self.conn, "s4", self.uid, status="active")


class ForeignKeyCascadeTests(unittest.TestCase):
    """`ON DELETE CASCADE` 真的级联 —— 但**前提是 foreign_keys 被打开**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t17-fk-")
        self.db = os.path.join(self.tmp, "fk.db")
        run(self.db, backup=False, log=noop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_raw_connection_has_foreign_keys_off_by_default(self):
        """先把这个坑显式记录下来：**原始 sqlite3 连接默认不开外键**。

        T-15 已让应用的每条连接执行 `PRAGMA foreign_keys=ON`，
        但测试/运维直接用 sqlite3 时不会 —— 于是"CASCADE 不生效"
        会被误判成"外键写错了"。所以下面的级联用例必须显式开启。
        """
        conn = connect(self.db)
        try:
            self.assertEqual(
                conn.execute("PRAGMA foreign_keys").fetchone()[0], 0,
                "前置条件变了：原始连接默认开启了外键，本组用例的说明需更新",
            )
        finally:
            conn.close()

    def test_deleting_user_cascades_to_sessions(self):
        conn = connect(self.db, foreign_keys=True)
        try:
            uid = add_user(conn)
            add_session(conn, "s1", uid, status="finished")
            add_session(conn, "s2", uid, status="abandoned")
            self.assertEqual(
                conn.execute("SELECT count(*) FROM interview_sessions").fetchone()[0], 2
            )
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            left = conn.execute("SELECT count(*) FROM interview_sessions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(left, 0, "删除用户后会话未级联删除")

    def test_session_with_nonexistent_user_is_rejected(self):
        """`user_id` 是 NOT NULL + 外键：孤儿会话必须插不进去。"""
        conn = connect(self.db, foreign_keys=True)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                add_session(conn, "orphan", 999999)
        finally:
            conn.close()


class DoesNotTouchExistingTablesTests(unittest.TestCase):
    """002 只新增对象，不得改动既有表的结构或数据。

    为了**只**检验 002 的行为，这里用一个"只含 001 + 002"的版本目录，
    而不是完整链。否则 T-18 的 003 会合法地改这些表，
    本组用例就会把"003 的既定改动"误判成"002 越界" ——
    首版就是这样：003 一落地，`test_legacy_tables_gain_no_columns` 立刻失败。
    按迁移**分别**验证各自的影响范围，才不会互相干扰。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t17-existing-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.versions = os.path.join(self.tmp, "versions_001_002")
        os.makedirs(self.versions)
        for name in ("001_baseline.py", "002_sessions_and_stores.py"):
            shutil.copy(os.path.join(VERSIONS_DIR, name),
                        os.path.join(self.versions, name))

        self.db = os.path.join(self.tmp, "e.db")
        run(self.db, backup=False, versions_dir=self.versions, log=noop)
        self.conn = connect(self.db)
        self.uid = add_user(self.conn)
        self.conn.execute(
            "INSERT INTO interview_records (user_id, role, status) VALUES (?, 'r', 'pending')",
            (self.uid,),
        )
        self.conn.execute(
            "INSERT INTO notifications (user_id, type, message) VALUES (?, 'system', 'hi')",
            (self.uid,),
        )
        self.conn.commit()
        self.before = {t: column_names(self.conn, t) for t in LEGACY_TABLES}
        self.before_counts = {
            t: self.conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            for t in LEGACY_TABLES
        }

    def tearDown(self):
        self.conn.close()

    def test_reexecuting_002_is_a_noop(self):
        """DDL 级幂等：把 002 的语句再执行一遍，不得报错、不得改动既有数据。"""
        mig = {m.revision: m for m in load_migrations(VERSIONS_DIR)}["002"]
        for stmt in mig.upgrade:
            self.conn.execute(stmt)

        for table, cols in self.before.items():
            self.assertEqual(column_names(self.conn, table), cols,
                             "重复执行 002 改动了既有表 %s" % table)
        after_counts = {
            t: self.conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            for t in LEGACY_TABLES
        }
        self.assertEqual(after_counts, self.before_counts)

    def test_legacy_tables_gain_no_columns(self):
        """002 不得动既有表的列 —— 加列/删列是 003（T-18）的工作。

        003 落地后，完整链的最终结构**当然**含 `must_change_password`
        且不含 `link_url`；本用例检的是**002 单独的**影响范围，
        因此必须只看 001+002 的结果。
        """
        self.assertNotIn("client_token", column_names(self.conn, "users"))
        self.assertNotIn("must_change_password", column_names(self.conn, "users"))
        self.assertNotIn("client_token", column_names(self.conn, "interview_records"))
        # link_url 也还在（003 才删）
        self.assertIn("link_url", column_names(self.conn, "notifications"))


if __name__ == "__main__":
    unittest.main()
