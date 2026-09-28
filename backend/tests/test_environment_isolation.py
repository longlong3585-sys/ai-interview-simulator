"""T-02 最关键的安全测试：证明测试**绝不会**污染真实数据库。

背景：`database.py` 在 import 期就 `create_all()` 并执行手写 ALTER，
一旦 `DATABASE_URL` 指向真实库，跑一次测试就会改写线上数据。
本文件把"隔离是否真的生效"变成**可执行断言**，而不是靠人工自觉。
"""

import os
import unittest

from tests import REAL_DB_PATH, TEST_DB_PATH, real_db_fingerprint

import database  # noqa: E402  —— 由 tests/__init__.py 保证此时 DATABASE_URL 已隔离


class TestEnvironmentIsolation(unittest.TestCase):

    def test_database_url_points_to_temp_not_real(self):
        """DATABASE_URL 必须指向临时测试库。"""
        url = database.DATABASE_URL
        self.assertIn("test_interview.db", url)
        self.assertNotIn(
            os.path.basename(REAL_DB_PATH),
            url.replace("test_interview.db", ""),
            "DATABASE_URL 指向了真实库 basename，隔离失败",
        )

    def test_engine_url_is_temp(self):
        """engine 实际使用的 URL 也必须是临时库。"""
        engine_url = str(database.engine.url)
        self.assertIn("test_interview.db", engine_url)
        self.assertNotEqual(
            os.path.abspath(REAL_DB_PATH).replace("\\", "/"),
            TEST_DB_PATH.replace("\\", "/"),
        )

    def test_temp_db_file_exists(self):
        """测试库文件应已被 create_all 建出。"""
        self.assertTrue(
            os.path.isfile(TEST_DB_PATH),
            "测试库未创建：%s" % TEST_DB_PATH,
        )

    def test_write_lands_in_temp_db_and_real_db_unchanged(self):
        """写入一条数据：必须落在测试库，且真实库指纹完全不变。

        注意：本用例会向测试库写入数据，必须**自行清理**，
        否则会污染同会话中其他"期望空库"的用例（测试间不得有隐式依赖）。
        """
        before = real_db_fingerprint()

        db = database.SessionLocal()
        try:
            probe = database.User(
                username="__isolation_probe__",
                hashed_password="not-a-real-hash",
                email="isolation-probe@test.local",
            )
            db.add(probe)
            db.commit()

            found = (
                db.query(database.User)
                .filter(database.User.username == "__isolation_probe__")
                .first()
            )
            self.assertIsNotNone(found, "写入未落到测试库")
        finally:
            # 清理探针，保证本用例无副作用
            try:
                db.query(database.User).filter(
                    database.User.username == "__isolation_probe__"
                ).delete()
                db.commit()
            finally:
                db.close()

        remaining = database.SessionLocal()
        try:
            # 只断言"自己的探针"被清掉，**不能**断言整表为 0：
            # 其他测试（如 test_chat_auth 触发的应用 startup）会创建 admin 账号，
            # 断言空表会让本用例依赖执行顺序。
            leftover = (
                remaining.query(database.User)
                .filter(database.User.username == "__isolation_probe__")
                .count()
            )
            self.assertEqual(leftover, 0, "探针数据未被清理干净")
        finally:
            remaining.close()

        after = real_db_fingerprint()
        self.assertEqual(
            before,
            after,
            "真实库 %s 在测试期间被修改（mtime/size 变化）—— 隔离失效！" % REAL_DB_PATH,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
