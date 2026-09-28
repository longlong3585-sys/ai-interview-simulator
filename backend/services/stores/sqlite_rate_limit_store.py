"""T-21：`SQLiteRateLimitStore` —— `RateLimitStore` 协议的 SQLite 实现。

## 为什么需要它

原实现（`auth.py`）用模块级字典 `ip_attempts: Dict[str, List]` 记失败次数。
与 T-20 的验证码同样的病：ADR-006 的部署形态是 `uvicorn --workers 2`，
**每个 worker 一份字典** → 攻击者在 worker A 失败 3 次、worker B 失败 3 次，
两边都没到 5 次上限 → **限流被 worker 数放大**。
落到表里之后，滑动窗口在多 worker 下才是真的。

## **只记失败，成功不写**（ADR-014）

登录成功是高频路径。若成功也写，就把这条路径变成写热点，
与 ADR-002/ADR-004 想消除的 `database is locked` 目标直接冲突。
因此本实现**没有**任何 `record_success` —— 成功时调用方调 `clear(ip)`
（删掉该 IP 的失败记录，让用户立刻恢复），那是一次 DELETE，不产生新行。

## 滑动窗口的边界（与 `purge_older_than` 严格互补）

    记入统计：attempted_at >= since
    清    除：attempted_at <  before

调用方传 `since = before = now - 窗口长度` 时，两式恰好互补 ——
**被判为"窗口外"的记录一定清得掉**，否则会出现"计数时不算它、清理时又留着"
的错位（表只增不减，且每次登录都要扫更多行）。
与 T-20 的 verify/purge 互补性是同一条纪律。

## 窗口长度与阈值不在本层

`CAPTCHA_MAX_ERRORS = 5` / `CAPTCHA_LOCK_MINUTES = 10` 由 `config` 持有，
调用方负责把 `since` 算好传进来。存储层不重复定义，避免两处漂移。
"""

import logging

from sqlalchemy import text

logger = logging.getLogger("app.stores.rate_limit")


class SQLiteRateLimitStore(object):
    """`RateLimitStore` 协议在 SQLite 上的实现。

    构造参数 `session_factory` 语义同其余两个仓储：默认 `database.SessionLocal`
    （继承 T-15 的 engine 契约），测试可注入独立工厂。
    """

    def __init__(self, session_factory=None):
        self._session_factory = session_factory

    def _factory(self):
        if self._session_factory is None:
            from database import SessionLocal  # 延迟导入，避免 import 期绑定引擎
            self._session_factory = SessionLocal
        return self._session_factory

    def _session(self):
        return self._factory()()

    # ------------------------------------------------------------------

    def count_failures(self, ip: str, since: str) -> int:
        """统计该 IP 在 `since`（**含**）之后的失败次数。

        含边界是有意的：调用方传 `since = now - 10min` 时，
        "正好 10 分钟前那一次"仍应计入窗口（见模块说明的互补性）。
        """
        s = self._session()
        try:
            row = s.execute(
                text("SELECT count(*) FROM auth_attempts "
                     "WHERE ip = :ip AND attempted_at >= :since"),
                {"ip": ip, "since": since},
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            s.close()

    def record_failure(self, ip: str, at: str) -> None:
        """记录一次失败。**只在失败时调用**（成功路径不得写入，见模块说明）。"""
        s = self._session()
        try:
            s.execute(
                text("INSERT INTO auth_attempts (ip, attempted_at) VALUES (:ip, :at)"),
                {"ip": ip, "at": at},
            )
            s.commit()
        finally:
            s.close()

    def clear(self, ip: str) -> None:
        """清除该 IP 的失败记录（登录成功后调用，让用户立刻恢复）。

        注意这是**删除**而不是"记一次成功" —— 表里只该有失败。
        """
        s = self._session()
        try:
            s.execute(text("DELETE FROM auth_attempts WHERE ip = :ip"), {"ip": ip})
            s.commit()
        finally:
            s.close()

    def purge_older_than(self, before: str) -> int:
        """删除 `before` **之前**的记录，返回删除行数（T-22 定时清理调用）。"""
        s = self._session()
        try:
            result = s.execute(
                text("DELETE FROM auth_attempts WHERE attempted_at < :before"),
                {"before": before},
            )
            s.commit()
            return result.rowcount or 0
        finally:
            s.close()

    # ------------------------------------------------------------------
    # 只读辅助（不属于协议，供测试与运维排查）
    # ------------------------------------------------------------------

    def count_all(self, ip: str = None) -> int:
        """该 IP（或不限 IP）的全部失败记录数 —— **不含窗口过滤**。"""
        s = self._session()
        try:
            if ip is None:
                row = s.execute(text("SELECT count(*) FROM auth_attempts")).fetchone()
            else:
                row = s.execute(
                    text("SELECT count(*) FROM auth_attempts WHERE ip = :ip"),
                    {"ip": ip},
                ).fetchone()
            return int(row[0]) if row else 0
        finally:
            s.close()
