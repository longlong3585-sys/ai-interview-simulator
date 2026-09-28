"""测试包引导 —— **必须在导入任何应用模块之前完成环境隔离**。

T-02 核心目标：让测试永不触碰真实的 `backend/interview.db`。

原理（已核对源码）：
  - `database.py:4` 调用 `load_dotenv()`，**默认 `override=False`**；
  - `database.py:11` 才执行 `os.getenv("DATABASE_URL", ...)`。

因此只要在本包导入时**先**把 `DATABASE_URL` 指向临时文件，
`.env` 中的真实值就不会覆盖它，后续 `import database` 会建出**独立的测试库**。

隔离是**进程级**的：只要测试通过本包进入，就自动生效，
不需要每个用例自己记得切换 —— 这正是"防呆"设计。

用法：
    cd backend
    python -m unittest discover -s tests -t . -v
"""

import atexit
import os
import shutil
import sys
import tempfile

# ---- 1. 让 `import database` / `import config` 等可用（backend/ 加入 sys.path）----
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

REPO_ROOT = os.path.dirname(BACKEND_DIR)
REAL_DB_PATH = os.path.join(BACKEND_DIR, "interview.db")

# ---- 2. 创建独立测试库（文件型，非内存 —— 以覆盖 WAL / 事务行为）----
_TEST_TMP_DIR = tempfile.mkdtemp(prefix="interview-test-")
TEST_DB_PATH = os.path.join(_TEST_TMP_DIR, "test_interview.db")

# 仅当调用方没有显式指定时才覆盖，便于将来手动指向别的库
if not os.environ.get("INTERVIEW_TEST_KEEP_DB_URL"):
    os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DB_PATH.replace("\\", "/")

# ---- 2b. T-14：测试库的表结构改由迁移脚本建立 ----
# 原先依赖 `database.py` 在 import 期调用 `create_all()`；该行为已被移除
# （import 有副作用是当初结构失控的根源之一）。因此这里显式跑一次迁移。
# 必须放在 import database **之前**，否则引擎会指向一个还没有表的库。
def _bootstrap_test_schema():
    from migrations.runner import run as run_migrations

    run_migrations(TEST_DB_PATH, backup=False, log=lambda *_a, **_k: None)


_bootstrap_test_schema()

# ---- 3. 会话结束自动清理 ----
@atexit.register
def _cleanup_test_tmp():
    shutil.rmtree(_TEST_TMP_DIR, ignore_errors=True)


def real_db_fingerprint():
    """返回真实库的 (mtime, size)；不存在则为 None。

    供隔离性测试断言"真实库未被测试改动"。
    """
    if not os.path.isfile(REAL_DB_PATH):
        return None
    st = os.stat(REAL_DB_PATH)
    return (st.st_mtime_ns, st.st_size)
