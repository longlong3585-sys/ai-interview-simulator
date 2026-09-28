"""T-02 冒烟测试：确认测试库按预期被建表，且应用模块可正常导入。"""

import unittest

import database  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证


class TestSmoke(unittest.TestCase):

    def test_expected_tables_created(self):
        """3 张业务表必须存在于测试库。"""
        from sqlalchemy import inspect

        tables = set(inspect(database.engine).get_table_names())
        expected = {"users", "interview_records", "notifications"}
        self.assertTrue(
            expected.issubset(tables),
            "缺少业务表：%s（实际：%s）" % (sorted(expected - tables), sorted(tables)),
        )

    def test_models_are_queryable(self):
        """三张表都能被正常查询（返回整数行数，而不是报错）。

        刻意**不断言为 0**：会话内其他用例可能已写入数据，
        断言"空库"会让本用例依赖执行顺序（测试间隐式耦合）。
        "空库"性质由 test_environment_isolation 用独立探针单独保证。
        """
        db = database.SessionLocal()
        try:
            for model in (database.User, database.InterviewRecord, database.Notification):
                count = db.query(model).count()
                self.assertIsInstance(count, int)
                self.assertGreaterEqual(count, 0)
        finally:
            db.close()

    def test_notifications_expected_columns(self):
        """覆盖 database.py 手写 ALTER 补列的历史包袱，确认列齐全。"""
        from sqlalchemy import inspect

        cols = {c["name"] for c in inspect(database.engine).get_columns("notifications")}
        self.assertTrue(
            {"type", "target_type", "target_id"}.issubset(cols),
            "notifications 缺少列：%s" % sorted({"type", "target_type", "target_id"} - cols),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
