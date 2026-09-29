"""存储访问抽象层（T-16）。

对外只暴露**协议与数据类型**，不暴露任何具体实现：

    from services.stores import SessionStore, CaptchaStore, RateLimitStore
    from services.stores import SessionSnapshot, SessionDraft, TurnCommit, CommitResult

具体实现（`SQLiteSessionStore` 等）在 T-19 ~ T-21 落地，
装配点是 `services.stores.sqlite_store`（尚未创建）。
业务代码**不得** import 实现模块，只依赖本包暴露的协议 —— 这样将来把
SQLite 换成 Redis 只需改装配处一行，不需要动业务逻辑
（ADR-004R §2.8 逃生舱，阈值见 docs/02-architecture-v2.md）。
"""

from services.stores.base import (  # noqa: F401
    ActiveSessionExists,
    CaptchaStore,
    CommitResult,
    EndedReason,
    RateLimitStore,
    ReplayLookup,
    SessionDraft,
    SessionNotFound,
    SessionSnapshot,
    SessionStatus,
    SessionStore,
    StoreError,
    TERMINAL_STATUSES,
    TokenBlacklistStore,
    iso_after,
    utcnow_iso,
)

__all__ = [
    "ActiveSessionExists",
    "CaptchaStore",
    "CommitResult",
    "EndedReason",
    "RateLimitStore",
    "ReplayLookup",
    "SessionDraft",
    "SessionNotFound",
    "SessionSnapshot",
    "SessionStatus",
    "SessionStore",
    "StoreError",
    "TERMINAL_STATUSES",
    "TokenBlacklistStore",
    "iso_after",
    "utcnow_iso",
]
