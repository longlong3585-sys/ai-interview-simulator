"""T-13 测试：跳过词的单一来源（FR-4.10）。

背景：
  修复前 `App.tsx` 与服务端 `interview.py` 各硬编码一份完全相同的 23 词列表。
  任一侧单独改动都会让"前端本地判定"与"服务端判定"分歧，且不会有任何报错。
  现在唯一来源是后端 `SKIP_WORDS` 常量，经 `GET /api/interview/config` 下发。

本文件验证：接口需要登录、返回 23 个词，且**返回的列表与实际判定逻辑同源**
（逐词验证：接口给的每个词都能触发跳过；接口没给的词不会触发）。
"""

import unittest

from fastapi.testclient import TestClient

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from routers.interview import SKIP_WORDS, _is_skip_message
from tests.support import bearer, create_test_user, delete_users, mint_token

USERNAME = "t13_user"
URL = "/api/interview/config"


class SkipWordsSingleSourceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        create_test_user(USERNAME, role="user")
        self.token = mint_token(USERNAME, role="user")

    def tearDown(self):
        delete_users([USERNAME])

    # ---------- 常量本身 ----------

    def test_constant_is_23_unique_nonempty_words(self):
        self.assertEqual(len(SKIP_WORDS), 23, "跳过词数量变化，需同步更新前端断言与 spec FR-4.10")
        self.assertEqual(len(set(SKIP_WORDS)), 23, "存在重复项")
        self.assertTrue(all(w.strip() for w in SKIP_WORDS), "存在空串")

    # ---------- 鉴权 ----------

    def test_config_requires_auth(self):
        """不在 §3.1 公开白名单内 —— 未登录必须 401。"""
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 401, "面试配置接口不应允许匿名访问")

    def test_config_rejects_admin(self):
        """admin 不能参加面试，访问面试配置应被 require 语义挡下吗？

        设计取舍：本接口只挂 get_current_user（仅要求登录）。
        若将来收紧为 require_user，本用例需要相应调整 —— 当前明确记录为"允许 admin"。
        """
        create_test_user("t13_admin", role="admin")
        try:
            token = mint_token("t13_admin", role="admin")
            r = self.client.get(URL, headers=bearer(token))
            self.assertEqual(r.status_code, 200, "当前设计允许已登录的 admin 读取该配置")
        finally:
            delete_users(["t13_admin"])

    # ---------- 接口契约 ----------

    def test_config_returns_skip_words(self):
        r = self.client.get(URL, headers=bearer(self.token))
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("skip_words", data)
        self.assertIsInstance(data["skip_words"], list)
        self.assertEqual(data["skip_words"], list(SKIP_WORDS))

    def test_returned_list_is_not_a_shared_mutable_reference(self):
        """接口返回的是副本，调用方改不动服务端常量。"""
        r = self.client.get(URL, headers=bearer(self.token))
        r.json()["skip_words"].append("篡改")
        self.assertNotIn("篡改", SKIP_WORDS)

    # ---------- 核心：接口下发的列表与实际判定同源 ----------

    def test_every_delivered_word_actually_triggers_skip(self):
        """逐词验证：接口给的每个词都必须真的能触发跳过判定。

        这是"单一来源"的实质断言 —— 若判定逻辑另有一份列表，
        某个词就会在接口里出现却不生效（或反之）。
        """
        delivered = self.client.get(URL, headers=bearer(self.token)).json()["skip_words"]
        for word in delivered:
            with self.subTest(word=word):
                self.assertTrue(
                    _is_skip_message(word),
                    "接口下发了 %r 但判定逻辑不认它 —— 可能存在第二份列表" % word,
                )

    def test_words_not_delivered_do_not_trigger(self):
        for word in ["这是正常回答", "我会这道题", "hello", "pass"]:
            with self.subTest(word=word):
                self.assertFalse(_is_skip_message(word))

    def test_detection_is_embedded_in_a_sentence(self):
        """实际使用场景：跳过词出现在句子中间也要能识别。"""
        for text in ["这个我不会啊", "太难了，换一个吧", "没学过这个技术", "想不起来怎么写"]:
            with self.subTest(text=text):
                self.assertTrue(_is_skip_message(text))

    def test_empty_message_is_not_a_skip(self):
        self.assertFalse(_is_skip_message(""))
        self.assertFalse(_is_skip_message("   "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
