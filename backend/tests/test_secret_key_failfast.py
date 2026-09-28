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
import shutil
import tempfile
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


class RealEnvFileTests(unittest.TestCase):
    """T-07 补强：用**真实 .env 文件**验证，不 mock load_dotenv。

    为什么必须补这一组：
      上面那些用例都 mock 掉了 load_dotenv，等于绕开了"真实文件"这条路径。
      而生产上最可能出问题的恰恰是：**.env 文件存在、但漏配 SECRET_KEY**。
      正是这个盲区导致我第一次给用户的手工验证指令是错的
      （PowerShell 的 `$env:SECRET_KEY=""` 实际是删除变量，load_dotenv 随后
      又从真 .env 把值补了回来，于是"验证"通过但什么也没验证到）。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="t07-envfile-")
        self.env_path = os.path.join(self.tmpdir, ".env")

    def tearDown(self):
        importlib.reload(config)  # 恢复正常配置
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_env(self, body):
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write(body)

    def _load_with_real_env_file(self):
        """让 config 去读我们造的 .env 文件，并把 SECRET_KEY 从进程环境里清掉。

        清掉是必要的：python-dotenv 的 override=False 规则是"键已存在就不覆盖"，
        若不清，外部环境的值会掩盖 .env 的内容。
        """
        env = {k: v for k, v in REAL_ENV.items() if k not in ("SECRET_KEY", "APP_ENV_FILE")}
        env["APP_ENV_FILE"] = self.env_path
        with patch.dict(os.environ, env, clear=True):
            try:
                importlib.reload(config)
                return None
            except ValueError as exc:
                return exc

    def test_real_env_file_without_secret_key_raises(self):
        """生产最常见漏配：.env 有 API Key，但漏了 SECRET_KEY。"""
        self._write_env(
            "DEEPSEEK_API_KEY=sk-real-looking-key\n"
            "OPENAI_BASE_URL=https://api.deepseek.com/v1\n"
        )
        exc = self._load_with_real_env_file()
        self.assertIsNotNone(
            exc,
            ".env 文件里漏配 SECRET_KEY 时竟然启动成功 —— fail-fast 未生效",
        )
        self.assertIn("SECRET_KEY", str(exc))

    def test_real_env_file_with_empty_secret_key_raises(self):
        """另一种常见写法：写了键但值是空的。"""
        self._write_env(
            "DEEPSEEK_API_KEY=sk-real-looking-key\n"
            "SECRET_KEY=\n"
        )
        exc = self._load_with_real_env_file()
        self.assertIsNotNone(exc, "SECRET_KEY 为空值时竟然启动成功")
        self.assertIn("SECRET_KEY", str(exc))

    def test_real_env_file_with_placeholder_raises(self):
        self._write_env(
            "DEEPSEEK_API_KEY=sk-real-looking-key\n"
            "SECRET_KEY=change-this-to-a-random-string-at-least-64-chars\n"
        )
        exc = self._load_with_real_env_file()
        self.assertIsNotNone(exc, "占位符竟然被接受")
        self.assertIn("占位符", str(exc))

    def test_real_env_file_with_valid_secret_key_loads(self):
        """正样本：配置完整时必须能正常加载（防止修得过头把正常部署也锁死）。"""
        self._write_env(
            "DEEPSEEK_API_KEY=sk-real-looking-key\n"
            "SECRET_KEY=%s\n" % VALID_KEY
        )
        exc = self._load_with_real_env_file()
        self.assertIsNone(exc, "配置完整时不应报错，实际：%s" % exc)
        self.assertEqual(config.SECRET_KEY, VALID_KEY)

    def test_missing_env_file_entirely_reports_deepseek_first(self):
        """连 .env 都没有时，先报 DEEPSEEK_API_KEY（两者都缺的既有优先级）。"""
        env = {k: v for k, v in REAL_ENV.items()
               if k not in ("SECRET_KEY", "DEEPSEEK_API_KEY", "APP_ENV_FILE")}
        env["APP_ENV_FILE"] = os.path.join(self.tmpdir, "does-not-exist.env")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError) as ctx:
                importlib.reload(config)
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))

    def test_legacy_env_var_removal_does_not_disable_the_check(self):
        """还原用户当时的操作：变量被"置空"（实为删除），但真 .env 仍在。

        此时应当**从 .env 正常取值**（这是 dotenv 的预期行为），
        而不是误报错误 —— 用用例把这个语义钉死，避免以后又被误判为 bug。
        """
        self._write_env(
            "DEEPSEEK_API_KEY=sk-real-looking-key\n"
            "SECRET_KEY=%s\n" % VALID_KEY
        )
        exc = self._load_with_real_env_file()  # 内部已清掉外部 SECRET_KEY
        self.assertIsNone(exc)
        self.assertEqual(config.SECRET_KEY, VALID_KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
