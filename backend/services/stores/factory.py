"""服务层装配点（composition root）。

**这是全项目唯一允许 import 具体存储实现的地方。**
业务代码（routers / 业务函数）只能拿到这里返回的**协议对象**，
不得自行 `from services.stores.sqlite_store import ...`。

这样将来把 SQLite 换成 Redis 时，改动面是"新增一个实现类 + 改这里的几行"，
而不是"翻遍业务代码"。装配点还负责**启动期自检**（见 `_assemble`）。

逃生舱触发阈值见 docs/02-architecture-v2.md §2.8：
    多台应用服务器 / database is locked > 1 次每日 / 峰值写 > 50/s / 活跃会话 > 1000
"""

import os

from services.stores.base import CaptchaStore, RateLimitStore, SessionStore
from services.stores.sqlite_captcha_store import SQLiteCaptchaStore
from services.stores.sqlite_rate_limit_store import SQLiteRateLimitStore
from services.stores.sqlite_store import SQLiteSessionStore

#: 装配表：kind -> (环境变量, 协议, 中文名, sqlite 实现类)
#:
#: **加一个存储只需要在这里加一行** —— `_assemble` 的装配与协议自检逻辑
#: 完全不用改（T-20 与 T-21 都验证了这一点）。
_REGISTRY = {
    "session": (
        "SESSION_STORE_BACKEND", SessionStore, "会话存储", SQLiteSessionStore,
    ),
    "captcha": (
        "CAPTCHA_STORE_BACKEND", CaptchaStore, "验证码存储", SQLiteCaptchaStore,
    ),
    "rate_limit": (
        "RATE_LIMIT_STORE_BACKEND", RateLimitStore, "限流存储",
        SQLiteRateLimitStore,
    ),
}

_instances = {}


def _assemble(kind):
    """按 `_REGISTRY` 装配一个存储实现，并做**协议自检**。"""
    env_name, protocol, label, sqlite_impl = _REGISTRY[kind]
    backend = os.getenv(env_name, "sqlite").strip().lower()

    if backend != "sqlite":
        raise RuntimeError(
            "未知的 %s=%r（%s当前仅支持 'sqlite'）" % (env_name, backend, label)
        )

    store = sqlite_impl()

    # 装配期自检：实现必须满足协议。
    # `SessionStore` / `CaptchaStore` 都是 @runtime_checkable 的，所以这句
    # 是真的在检查（T-16 专门为此写了用例）。放在这里，是为了让
    # "实现漏了方法" 在**启动时**就炸，而不是等某个接口第一次被调用。
    if not isinstance(store, protocol):
        raise RuntimeError(
            "%s 未满足 %s 协议 —— 检查是否漏实现某个方法"
            % (type(store).__name__, protocol.__name__)
        )

    _instances[kind] = store
    return store


def get_session_store():
    """返回会话存储（进程内单例）。实现由 `SESSION_STORE_BACKEND` 选择。"""
    if "session" not in _instances:
        _assemble("session")
    return _instances["session"]


def get_captcha_store():
    """返回验证码存储（进程内单例）。实现由 `CAPTCHA_STORE_BACKEND` 选择。"""
    if "captcha" not in _instances:
        _assemble("captcha")
    return _instances["captcha"]


def get_rate_limit_store():
    """返回限流存储（进程内单例）。实现由 `RATE_LIMIT_STORE_BACKEND` 选择。

    ⚠️ 限流键必须来自 `utils.client_ip.resolve_client_ip(request)`，
    **不要**用 `request.client.host` —— 那在 Nginx 后面是全站共用的代理 IP。
    """
    if "rate_limit" not in _instances:
        _assemble("rate_limit")
    return _instances["rate_limit"]


def reset_stores():
    """仅供测试：清掉全部单例，让下次调用重新装配。"""
    _instances.clear()


def reset_session_store():
    """仅供测试：只清会话存储单例（T-19 引入，保留以兼容既有用例）。"""
    _instances.pop("session", None)


def reset_captcha_store():
    """仅供测试：只清验证码存储单例。"""
    _instances.pop("captcha", None)


def reset_rate_limit_store():
    """仅供测试：只清限流存储单例。"""
    _instances.pop("rate_limit", None)


__all__ = [
    "get_session_store",
    "get_captcha_store",
    "get_rate_limit_store",
    "reset_stores",
    "reset_session_store",
    "reset_captcha_store",
    "reset_rate_limit_store",
]
