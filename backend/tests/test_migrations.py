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

# 与 001_baseline 声明一致 —— 即"迁移框架接管之前就已存在的三张表"。
# 既有库（legacy）里只有这三张，因此对它们的列/行数比对是"零改动"的判据。
LEGACY_EXPECTED = {
    "users": ["id", "username", "hashed_password", "created_at", "role", "email",
              "is_active", "nickname", "avatar", "bio", "gender", "birthday"],
    "interview_records": ["id", "user_id", "role", "messages", "report",
                          "created_at", "status", "admin_comment"],
    "notifications": ["id", "user_id", "type", "message", "target_type",
                      "target_id", "is_read", "created_at", "link_url"],
}

# 002 新增的四张表。
NEW_TABLES = {
    "interview_sessions": ["session_id", "user_id", "role", "questions",
                           "question_status", "user_answers", "current_index",
                           "last_seq", "last_reply", "version", "status",
                           "created_at", "updated_at", "expires_at",
                           "ended_reason"],
    "captcha_store": ["captcha_id", "code", "expires_at", "used"],
    "auth_attempts": ["id", "ip", "attempted_at"],
    "token_blacklist": ["jti", "expires_at"],
}

# 空库跑完全部迁移后期望的完整结构。
EXPECTED = {}
EXPECTED.update(LEGACY_EXPECTED)
EXPECTED.update(NEW_TABLES)


def _all_revisions():
    """从迁移脚本**实际加载**出的版本列表。

    刻意不写死 `["001", "002"]` —— 写死会让"新增一个迁移"变成"改一堆测试"，
    久而久之大家就会为了测试通过而改测试，而不是为了正确而改代码。
    """
    return [m.revision for m in load_migrations(VERSIONS_DIR)]


#: 破坏性语句的首关键字。`ON DELETE CASCADE` 这类**引用动作**不会命中
#: （它的首关键字是 `REFERENCES` 所在语句的 `CREATE`）。
DESTRUCTIVE_KEYWORDS = {"DROP", "DELETE", "TRUNCATE"}


def _leading_keywords(statement):
    """返回一条迁移语句里**每个 SQL 语句**的首关键字（大写）。

    为什么必须按语句分析而不是按子串搜索：
        `CREATE TABLE t (... REFERENCES users(id) ON DELETE CASCADE)`
        含 `DELETE` 子串，但整条语句是 CREATE，不做任何删除。
    为什么先按 `;` 切分：一条 UPGRADE_STATEMENTS 条目原则上只有一个语句
    （`sqlite3.Connection.execute` 也只接受一个），但若有人塞进
    `CREATE TABLE a(...); DROP TABLE b`，按语句切分才能识破。
    """
    import re

    keywords = []
    for chunk in statement.split(";"):
        # 去掉 -- 行注释与 /* */ 块注释，避免注释里的词被当成关键字
        text = re.sub(r"--[^\n]*", " ", chunk)
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        for token in re.findall(r"[A-Za-z_]+", text):
            keywords.append(token.upper())
            break
    return keywords


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

    def test_no_migration_contains_destructive_statements(self):
        """安全闸 4：任何迁移都不得出现 **破坏性语句**。

        两处必须讲清楚，否则这条闸门会被误用：

        1. **按"语句首关键字"判断，不能按子串判断。**
           T-17 的 `REFERENCES users(id) ON DELETE CASCADE` 含有 `DELETE`
           子串，但它是外键的**引用动作**，不是删除语句。
           首版实现用 `assertNotIn("DELETE ", blob)` 直接误报 —— 见下方
           `_leading_keywords` 的说明。
        2. **不禁止 `ALTER`。** T-18 的既定工作就是删死列 `link_url`
           （`ALTER TABLE ... DROP COLUMN`）。框架无法区分"被批准的
           结构变更"与"误操作" —— 那是代码评审的职责，不是这条闸门的。
           本闸门守的是"**框架/脚本不会自己删数据或删表**"。

        基线（001）另有一条更严的约束：必须是纯 CREATE（见下一个用例）。
        """
        for mig in load_migrations(VERSIONS_DIR):
            for stmt in mig.upgrade:
                for kw in _leading_keywords(stmt):
                    self.assertNotIn(
                        kw, DESTRUCTIVE_KEYWORDS,
                        "迁移 %s 含有破坏性语句（首关键字 %s）：%s"
                        % (mig.revision, kw, stmt.strip()[:80]),
                    )

    def test_baseline_is_pure_create(self):
        """基线必须只建对象，不得改动任何数据（v1 库接管时的第一条闸门）。"""
        baseline = load_migrations(VERSIONS_DIR)[0]
        self.assertEqual(baseline.revision, BASELINE_REVISION)
        for stmt in baseline.upgrade:
            self.assertEqual(
                _leading_keywords(stmt)[:1], ["CREATE"],
                "基线迁移只允许 CREATE，实得：%s" % stmt.strip()[:80],
            )

    def test_every_migration_is_idempotent_by_construction(self):
        """安全闸 5 的静态部分：每一条 CREATE 都必须带 IF NOT EXISTS。

        （运行期的幂等由 EmptyDatabaseTests.test_is_idempotent 覆盖；
         这里检查的是**脚本写法**，能更早发现问题。）
        """
        for mig in load_migrations(VERSIONS_DIR):
            for stmt in mig.upgrade:
                head = stmt.strip().upper()
                if head.startswith("CREATE"):
                    self.assertIn(
                        "IF NOT EXISTS", head,
                        "迁移 %s 的语句缺少 IF NOT EXISTS，重复执行会失败：%s"
                        % (mig.revision, head[:70]),
                    )

    def test_revision_chain_is_linked(self):
        """down_revision 必须串成一条链，否则乱序执行会得到错误结构。"""
        migs = load_migrations(VERSIONS_DIR)
        self.assertIsNone(migs[0].down_revision, "第一个迁移的 down_revision 必须为空")
        for prev, cur in zip(migs, migs[1:]):
            self.assertEqual(
                cur.down_revision, prev.revision,
                "迁移链断了：%s.down_revision=%r，应为 %r"
                % (cur.revision, cur.down_revision, prev.revision),
            )


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

    def test_records_all_revisions(self):
        run(self.db, backup=False, log=lambda *a, **k: None)
        conn = sqlite3.connect(self.db)
        try:
            revs = applied_revisions(conn)
        finally:
            conn.close()
        self.assertEqual(revs, _all_revisions())

    def test_is_idempotent(self):
        expected = len(_all_revisions())
        first = run(self.db, backup=False, log=lambda *a, **k: None)
        self.assertEqual(first, expected, "一次运行应把全部待应用迁移都跑掉")
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
        # 只对"接管前就存在的三张表"做零改动比对；002 新增的表不在其中
        self.before_counts = _counts(self.db, LEGACY_EXPECTED)
        self.before_cols = {t: _columns(self.db, t) for t in LEGACY_EXPECTED}
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

    def test_existing_tables_and_data_untouched(self):
        """核心断言：既有三张表的结构一行不改、数据一行不丢。

        注意这里**不再**断言"库里的表集合不变" —— 既有库的正确处理是
        "stamp 基线（不建表）+ 继续应用其后的迁移"。因此 002 新增的四张表
        **应该**出现；而接管前就存在的三张表必须原样不动。二者要分开断言，
        否则要么误放行"既有表被改"，要么误拦"新迁移不该执行"。
        """
        run(self.db, backup=False, log=lambda *a, **k: None)

        self.assertEqual(_sha256(self.db) != self.before_hash, True,
                         "库文件应当发生变化（版本表 + 002 新建的四张表）")

        # 既有三张表的结构完全不变（link_url 等原样保留）
        for table, cols in self.before_cols.items():
            self.assertEqual(_columns(self.db, table), cols,
                             "既有库的表 %s 结构被改动了" % table)

        # 既有数据一行不丢
        self.assertEqual(_counts(self.db, LEGACY_EXPECTED), self.before_counts,
                         "既有库的数据行数发生变化 —— 可能丢数据！")

        # 002 的四张新表应当被建出来
        present = set(_tables(self.db))
        for table in NEW_TABLES:
            self.assertIn(table, present, "既有库上未应用 002：缺表 %s" % table)

        # 全部迁移都已记录
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(applied_revisions(conn), _all_revisions())
        finally:
            conn.close()

    def test_running_twice_is_safe(self):
        run(self.db, backup=False, log=lambda *a, **k: None)
        after_first = _sha256(self.db)
        run(self.db, backup=False, log=lambda *a, **k: None)
        self.assertEqual(_sha256(self.db), after_first, "第二次运行改动了数据库")
        self.assertEqual(_counts(self.db, LEGACY_EXPECTED), self.before_counts)


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
