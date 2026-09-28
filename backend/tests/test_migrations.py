"""T-14 测试：轻量迁移框架。

重点验证三件事（按重要性排序）：
  1. **既有库绝不被改动**：legacy 分支只写版本记录，表结构与数据行数逐表比对不变
  2. **空库能建出正确结构**：与迁移脚本声明的表/列/索引一致
  3. **幂等与安全**：重复运行无副作用；只读接口真的只读；异常结构拒绝自动处理

所有用例都在临时目录里造库，**不触碰 backend/interview.db**。
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from migrations.runner import (  # noqa: E402
    BASELINE_REVISION,
    MigrationError,
    applied_revisions,
    detect_state,
    load_migrations,
    run,
    status,
)

VERSIONS_DIR = os.path.join(BACKEND_DIR, "migrations", "versions")

# 与 001_baseline 声明一致
EXPECTED = {
    "users": ["id", "username", "hashed_password", "created_at", "role", "email",
              "is_active", "nickname", "avatar", "bio", "gender", "birthday"],
    "interview_records": ["id", "user_id", "role", "messages", "report",
                          "created_at", "status", "admin_comment"],
    "notifications": ["id", "user_id", "type", "message", "target_type",
                      "target_id", "is_read", "created_at", "link_url"],
}


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
    finally:
        conn.close()


def _columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return [d[1] for d in conn.execute("PRAGMA table_info(%s)" % table)]
    finally:
        conn.close()


def _counts(path, tables):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in tables}
    finally:
        conn.close()


def _make_legacy_db(path):
    """造一个"既有库"：结构与数据都模仿线上，但**没有 schema_migrations 表**。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER NOT NULL, username VARCHAR, hashed_password VARCHAR,
                created_at DATETIME, role VARCHAR, email VARCHAR NOT NULL,
                is_active BOOLEAN, nickname VARCHAR, avatar VARCHAR, bio TEXT,
                gender VARCHAR, birthday DATE, PRIMARY KEY (id));
            CREATE UNIQUE INDEX ix_users_username ON users (username);
            CREATE UNIQUE INDEX ix_users_email ON users (email);
            CREATE TABLE interview_records (
                id INTEGER NOT NULL, user_id INTEGER, role VARCHAR, messages TEXT,
                report TEXT, created_at DATETIME, status VARCHAR, admin_comment TEXT,
                PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES users (id));
            CREATE TABLE notifications (
                id INTEGER NOT NULL, user_id INTEGER, type VARCHAR, message TEXT,
                target_type VARCHAR, target_id INTEGER, is_read BOOLEAN,
                created_at DATETIME, link_url VARCHAR,
                PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES users (id));
        """)
        conn.execute("INSERT INTO users (username, email, role, is_active) VALUES ('u1','a@b.c','user',1)")
        conn.execute("INSERT INTO users (username, email, role, is_active) VALUES ('admin','x@y.z','admin',1)")
        for i in range(3):
            conn.execute("INSERT INTO interview_records (user_id, role, status) VALUES (1,'后端','pending')")
        conn.execute("INSERT INTO notifications (user_id, type, message) VALUES (1,'system','hi')")
        conn.commit()
    finally:
        conn.close()


class LoadMigrationsTests(unittest.TestCase):

    def test_baseline_is_first_and_wellformed(self):
        migs = load_migrations(VERSIONS_DIR)
        self.assertGreaterEqual(len(migs), 1)
        self.assertEqual(migs[0].revision, BASELINE_REVISION)
        self.assertIsNone(migs[0].down_revision)
        self.assertTrue(migs[0].description)

    def test_baseline_contains_no_destructive_sql(self):
        """安全闸 4：基线脚本里不得出现 DROP / DELETE / UPDATE / TRUNCATE。"""
        migs = load_migrations(VERSIONS_DIR)
        baseline = migs[0]
        blob = "\n".join(baseline.upgrade).upper()
        for bad in ("DROP ", "DELETE ", "UPDATE ", "TRUNCATE"):
            self.assertNotIn(bad, blob, "基线迁移含有破坏性语句：%s" % bad)


class EmptyDatabaseTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t14-empty-")
        self.db = os.path.join(self.tmp, "fresh.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_full_schema(self):
        run(self.db, backup=False, log=lambda *a, **k: None)
        # 业务表 + 迁移框架自己的版本表
        self.assertEqual(_tables(self.db), sorted(list(EXPECTED) + ["schema_migrations"]))
        for table, cols in EXPECTED.items():
            self.assertEqual(_columns(self.db, table), cols, "表 %s 列不符" % table)

    def test_records_baseline_revision(self):
        run(self.db, backup=False, log=lambda *a, **k: None)
        conn = sqlite3.connect(self.db)
        try:
            revs = applied_revisions(conn)
        finally:
            conn.close()
        self.assertEqual(revs, [BASELINE_REVISION])

    def test_is_idempotent(self):
        first = run(self.db, backup=False, log=lambda *a, **k: None)
        self.assertEqual(first, 1)
        before = _sha256(self.db)
        second = run(self.db, backup=False, log=lambda *a, **k: None)
        self.assertEqual(second, 0, "重复运行不应再应用任何迁移")
        self.assertEqual(_sha256(self.db), before, "重复运行不应改动数据库")

    def test_state_becomes_managed(self):
        run(self.db, backup=False, log=lambda *a, **k: None)
        conn = sqlite3.connect(self.db)
        try:
            state, _ = detect_state(conn, load_migrations(VERSIONS_DIR))
        finally:
            conn.close()
        self.assertEqual(state, "managed")


class LegacyDatabaseTests(unittest.TestCase):
    """最关键的一组：既有库必须**零改动**地被接管。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t14-legacy-")
        self.db = os.path.join(self.tmp, "legacy.db")
        _make_legacy_db(self.db)
        self.before_counts = _counts(self.db, EXPECTED)
        self.before_cols = {t: _columns(self.db, t) for t in EXPECTED}
        self.before_hash = _sha256(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detected_as_legacy(self):
        conn = sqlite3.connect(self.db)
        try:
            state, detail = detect_state(conn, load_migrations(VERSIONS_DIR))
        finally:
            conn.close()
        self.assertEqual(state, "legacy")

    def test_dry_run_changes_nothing(self):
        run(self.db, backup=False, dry_run=True, log=lambda *a, **k: None)
        self.assertEqual(_sha256(self.db), self.before_hash, "dry-run 改动了数据库")

    def test_stamp_only_no_ddl_no_data_loss(self):
        """核心断言：只记录版本，表结构不变、数据一行不丢。"""
        run(self.db, backup=False, log=lambda *a, **k: None)

        self.assertEqual(_sha256(self.db) != self.before_hash, True,
                         "应当只多了版本表（文件必然变化），此处用于确认确实执行了 stamp")

        # 表集合 = 原有 3 张 + schema_migrations
        self.assertEqual(sorted(_tables(self.db)),
                         sorted(list(EXPECTED) + ["schema_migrations"]))

        # 每个业务表的列完全不变（link_url 等原样保留）
        for table, cols in self.before_cols.items():
            self.assertEqual(_columns(self.db, table), cols,
                             "既有库的表 %s 结构被改动了" % table)

        # 数据一行不丢
        self.assertEqual(_counts(self.db, EXPECTED), self.before_counts,
                         "既有库的数据行数发生变化 —— 可能丢数据！")

        # 版本记录已写入
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(applied_revisions(conn), [BASELINE_REVISION])
        finally:
            conn.close()

    def test_running_twice_is_safe(self):
        run(self.db, backup=False, log=lambda *a, **k: None)
        after_first = _sha256(self.db)
        run(self.db, backup=False, log=lambda *a, **k: None)
        self.assertEqual(_sha256(self.db), after_first, "第二次运行改动了数据库")
        self.assertEqual(_counts(self.db, EXPECTED), self.before_counts)


class SafetyTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t14-safety-")
        self.db = os.path.join(self.tmp, "x.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_is_truly_readonly(self):
        """status() 只读：即便库不存在版本表，也不得尝试写入。"""
        _make_legacy_db(self.db)
        before = _sha256(self.db)
        info = status(self.db, VERSIONS_DIR)
        self.assertEqual(info["state"], "legacy")
        self.assertEqual(_sha256(self.db), before, "status() 改动了数据库")

    def test_partial_schema_is_refused(self):
        """只有部分业务表时拒绝自动处理，避免把残缺库当成空库重建。"""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(MigrationError) as ctx:
            run(self.db, backup=False, log=lambda *a, **k: None)
        self.assertIn("结构不完整", str(ctx.exception))

    def test_status_on_missing_file(self):
        info = status(os.path.join(self.tmp, "nope.db"), VERSIONS_DIR)
        self.assertEqual(info["state"], "missing")

    def test_failed_migration_rolls_back(self):
        """单个迁移失败时**整体回滚**：连已成功的 001 也一并撤销，不留任何中间态。

        断言为何是"表集合为空"而不是"good_one 不存在"：
        破坏性验证发现，后者在"去掉显式事务"时**也能通过** —— 因为连接关闭时的
        隐式回滚恰好也清掉了 good_one（它是在 `_record` 的 INSERT 开启隐式事务
        之后才创建的）。而 001 建的表和版本表是在那之前 autocommit 落盘的，
        显式事务被去掉时它们会残留。因此只有"回到初始状态"这个断言才真正
        区分得出"有事务"与"没事务"。
        """
        bad_dir = os.path.join(self.tmp, "versions")
        os.makedirs(bad_dir)
        shutil.copy(os.path.join(VERSIONS_DIR, "001_baseline.py"),
                    os.path.join(bad_dir, "001_baseline.py"))
        with open(os.path.join(bad_dir, "002_broken.py"), "w", encoding="utf-8") as f:
            f.write(
                'REVISION = "002"\n'
                'DESCRIPTION = "deliberately broken"\n'
                'DOWN_REVISION = "001"\n'
                'UPGRADE_STATEMENTS = [\n'
                '    "CREATE TABLE good_one (id INTEGER)",\n'
                '    "THIS IS NOT VALID SQL",\n'
                ']\n'
            )
        with self.assertRaises(sqlite3.Error):
            run(self.db, backup=False, versions_dir=bad_dir, log=lambda *a, **k: None)

        # 整体回滚：不应留下任何表（既无业务表，也无版本表，更无半成品表）
        self.assertEqual(
            _tables(self.db), [],
            "失败迁移留下了中间态（未整体回滚）：%s" % _tables(self.db),
        )
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(applied_revisions(conn), [], "失败迁移被错误地记录了版本")
        finally:
            conn.close()

    def test_duplicate_revision_is_rejected(self):
        bad_dir = os.path.join(self.tmp, "dup")
        os.makedirs(bad_dir)
        src = os.path.join(VERSIONS_DIR, "001_baseline.py")
        shutil.copy(src, os.path.join(bad_dir, "a.py"))
        shutil.copy(src, os.path.join(bad_dir, "b.py"))
        with self.assertRaises(MigrationError) as ctx:
            load_migrations(bad_dir)
        self.assertIn("重复", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
