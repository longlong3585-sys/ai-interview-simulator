"""T-12 / NFR-2：日志初始化。

背景：
  `main.py` 的全局异常处理器此前只返回
      {"detail": "服务器内部错误，请稍后重试"}
  而**不记录任何日志** —— 未捕获异常被完全吞掉，线上排障没有堆栈可查。

  同时项目此前没有任何 logging 配置，即便写了 logger 也不会按预期输出。

本模块提供幂等的日志初始化；未来目录重构时会迁移到 core/log_setup.py
（刻意不叫 logging.py，避免遮蔽标准库）。
"""

import logging
import os
import sys

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
DEFAULT_LEVEL = "INFO"

_configured = False


def configure_logging(level=None):
    """初始化根 logger（幂等：重复调用不会叠加 handler）。

    日志级别取自参数或环境变量 LOG_LEVEL，默认 INFO。
    """
    global _configured
    root = logging.getLogger()
    if _configured:
        return root

    resolved = (level or os.getenv("LOG_LEVEL") or DEFAULT_LEVEL).upper()
    root.setLevel(getattr(logging, resolved, logging.INFO))

    # 只在尚无 handler 时添加，避免与 uvicorn / pytest 的既有 handler 重复输出
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
        root.addHandler(handler)

    _configured = True
    return root


def reset_for_tests():
    """仅供测试使用：允许重新初始化。"""
    global _configured
    _configured = False
