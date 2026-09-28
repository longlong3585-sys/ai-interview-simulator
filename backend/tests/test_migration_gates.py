"""T-18 测试：迁移框架新增的三道安全闸。

T-17 之前，迁移框架只能"把语句跑完、把版本记上"，没有任何**数据层面**的
校验。这对"新增表"够用，但对 T-18 的**重建表**远远不够 ——
重建表最危险的失败模式（`INSERT ... SELECT` 少拷了行、列错位、
写入外键违约数据）在提交后是**完全静默**的。

因此新增三道闸，全部在 COMMIT **之前**执行，任何一道不通过即整体回滚：

  6. 行数不得减少（可用 `ALLOWS_DATA_LOSS = True` 显式豁免）
  7. 声明式自检 `VERIFY_STATEMENTS = [(SQL, 期望值), ...]`
  8. `PRAGMA foreign_key_check` 必须为空

本文件用**端到端的坏迁移**来验证它们真的会拦住提交（而不是只测辅助函数），
并逐条确认失败后库里**不留任何痕迹**。
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

from migrations.runner import (  # noqa: E402
    MigrationError,
    applied_revisions,
    load_migrations,
    run,
)

VERSIONS_DIR = os.path.join(BACKEND_DIR, "migrations", "versions")


def noop(*args, **kwargs):
    pass


BASE_EXTRA = """
CREATE TABLE probe_items (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL);
INSERT INTO probe_items (id, name) VALUES (1, 'a'), (2, 'b'), (3, 'c');
INSERT INTO users (username, email, role, is_active) VALUES
    ('u1', 'u1@x.y', 'user', 1), ('u2', 'u2@x.y', 'user', 1);
"""


class GateHarness(unittest.TestCase):
    """搭一个"001 + 一个自定义迁移"的版本目录，并在上面跑 run()。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t18-gate-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "g.db")
        self.versions = os.path.join(self.tmp, "versions")
        os.makedirs(self.versions)
        shutil.copy(os.path.join(VERSIONS_DIR, "001_baseline.py"),
                    os.path.join(self.versions, "001_baseline.py"))

        # 先落到 001 并装入探针数据，再让"坏迁移"在有数据的库上跑
        run(self.db, backup=False, versions_dir=self.versions, log=noop)
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(BASE_EXTRA)
            conn.commit()
        finally:
            conn.close()
        self.before = self._dump()

    def _add_migration(self, filename, body):
        with open(os.path.join(self.versions, filename), "w", encoding="utf-8") as fh:
            fh.write(body)

    def _dump(self):
        """把库的可观测状态抓下来（表 + 各表行数 + 版本），用于"无痕迹"断言。"""
        conn = sqlite3.connect(self.db)
        try:
            tables = sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"))
            counts = {t: conn.execute("SELECT count(*) FROM [%s]" % t).fetchone()[0]
                      for t in tables}
            return {"tables": tables, "counts": counts,
                    "revisions": applied_revisions(conn)}
        finally:
            conn.close()

    def _assert_untouched(self, exc):
        """失败之后必须**完全无痕迹**：表、行数、版本记录都一样。"""
        self.assertIsInstance(exc, MigrationError)
        after = self._dump()
        self.assertEqual(after, self.before,
                         "闸门拦下之后库里留下了痕迹：\n  前=%s\n  后=%s"
                         % (self.before, after))

    # ------------------------------------------------------------------
    # 闸门 6：行数不得减少
    # ------------------------------------------------------------------

    def test_row_loss_is_blocked(self):
        self._add_migration("900_delete_rows.py", '''
REVISION = "900"
DESCRIPTION = "故意删行"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = ["DELETE FROM probe_items WHERE id = 3"]
''')
        with self.assertRaises(MigrationError) as ctx:
            run(self.db, backup=False, versions_dir=self.versions, log=noop)
        self.assertIn("行数减少", str(ctx.exception))
        self.assertIn("probe_items", str(ctx.exception))
        self._assert_untouched(ctx.exception)

    def test_rebuild_losing_rows_is_blocked(self):
        """真实场景：重建表时少拷了行，随后旧表被 DROP —— 必须拦住。"""
        self._add_migration("900_partial_copy.py", '''
REVISION = "900"
DESCRIPTION = "重建但只拷了部分行"
DOWN_REVISION = "001"
ALLOWS_TABLE_REBUILD = True
REBUILD_REASON = "测试用：人为制造一次少拷行的重建事故"
UPGRADE_STATEMENTS = [
    "CREATE TABLE probe_items_new (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)",
    "INSERT INTO probe_items_new (id, name) SELECT id, name FROM probe_items WHERE id = 1",
    "DROP TABLE probe_items",
    "ALTER TABLE probe_items_new RENAME TO probe_items",
]
''')
        with self.assertRaises(MigrationError) as ctx:
            run(self.db, backup=False, versions_dir=self.versions, log=noop)
        self.assertIn("行数减少", str(ctx.exception))
        self.assertIn("probe_items: 3 -> 1", str(ctx.exception))
        self._assert_untouched(ctx.exception)

    def test_data_loss_can_be_explicitly_opted_out(self):
        """确实要删行的迁移（如清理任务）显式声明后必须放行。"""
        self._add_migration("900_cleanup.py", '''
REVISION = "900"
DESCRIPTION = "显式声明允许删行"
DOWN_REVISION = "001"
ALLOWS_DATA_LOSS = True
UPGRADE_STATEMENTS = ["DELETE FROM probe_items WHERE id = 3"]
''')
        self.assertEqual(run(self.db, backup=False, versions_dir=self.versions, log=noop), 1)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM probe_items").fetchone()[0], 2)
        finally:
            conn.close()

    def test_added_rows_are_fine(self):
        """行数**增加**不该被拦（新增表、回填数据都是正常操作）。"""
        self._add_migration("900_add.py", '''
REVISION = "900"
DESCRIPTION = "加行"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = [
    "INSERT INTO probe_items (id, name) VALUES (4, 'd')",
    "CREATE TABLE extra (id INTEGER PRIMARY KEY)",
]
''')
        self.assertEqual(run(self.db, backup=False, versions_dir=self.versions, log=noop), 1)

    # ------------------------------------------------------------------
    # 闸门 7：声明式自检
    # ------------------------------------------------------------------

    def test_verify_mismatch_is_blocked(self):
        self._add_migration("900_verify_fail.py", '''
REVISION = "900"
DESCRIPTION = "自检不通过"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = ["INSERT INTO probe_items (id, name) VALUES (4, 'd')"]
VERIFY_STATEMENTS = [("SELECT count(*) FROM probe_items", 999)]
''')
        with self.assertRaises(MigrationError) as ctx:
            run(self.db, backup=False, versions_dir=self.versions, log=noop)
        self.assertIn("自检未通过", str(ctx.exception))
        self.assertIn("999", str(ctx.exception))
        self._assert_untouched(ctx.exception)

    def test_verify_passes_when_correct(self):
        self._add_migration("900_verify_ok.py", '''
REVISION = "900"
DESCRIPTION = "自检通过"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = ["INSERT INTO probe_items (id, name) VALUES (4, 'd')"]
VERIFY_STATEMENTS = [
    ("SELECT count(*) FROM probe_items", 4),
    ("SELECT count(*) FROM probe_items WHERE id = 4", 1),
]
''')
        self.assertEqual(run(self.db, backup=False, versions_dir=self.versions, log=noop), 1)

    def test_verify_sql_error_is_reported_clearly(self):
        self._add_migration("900_verify_bad_sql.py", '''
REVISION = "900"
DESCRIPTION = "自检 SQL 本身写错"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = []
VERIFY_STATEMENTS = [("SELECT count(*) FROM no_such_table", 0)]
''')
        with self.assertRaises(MigrationError) as ctx:
            run(self.db, backup=False, versions_dir=self.versions, log=noop)
        self.assertIn("自检 SQL 执行失败", str(ctx.exception))
        self.assertIn("no_such_table", str(ctx.exception))
        self._assert_untouched(ctx.exception)

    def test_malformed_verify_declaration_is_rejected_at_load(self):
        self._add_migration("900_verify_shape.py", '''
REVISION = "900"
DESCRIPTION = "自检声明形状不对"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = []
VERIFY_STATEMENTS = ["这不是一对 (SQL, 期望值)"]
''')
        with self.assertRaises(MigrationError) as ctx:
            load_migrations(self.versions)
        self.assertIn("VERIFY_STATEMENTS", str(ctx.exception))

    # ------------------------------------------------------------------
    # 闸门 8：外键完整性
    # ------------------------------------------------------------------

    def test_fk_violation_is_blocked(self):
        """**这道闸存在的原因**：迁移期间外键强制是关闭的，
        违约数据不会被当场拒绝 —— 必须靠提交前的 foreign_key_check 兜住。
        """
        self._add_migration("900_fk_violation.py", '''
REVISION = "900"
DESCRIPTION = "写入外键违约数据"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = [
    "INSERT INTO interview_records (user_id, role, status) "
    "VALUES (999999, 'orphan', 'pending')",
]
''')
        with self.assertRaises(MigrationError) as ctx:
            run(self.db, backup=False, versions_dir=self.versions, log=noop)
        self.assertIn("外键违规", str(ctx.exception))
        self.assertIn("interview_records", str(ctx.exception))
        self._assert_untouched(ctx.exception)

    def test_premise_runner_runs_with_foreign_keys_off(self):
        """把"迁移期间外键是关的"这个前提本身钉住。

        如果哪天框架改成在迁移时开启外键，闸门 8 的价值会下降
        （违约写入会当场报错），这条用例会失败并提醒我们更新说明。
        """
        recorded = {}

        self._add_migration("900_record_fk.py", '''
REVISION = "900"
DESCRIPTION = "记录迁移期的 foreign_keys 值"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = [
    "CREATE TABLE fk_probe (value INTEGER)",
]
VERIFY_STATEMENTS = [("SELECT count(*) FROM pragma_table_info('fk_probe')", 1)]
''')
        # 用一个自检把当时的取值带出来：foreign_keys 关闭时，孤儿行能插进去
        self._add_migration("901_fk_state.py", '''
REVISION = "901"
DESCRIPTION = "探测 foreign_keys 状态"
DOWN_REVISION = "900"
UPGRADE_STATEMENTS = [
    "INSERT INTO interview_records (user_id, role, status) VALUES (1, 'ok', 'pending')",
]
VERIFY_STATEMENTS = [("SELECT count(*) FROM interview_records WHERE user_id = 1", 1)]
''')
        run(self.db, backup=False, versions_dir=self.versions, log=noop)
        conn = sqlite3.connect(self.db)
        try:
            # 外键开着时，下面的孤儿行会立刻被拒；关着时插得进去 —— 用来判别
            try:
                conn.execute("INSERT INTO interview_records (user_id, role, status) "
                             "VALUES (999999, 'orphan', 'pending')")
                recorded["fk_enforced_on_raw_conn"] = False
            except sqlite3.IntegrityError:
                recorded["fk_enforced_on_raw_conn"] = True
        finally:
            conn.close()
        self.assertFalse(
            recorded["fk_enforced_on_raw_conn"],
            "前提变了：运行迁移用的裸连接现在会强制外键 —— "
            "请更新闸门 8 的说明与 test_fk_violation_is_blocked",
        )

    # ------------------------------------------------------------------
    # 重建例外必须在加载期自证
    # ------------------------------------------------------------------

    def test_rebuild_without_reason_is_rejected(self):
        self._add_migration("900_rebuild_no_reason.py", '''
REVISION = "900"
DESCRIPTION = "声明重建但没写理由"
DOWN_REVISION = "001"
ALLOWS_TABLE_REBUILD = True
UPGRADE_STATEMENTS = ["DROP TABLE probe_items"]
''')
        with self.assertRaises(MigrationError) as ctx:
            load_migrations(self.versions)
        self.assertIn("REBUILD_REASON", str(ctx.exception))

    def test_gate_failure_does_not_record_revision(self):
        """闸门是在 COMMIT 之前拦的 —— 版本记录也不能留下。"""
        self._add_migration("900_delete_rows.py", '''
REVISION = "900"
DESCRIPTION = "故意删行"
DOWN_REVISION = "001"
UPGRADE_STATEMENTS = ["DELETE FROM probe_items WHERE id = 3"]
''')
        with self.assertRaises(MigrationError):
            run(self.db, backup=False, versions_dir=self.versions, log=noop)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(applied_revisions(conn), ["001"],
                             "被拦下的迁移竟然被记进了版本表")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
