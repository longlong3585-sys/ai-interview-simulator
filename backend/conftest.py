"""pytest 引导（前向兼容）。

当前环境 **PyPI 不可达**（DNS/TCP 通但 TLS 被重置），无法安装 pytest，
因此本轮的测试以标准库 `unittest` 运行：

    cd backend
    python -m unittest discover -s tests -t . -v

本文件的作用：一旦将来 pytest 可安装，`pytest` 会自动加载本 conftest，
先导入 `tests` 包以完成**环境隔离**，再收集用例 —— 行为与 unittest 一致，
无需改动任何测试代码。
"""

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import tests  # noqa: F401,E402  —— 触发 tests/__init__.py 的环境隔离（必须在应用模块之前）
