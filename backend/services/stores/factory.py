"""服务层装配点（composition root）。

**这是全项目唯一允许 import 具体存储实现的地方。**
业务代码（routers / 业务函数）只能拿到这里返回的**协议对象**，
不得自行 `from services.stores.sqlite_store import ...`。

这样将来把 SQLite 换成 Redis 时，改动面是"新增一个实现类 + 改这里的几行"，
而不是"翻遍业务代码"。装配点还适合放**启动期自检**（见下）。
"""

import os

from services.stores.base import SessionStore
from services.stores.sqlite_store import SQLiteSessionStore

#: 逃生舱阈值见 docs/02-architecture-v2.md §2.8
_OVERRIDE_ENV = "SESSION_STORE_BACKEND"

_session_store = None


def get_session_store():
    """返回会话存储（进程内单例）。

    实现由 `SESSION_STORE_BACKEND` 选择，目前只支持 `sqlite`（默认）。
    未来新增 Redis 实现时在这里加一个分支即可，业务代码一行都不用改。
    """
    global _session_store
    backend = os.getenv(_OVERRIDE_ENV, "sqlite").strip().lower()

    if _session_store is not None:
        return _session_store

    if backend == "sqlite":
        store = SQLiteSessionStore()
    else:
        raise RuntimeError(
            "未知的 %s=%r（当前仅支持 'sqlite'）" % (_OVERRIDE_ENV, backend)
        )

    # 装配期自检：实现必须满足协议，且实现类不得漏方法。
    # `SessionStore` 是 @runtime_checkable 的，所以这句是真的在检查
    # （T-16 专门为此写了用例）。放在这里，是为了让"实现漏了方法"
    # 在**启动时**就炸，而不是等某个接口第一次被调用。
    if not isinstance(store, SessionStore):
        raise RuntimeError(
            "%s 未满足 SessionStore 协议 —— 检查是否漏实现某个方法"
            % type(store).__name__
        )

    _session_store = store
    return store


def reset_session_store():
    """仅供测试：清掉单例，让下次调用重新装配。"""
    global _session_store
    _session_store = None
