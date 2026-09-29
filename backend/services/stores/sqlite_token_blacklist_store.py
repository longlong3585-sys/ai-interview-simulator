"""T-51 / ADR-003 选 B：`TokenBlacklistStore` —— `token_blacklist` 表的 SQLite 实现。

## 为什么需要它

T-50 的滑动续期让令牌链最长可活 **8 小时**（绝对上限）。在此之前"登出"只是
**前端把 localStorage 里的令牌删掉** —— 令牌本身仍然有效，谁把它抄走谁就还能用。
续期把窗口从 30 分钟拉长到 8 小时之后，"前端删除 = 登出"这个假设彻底不成立。

ADR-003 选 B 的做法：签发时带上 `jti`，登出时把 `jti` 写进黑名单，
任何请求先查黑名单。**续期沿用同一 `jti`**（T-50），因此一次登出能把整条续期链吊销。

## TTL 必须 ≥ 8 小时（`docs/02-arch-review.md` 的 R-4）

初稿把黑名单清理写成 30 分钟，而续期链可活 8 小时 ⇒ 被吊销的令牌**30 分钟后复活**，
吊销形同虚设。因此写入时算的过期时刻取：

    min(令牌自己的 exp, now + 8h)

* 取 `exp` 是"不早于令牌自然死亡"；
* 取 `now + 8h` 上限是"不晚于绝对上限"（T-50 保证没有任何令牌能活过它）。

两者取小 ⇒ 黑名单记录**一定**覆盖该令牌的完整剩余寿命，且不会留下
超过绝对上限的垃圾行。

## 与 `purge_expired` 的关系

清理是 **T-22 的既有定时任务**（`scripts/cleanup.py` 的 `--blacklist-days` 或
按 `expires_at` 清理）；本层只提供 `purge_expired(now)` 给它调用，
不自己起 timer。**不在验收路径上**也是刻意的：T-51 的验收标准只有
"登出后旧 token → 401"与"TTL ≥ 8h"，清理属运维节奏，不塞进登录/登出链路。
"""

import logging

from sqlalchemy import text

from services.stores._sqlite_tx import begin_write

logger = logging.getLogger("app.stores.token_blacklist")


class SQLiteTokenBlacklistStore(object):
    """`token_blacklist` 在 SQLite 上的实现。

    构造参数 `session_factory` 语义同其余仓储：默认 `database.SessionLocal`
    （继承 T-15 的 engine 契约），测试可注入独立工厂。

    时间一律用 **UTC ISO 字符串**（与 `base.py` 的时间契约一致）：
    同格式 UTC ISO 串的字典序等于时间序，`expires_at > :now` 因此**用得上索引**
    （`idx_blacklist_expires`）。
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

    def revoke(self, jti: str, expires_at: str) -> None:
        """吊销一个 `jti`，直到 `expires_at`（UTC ISO 串）。

        **幂等**：同一个 `jti` 被登出两次不应报错 —— 用户在两个标签页点"退出"、
        或者前端因网络重试，都会走到这里。用 `INSERT OR REPLACE` 而不是 `INSERT`：
        后者会抛主键冲突，把一个正常操作变成 500。
        """
        if not jti:
            raise ValueError("revoke() 需要非空 jti —— 没有 jti 的令牌无法被吊销")
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            s.execute(
                text("INSERT OR REPLACE INTO token_blacklist (jti, expires_at) "
                     "VALUES (:jti, :expires_at)"),
                {"jti": jti, "expires_at": expires_at},
            )
            s.commit()
        finally:
            s.close()

    def is_revoked(self, jti: str, now: str) -> bool:
        """该 `jti` 是否处于**生效中**的吊销状态。

        `expires_at <= now` 的行视为**已失效**（等同于不存在）：即使清理任务
        还没跑，行为也必须与"已清理"一致 —— 否则清理节奏会变成一个隐藏开关。
        """
        if not jti:
            return False
        s = self._session()
        try:
            row = s.execute(
                text("SELECT 1 FROM token_blacklist "
                     "WHERE jti = :jti AND expires_at > :now LIMIT 1"),
                {"jti": jti, "now": now},
            ).fetchone()
            return row is not None
        finally:
            s.close()

    # ------------------------------------------------------------------
    # 只读辅助（不属于协议，供清理任务与运维排查）
    # ------------------------------------------------------------------

    def purge_expired(self, now: str) -> int:
        """删除 `expires_at <= now` 的记录，返回删除行数。

        只能删**已过期**的行 —— 删除仍然生效的吊销记录等于让被吊销的令牌复活。
        """
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            result = s.execute(
                text("DELETE FROM token_blacklist WHERE expires_at <= :now"),
                {"now": now},
            )
            s.commit()
            return result.rowcount or 0
        finally:
            s.close()

    def count_all(self) -> int:
        """全部吊销记录数（**不含**过期过滤）。"""
        s = self._session()
        try:
            row = s.execute(text("SELECT count(*) FROM token_blacklist")).fetchone()
            return int(row[0]) if row else 0
        finally:
            s.close()

    def get_expires_at(self, jti: str):
        """取某条吊销记录的过期时刻（UTC ISO 串）；不存在返回 `None`。"""
        s = self._session()
        try:
            row = s.execute(
                text("SELECT expires_at FROM token_blacklist WHERE jti = :jti"),
                {"jti": jti},
            ).fetchone()
            return str(row[0]) if row else None
        finally:
            s.close()
