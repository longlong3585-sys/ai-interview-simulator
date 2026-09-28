"""T-07 测试：SECRET_KEY 缺失/弱值必须导致启动即失败（NFR-1c）。

漏洞背景：
  修复前 `config.py` 写的是
      SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
  漏配 .env 时会**静默**使用这个公开可知的默认值签发/校验 JWT ——
  任何人都能伪造任意用户（含 admin）的令牌，且没有任何告警。
  对照：同文件对 DEEPSEEK_API_KEY 缺失就是直接 raise。

测试手法：
  `config.py` 在 import 期执行校验，因此用 importlib.reload + 清空环境变量来触发，
  并 mock 掉 load_dotenv（否则它会从 .env 把 SECRET_KEY 重新读回来）。
  每个用例结束后 reload 一次恢复正常配置，避免污染其他测试。
"""

import importlib
import os
import unittest
from unittest.mock import patch

import config  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证

VALID_KEY = "k" * 64
REAL_ENV = dict(os.environ)


def _reload_with(secret_key, drop=False):
    """在指定 SECRET_KEY 环境下重新加载 config，返回捕获的异常（无异常则 None）。"""
    env = dict(REAL_ENV)
    env["DEEPSEEK_API_KEY"] = env.get("DEEPSEEK_API_KEY") or "sk-test-placeholder"
    if drop:
        env.pop("SECRET_KEY", None)
    else:
        env["SECRET_KEY"] = secret_key

    with patch.dict(os.environ, env, clear=True), \
         patch("dotenv.load_dotenv", lambda *a, **k: None):
        try:
            importlib.reload(config)
            return None
        except ValueError as exc:
            return exc


class SecretKeyFailFastTests(unittest.TestCase):

    def tearDown(self):
        # 无论用例如何结束，都恢复正常配置
        importlib.reload(config)

    # ---------- 1. 缺失 → 必须失败 ----------

    def test_missing_secret_key_raises(self):
        exc = _reload_with(None, drop=True)
        self.assertIsNotNone(exc, "SECRET_KEY 缺失时竟然启动成功 —— 会静默使用默认密钥")
        self.assertIn("SECRET_KEY", str(exc))

    def test_empty_secret_key_raises(self):
        exc = _reload_with("")
        self.assertIsNotNone(exc, "SECRET_KEY 为空字符串时竟然启动成功")

    # ---------- 2. 占位符 → 必须失败 ----------

    def test_documented_placeholders_are_rejected(self):
        for placeholder in (
            "dev-secret-change-in-production",
            "change-this-to-a-random-string-at-least-64-chars",
            "your-random-secret-key-at-least-64-chars",
        ):
            with self.subTest(placeholder=placeholder):
                exc = _reload_with(placeholder)
                self.assertIsNotNone(
                    exc, "文档占位符 %r 被接受 —— 照抄示例即可上线" % placeholder
                )
                self.assertIn("占位符", str(exc))

    # ---------- 3. 过短 → 必须失败 ----------

    def test_short_secret_key_is_rejected(self):
        exc = _reload_with("short-key")
        self.assertIsNotNone(exc, "过短的 SECRET_KEY 被接受")
        self.assertIn("过短", str(exc))

    def test_boundary_length_31_rejected_32_accepted(self):
        self.assertIsNotNone(_reload_with("a" * 31), "31 字符应被拒绝")
        self.assertIsNone(_reload_with("a" * 32), "32 字符应被接受（边界）")

    # ---------- 4. 合法值 → 正常 ----------

    def test_valid_secret_key_is_accepted(self):
        exc = _reload_with(VALID_KEY)
        self.assertIsNone(exc, "合法 SECRET_KEY 不应报错")
        self.assertEqual(config.SECRET_KEY, VALID_KEY)

    def test_real_project_env_is_still_valid(self):
        """回归：项目真实 .env 里的 SECRET_KEY 必须仍然可用（本次改动不能锁死现有部署）。"""
        importlib.reload(config)
        self.assertTrue(config.SECRET_KEY)
        self.assertGreaterEqual(len(config.SECRET_KEY), 32)

    # ---------- 5. 与 DEEPSEEK_API_KEY 的处理保持一致 ----------

    def test_deepseek_key_still_fails_fast(self):
        """同文件既有的 fail-fast 行为不应被破坏。"""
        env = dict(REAL_ENV)
        env.pop("DEEPSEEK_API_KEY", None)
        env["SECRET_KEY"] = VALID_KEY
        with patch.dict(os.environ, env, clear=True), \
             patch("dotenv.load_dotenv", lambda *a, **k: None):
            with self.assertRaises(ValueError) as ctx:
                importlib.reload(config)
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
