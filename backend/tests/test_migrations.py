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
# 既有库（legacy）里最初只有这三张，因此对它们的**数据行数**比对是"零丢失"的判据。
LEGACY_EXPECTED = {
    "users": ["id", "username", "hashed_password", "created_at", "role", "email",
              "is_active", "nickname", "avatar", "bio", "gender", "birthday"],
    "interview_records": ["id", "user_id", "role", "messages", "report",
                          "created_at", "status", "admin_comment"],
    "notifications": ["id", "user_id", "type", "message", "target_type",
                      "target_id", "is_read", "created_at", "link_url"],
}

# 002 + 003 之后，这三张**既有表**的最终结构。
# 刻意从 LEGACY_EXPECTED 推导而不是手抄 —— 将来若有迁移再改它们，
# 这里必须显式写出增量，改了什么一目了然。
FINAL_LEGACY = {
    "users": LEGACY_EXPECTED["users"] + ["must_change_password"],          # 003 加列
    "interview_records": LEGACY_EXPECTED["interview_records"] + ["client_token"],  # 003 加列
    "notifications": [c for c in LEGACY_EXPECTED["notifications"] if c != "link_url"],  # 003 删列
}

#: 迁移接管前就已存在的三张表（既有库的数据零丢失判据只针对它们）。
LEGACY_TABLES = sorted(LEGACY_EXPECTED)

# 002 新增的四张表。
NEW_TABLES = {
    "interview_sessions": ["session_id", "user_id", "role", "questions",
                           "question_status", "user_answers", "current_index",
                           "last_seq", "last_reply", "version", "status",
                           "created_at", "updated_at", "expires_at",
                           "ended_reason", "report"],
    "captcha_store": ["captcha_id", "code", "expires_at", "used"],
    "auth_attempts": ["id", "ip", "attempted_at"],
    "token_blacklist": ["jti", "expires_at"],
}

# 空库跑完全部迁移后期望的完整结构。
EXPECTED = {}
EXPECTED.update(FINAL_LEGACY)
EXPECTED.update(NEW_TABLES)


def _schema_facts(path):
    """提取一个库的"结构事实"，用于比较两条迁移路径是否收敛。

    刻意**不比较 `sqlite_master.sql` 文本**。实测发现：SQLite 执行
    `ALTER TABLE ADD COLUMN` 时会**就地拼接**新列，拼接点取决于原文本的排版
    （最后一列与 `PRIMARY KEY` 是否同行，拼接结果就不同）。因此文本差异可能
    纯属排版，不代表结构不同 —— 首版用文本比较，把无害的排版差异报成了漂移。
    比较 `PRAGMA` 给出的结构事实才可靠。
    """
    conn = sqlite3.connect(path)
    try:
        facts = {}
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in tables:
            columns = [
                # (列名, 类型, NOT NULL, 默认值, 是否主键)
                (d[1], (d[2] or "").upper(), d[3], d[4], d[5])
                for d in conn.execute("PRAGMA table_info([%s])" % name)
            ]
            indexes = []
            for r in conn.execute("PRAGMA index_list([%s])" % name):
                # r = (seq, name, unique, origin, partial)
                cols = tuple(i[2] for i in conn.execute("PRAGMA index_info([%s])" % r[1]))
                # 自动索引的**名字**含序号，与创建顺序有关；用占位名避免假失败
                idx_name = "«autoindex»" if r[1].startswith("sqlite_autoindex") else r[1]
                indexes.append((idx_name, r[2], cols, r[4]))
            foreign_keys = [
                (f[2], f[3], f[4], f[5], f[6])
                for f in conn.execute("PRAGMA foreign_key_list([%s])" % name)
            ]
            facts[name] = {
                "columns": columns,
                "indexes": sorted(indexes),
                "foreign_keys": sorted(foreign_keys),
            }
        return facts
    finally:
        conn.close()


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
    """造一个"既有库"：结构与数据都模仿线上，但**没有 schema_migrations 表**。

    这里的 DDL 与索引清单是**照真库抄的**（用 `PRAGMA` 导出后人工核对）：
    真库的 `users` 用的是 SQLAlchemy 生成的多行排版，并且带有
    `ix_users_id` / `ix_interview_records_id` / `ix_notifications_id` 三个索引
    （旧版 `create_all()` 按 ORM 的 `index=True` 建的）。
    T-18 新增的"两条路径必须收敛"断言**正是靠这个夹具发现**：
    原先夹具漏建了 `ix_users_id`，而 001 在既有库上只被 stamp、不执行，
    于是那个索引永远不会出现在既有库上 —— 夹具不忠实就会掩盖真实漂移。
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER NOT NULL, username VARCHAR, hashed_password VARCHAR,
                created_at DATETIME, role VARCHAR, email VARCHAR NOT NULL,
                is_active BOOLEAN, nickname VARCHAR, avatar VARCHAR, bio TEXT,
                gender VARCHAR, birthday DATE, PRIMARY KEY (id));
            CREATE INDEX ix_users_id ON users (id);
            CREATE UNIQUE INDEX ix_users_username ON users (username);
            CREATE UNIQUE INDEX ix_users_email ON users (email);
            CREATE TABLE interview_records (
                id INTEGER NOT NULL, user_id INTEGER, role VARCHAR, messages TEXT,
                report TEXT, created_at DATETIME, status VARCHAR, admin_comment TEXT,
                PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES users (id));
            CREATE INDEX ix_interview_records_id ON interview_records (id);
            CREATE TABLE notifications (
                id INTEGER NOT NULL, user_id INTEGER, type VARCHAR, message TEXT,
                target_type VARCHAR, target_id INTEGER, is_read BOOLEAN,
                created_at DATETIME, link_url VARCHAR,
                PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES users (id));
            CREATE INDEX ix_notifications_id ON notifications (id);
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
        2. **不禁止 `ALTER`。** T-18 的既定工作之一就是删死列
           （`notifications.link_url`）。框架无法区分"被批准的结构变更"
           与"误操作" —— 那是代码评审的职责，不是这条闸门的。
        3. **`DROP` 是"声明的例外"。** 重建表（SQLite 不支持
           `ADD COLUMN ... UNIQUE`）固有地需要 `DROP TABLE <旧表>`。
           因此允许，但必须：迁移显式声明 `ALLOWS_TABLE_REBUILD`、
           写出非空的 `REBUILD_REASON`、且只允许 `DROP TABLE <表名>` 这一种形态
           —— 细节由 `test_only_declared_migrations_may_drop` 逐条检查。

        基线（001）另有一条更严的约束：必须是纯 CREATE（见下一个用例）。
        """
        for mig in load_migrations(VERSIONS_DIR):
            for stmt in mig.upgrade:
                for kw in _leading_keywords(stmt):
                    if kw == "DROP":
                        # 只允许在已声明重建的迁移里出现（细节见另一个用例）
                        self.assertTrue(
                            mig.allows_table_rebuild,
                            "迁移 %s 含 DROP 但未声明 ALLOWS_TABLE_REBUILD：%s"
                            % (mig.revision, stmt.strip()[:80]),
                        )
                        continue
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
        """安全闸 5 的静态部分：CREATE 语句在正常情况下必须带 IF NOT EXISTS。

        **唯一的例外**是重建表用的临时表 `x_new`：它**刻意**不加
        IF NOT EXISTS —— 若 `x_new` 已存在，说明状态异常（上一次重建没清理干净），
        此时应当**立刻报错**而不是继续在脏状态上操作。
        这个例外只允许出现在显式声明了 `ALLOWS_TABLE_REBUILD` 的迁移里。
        """
        for mig in load_migrations(VERSIONS_DIR):
            for stmt in mig.upgrade:
                head = stmt.strip().upper()
                if not head.startswith("CREATE"):
                    continue
                if "IF NOT EXISTS" in head:
                    continue
                self.assertTrue(
                    mig.allows_table_rebuild,
                    "迁移 %s 的语句缺少 IF NOT EXISTS，且未声明重建例外：%s"
                    % (mig.revision, head[:70]),
                )
                self.assertRegex(
                    head, r"^CREATE TABLE \w+_NEW\b",
                    "迁移 %s 的非幂等 CREATE 只允许用于重建临时表（*_new）：%s"
                    % (mig.revision, head[:70]),
                )

    def test_only_declared_migrations_may_drop(self):
        """重建例外必须"声明 + 理由 + 只 DROP 临时目标"三件齐备。"""
        for mig in load_migrations(VERSIONS_DIR):
            drops = [s.strip() for s in mig.upgrade
                     if _leading_keywords(s)[:1] == ["DROP"]]
            if not drops:
                continue
            self.assertTrue(
                mig.allows_table_rebuild,
                "迁移 %s 含 DROP 语句但未声明 ALLOWS_TABLE_REBUILD：%s"
                % (mig.revision, drops),
            )
            self.assertTrue(
                mig.rebuild_reason.strip(),
                "迁移 %s 声明了重建但没写 REBUILD_REASON" % mig.revision,
            )
            for stmt in drops:
                # 只允许 DROP TABLE <名字>；不允许 DROP INDEX / VIEW / 带条件等
                self.assertRegex(
                    stmt.upper(), r"^DROP TABLE \w+$",
                    "重建只允许 `DROP TABLE <表名>`：%s" % stmt,
                )

    def test_rebuild_reason_is_substantive(self):
        for mig in load_migrations(VERSIONS_DIR):
            if mig.allows_table_rebuild:
                self.assertGreaterEqual(
                    len(mig.rebuild_reason.strip()), 30,
                    "迁移 %s 的重建理由过于简短，不足以让人日后判断是否仍需重建"
                    % mig.revision,
                )

    def test_baseline_and_002_do_not_claim_rebuild(self):
        """没有重建需求的迁移不得顺手声明该例外（避免例外被滥用）。"""
        migs = {m.revision: m for m in load_migrations(VERSIONS_DIR)}
        for rev in ("001", "002"):
            self.assertFalse(
                migs[rev].allows_table_rebuild,
                "迁移 %s 不需要重建，不应声明 ALLOWS_TABLE_REBUILD" % rev,
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

    def test_existing_data_is_never_lost(self):
        """核心断言：既有三张表的**数据一行不丢**。

        注意这里与 T-14 时期的语义差别（T-18 修正）：
        当时只有 001，既有库的正确处理是"只 stamp、不建表"，所以断言是
        "结构与数据都零改动"。现在有了 002/003，既有库会**被合法地改动**
        （002 新增四张表，003 给 users/interview_records 加列、删 notifications 的死列）。
        因此正确的判据是：

          * **数据行数**必须完全不变（本用例）
          * **最终结构**必须等于全部迁移声明的结果（下一个用例）
          * 且两条路径（既有库 / 空库）必须**收敛到同一套结构**（再下一个用例）

        把"结构不变"与"数据不丢"分开，才能既不误放行数据丢失，
        也不误拦合法的结构演进。
        """
        run(self.db, backup=False, log=lambda *a, **k: None)

        self.assertNotEqual(_sha256(self.db), self.before_hash,
                            "库文件应当发生变化（版本表 + 002/003 的结构变更）")

        # 既有数据一行不丢
        self.assertEqual(_counts(self.db, LEGACY_TABLES), self.before_counts,
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

    def test_existing_tables_reach_final_structure(self):
        """既有表最终必须等于迁移声明的结构（003 的加列/删列确实生效）。"""
        run(self.db, backup=False, log=lambda *a, **k: None)
        for table, cols in FINAL_LEGACY.items():
            self.assertEqual(
                _columns(self.db, table), cols,
                "既有表 %s 的最终结构与声明不符" % table,
            )

    def test_legacy_and_empty_paths_converge(self):
        """**两条路径必须收敛到同一套结构。**

        这是最有价值的一条断言：001 的 docstring 早就警告过
        "基线必须反映现实，否则对新库与既有库会得到两套不同结构"。
        有了重建表这种操作，路径分歧的风险显著上升
        （例如既有库走 stamp+增量、空库走完整脚本，二者若不等价就会埋雷）。

        比较的是**结构事实**（列/索引/外键），不是建表语句文本 ——
        理由见 `_schema_facts` 的说明。

        这个断言在 T-18 首次运行时**真的抓到了东西**：合成夹具漏建
        `ix_users_id`，而 001 在既有库上只 stamp 不执行，该索引便永远不会
        出现 —— 说明夹具本身不忠实。已按真库实际结构修正夹具。
        """
        run(self.db, backup=False, log=lambda *a, **k: None)

        other_tmp = tempfile.mkdtemp(prefix="t14-converge-")
        self.addCleanup(shutil.rmtree, other_tmp, ignore_errors=True)
        fresh = os.path.join(other_tmp, "fresh.db")
        run(fresh, backup=False, log=lambda *a, **k: None)

        legacy, from_scratch = _schema_facts(self.db), _schema_facts(fresh)

        self.assertEqual(
            sorted(legacy), sorted(from_scratch),
            "既有库与空库建出的表集合不同：只在既有库=%s 只在空库=%s"
            % (sorted(set(legacy) - set(from_scratch)),
               sorted(set(from_scratch) - set(legacy))),
        )
        for table in sorted(from_scratch):
            self.assertEqual(
                legacy[table]["columns"], from_scratch[table]["columns"],
                "表 %s 的列在两条路径下不同：\n  既有库=%s\n  空库  =%s"
                % (table, legacy[table]["columns"], from_scratch[table]["columns"]),
            )
            self.assertEqual(
                legacy[table]["indexes"], from_scratch[table]["indexes"],
                "表 %s 的索引在两条路径下不同：\n  既有库=%s\n  空库  =%s"
                % (table, legacy[table]["indexes"], from_scratch[table]["indexes"]),
            )
            self.assertEqual(
                legacy[table]["foreign_keys"], from_scratch[table]["foreign_keys"],
                "表 %s 的外键在两条路径下不同：\n  既有库=%s\n  空库  =%s"
                % (table, legacy[table]["foreign_keys"],
                   from_scratch[table]["foreign_keys"]),
            )

    def test_running_twice_is_safe(self):
        run(self.db, backup=False, log=lambda *a, **k: None)
        after_first = _sha256(self.db)
        run(self.db, backup=False, log=lambda *a, **k: None)
        self.assertEqual(_sha256(self.db), after_first, "第二次运行改动了数据库")
        self.assertEqual(_counts(self.db, LEGACY_TABLES), self.before_counts)


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


class ProtocolSchemaAgreementTests(unittest.TestCase):
    """协议 ↔ DDL 的**跨层一致性**（跑完整迁移链后比对）。

    这是本项目最有价值的一条跨层断言：它把"图纸"（`SessionSnapshot` 协议）
    与"实物"（`interview_sessions` 的列）钉在一起。只改一边会立刻失败，
    而不是拖到运行时才暴露。

    为什么放在这里而不是某个迁移的测试里：协议对应的是**最终**结构，
    所以必须跑完整链条。放在 002 的测试里会在每次新增迁移时误报
    —— T-19 加 004 时就是这样。
    （反向断言在 `test_stores_protocol.py`：它用硬编码集合校验协议本身。）
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t14-proto-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "full.db")
        run(self.db, backup=False, log=lambda *a, **k: None)

    def test_session_snapshot_matches_final_ddl(self):
        from dataclasses import fields

        from services.stores.base import SessionSnapshot

        ddl = set(_columns(self.db, "interview_sessions"))
        protocol = {f.name for f in fields(SessionSnapshot)}
        self.assertEqual(
            ddl, protocol,
            "interview_sessions 的列与 SessionSnapshot 字段不一致："
            "只在 DDL=%s 只在协议=%s"
            % (sorted(ddl - protocol), sorted(protocol - ddl)),
        )

    def test_full_chain_matches_expected_columns(self):
        """`EXPECTED` 必须与实跑出来的**最终**结构完全一致。"""
        for table, cols in EXPECTED.items():
            self.assertEqual(_columns(self.db, table), cols,
                             "表 %s 的最终列与 EXPECTED 不符" % table)


if __name__ == "__main__":
    unittest.main(verbosity=2)
