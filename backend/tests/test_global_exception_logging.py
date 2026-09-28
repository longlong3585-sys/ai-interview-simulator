"""T-12 测试：全局异常处理器记录堆栈，对外只给通用消息（NFR-2）。

漏洞背景：
  `main.py` 的 `@app.exception_handler(Exception)` 此前**不记录任何日志**，
  只返回 {"detail": "服务器内部错误，请稍后重试"} —— 未捕获异常被完全吞掉，
  线上排障没有堆栈可查。同时项目此前没有任何 logging 配置。

修复后：服务端 ERROR + 完整 traceback + 请求上下文；对外仍是通用消息
（不含异常类型/消息/堆栈），并附带 error_id 便于对日志。
"""

import asyncio
import logging
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from utils.log_setup import DEFAULT_FORMAT, configure_logging, reset_for_tests

GENERIC_MESSAGE = "服务器内部错误，请稍后重试"
SECRET_DETAIL = "内部数据库连接串 postgres://user:pw@host/db 泄露"


def _make_request(method="GET", path="/api/boom", client=("203.0.113.9", 54321)):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": client,
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)


def _raised_exc(exc_type=RuntimeError, message=SECRET_DETAIL):
    """返回一个**真正被抛出过**的异常。

    注意：直接 `RuntimeError("x")` 构造的对象 `__traceback__` 是 None，
    logging 便打不出 "Traceback (most recent call last)" 头
    （本用例初版就踩了这个坑）。生产路径上异常必然是被抛出的，
    测试必须同样如此才真实。
    """
    try:
        raise exc_type(message)
    except exc_type as exc:  # noqa: PERF203
        return exc


class LogSetupTests(unittest.TestCase):

    def tearDown(self):
        reset_for_tests()

    def test_configure_logging_is_idempotent(self):
        """重复调用不得叠加 handler（否则每条日志会打印多次）。"""
        reset_for_tests()
        configure_logging()
        count_after_first = len(logging.getLogger().handlers)
        configure_logging()
        configure_logging()
        self.assertEqual(len(logging.getLogger().handlers), count_after_first)

    def test_format_is_defined(self):
        self.assertIn("%(levelname)", DEFAULT_FORMAT)
        self.assertIn("%(message)s", DEFAULT_FORMAT)


class GlobalExceptionHandlerTests(unittest.TestCase):
    """直接调用处理器，断言"日志有堆栈、响应不含内部细节"。"""

    def test_logs_traceback_and_returns_generic_message(self):
        exc = _raised_exc(RuntimeError, SECRET_DETAIL)

        with self.assertLogs("app", level="ERROR") as captured:
            response = asyncio.run(
                main.global_exception_handler(_make_request(), exc)
            )

        # 1) 日志确实记录了，且含完整堆栈与上下文
        joined = "\n".join(captured.output)
        self.assertIn("未捕获异常", joined)
        self.assertIn("error_id=", joined)
        self.assertIn("GET /api/boom", joined)
        self.assertIn("203.0.113.9", joined)
        self.assertIn("Traceback (most recent call last)", joined, "日志缺少堆栈")
        self.assertIn(SECRET_DETAIL, joined, "日志应包含异常原文以便排障")

        # 2) 对外只给通用消息，绝不泄露内部细节
        self.assertEqual(response.status_code, 500)
        body = response.body.decode("utf-8")
        self.assertIn(GENERIC_MESSAGE, body)
        self.assertNotIn(SECRET_DETAIL, body, "响应体泄露了异常原文")
        self.assertNotIn("Traceback", body, "响应体泄露了堆栈")
        self.assertNotIn("RuntimeError", body, "响应体泄露了异常类型")

    def test_error_id_is_present_and_matches_log(self):
        """响应里的 error_id 必须能在日志中检索到（否则关联不上）。"""
        exc = _raised_exc(ValueError, "boom")
        with self.assertLogs("app", level="ERROR") as captured:
            response = asyncio.run(
                main.global_exception_handler(_make_request(), exc)
            )
        import json as _json

        payload = _json.loads(response.body.decode("utf-8"))
        error_id = payload.get("error_id")
        self.assertTrue(error_id, "响应应带 error_id 便于报障对日志")
        self.assertIn(error_id, "\n".join(captured.output))

    def test_handler_works_without_client_info(self):
        """request.client 为 None 时不得二次抛错。"""
        exc = _raised_exc(RuntimeError, "no client")
        with self.assertLogs("app", level="ERROR"):
            response = asyncio.run(
                main.global_exception_handler(_make_request(client=None), exc)
            )
        self.assertEqual(response.status_code, 500)


class RealRequestIntegrationTests(unittest.TestCase):
    """端到端：真的发一个会抛异常的请求，验证 500 与日志。"""

    def setUp(self):
        self.app = FastAPI()
        # 复用生产环境的处理器
        self.app.add_exception_handler(Exception, main.global_exception_handler)

        @self.app.get("/boom")
        async def boom():
            raise RuntimeError(SECRET_DETAIL)

        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_unhandled_exception_yields_500_and_is_logged(self):
        with self.assertLogs("app", level="ERROR") as captured:
            r = self.client.get("/boom")

        self.assertEqual(r.status_code, 500)
        body = r.text
        self.assertIn(GENERIC_MESSAGE, body)
        self.assertNotIn(SECRET_DETAIL, body)
        self.assertNotIn("Traceback", body)

        joined = "\n".join(captured.output)
        self.assertIn(SECRET_DETAIL, joined)
        self.assertIn("Traceback (most recent call last)", joined)

    def test_http_exception_still_returns_its_own_detail(self):
        """回归：HTTPException（如 401）不应被通用 500 处理器吞掉。"""
        client = TestClient(main.app)

        @main.app.get("/__t12_probe_401__")
        async def probe():
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="无效的认证凭证")

        try:
            r = client.get("/__t12_probe_401__")
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json()["detail"], "无效的认证凭证")
        finally:
            main.app.router.routes = [
                route
                for route in main.app.router.routes
                if getattr(route, "path", None) != "/__t12_probe_401__"
            ]


if __name__ == "__main__":
    unittest.main(verbosity=2)
