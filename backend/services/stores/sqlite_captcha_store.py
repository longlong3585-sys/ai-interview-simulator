"""T-20：`SQLiteCaptchaStore` —— `CaptchaStore` 协议的 SQLite 实现。

## 为什么要把它从内存搬到表里

原实现（`auth.py`）用模块级字典 `captcha_store: Dict[str, Dict]` 存验证码。
单进程没问题，但 ADR-006 的部署形态是 `uvicorn --workers 2`：
**每个 worker 一个进程、一份字典** —— 用户在 worker A 拿到验证码，
下一个请求落到 worker B 就"验证码不存在"，登录随机失败。
这是 T-20 存在的全部理由。

## 三个语义要点（与原实现逐条对齐）

### 1. 只有**校验成功**才消费（输错不消费）
`verify_and_consume` 返回 `False` 的所有情形 —— 不存在、已用过、已过期、**码不匹配**
—— 都**不**消费。这样用户在有效期内可以改错重输；
暴力破解由 IP 失败计数兜（`CAPTCHA_MAX_ERRORS`，T-21 接手）。
若把"输错"也消费掉，打错一个字符就得刷新验证码重来，体验不可接受。

### 2. 原实现是"先读后写"，本实现在**一条 UPDATE** 里完成全部判断
```sql
UPDATE captcha_store SET used = 1
 WHERE captcha_id = :cid AND used = 0
   AND expires_at > :now AND code = :code
```
`rowcount == 1` 才算成功。这样做有两个好处：

* **原子**：判断与消费之间没有窗口，两个并发请求（哪怕在不同 worker）
  里**恰好一个**能拿到 `rowcount == 1`。原实现的"读 -> 判断 -> 写"
  在多进程下连正确性都不成立（字典各存各的），在单进程下也有 TOCTOU 缝隙。
* **少一次往返**：不需要先查再改。

### 3. 成功后置 `used = 1`，而不是 `DELETE`
DDL 里有 `used BOOLEAN NOT NULL DEFAULT 0` 列，意图显然是**保留使用痕迹**
直到 `purge_expired` 回收。原实现直接 `del`，那列传进来也没意义。
保留痕迹让"某个验证码到底有没有被用过"可查，排查登录问题时有用。

## 与原实现的一处**理论**差异（如实记录）

| | 原实现 | 本实现 |
|---|---|---|
| 过期判定 | `entry["expires_at"] < time.time()` → 严格小于 | `expires_at > :now` 才算有效 → **恰好等于**时算过期 |

即"到期那一瞬间"的行为不同。差异宽度是一个瞬间，实际不可观测，但既然不一致就写出来，
并有边界用例钉住（`test_boundary_instant_is_expired`）。选 `<=` 是为了与
T-19 会话存储的口径统一（`SessionSnapshot.is_expired` 同样把等号算过期）。

## 输入归一化

原实现比较的是 `user_code.strip()`（去首尾空白），本实现保留该行为 ——
验证码是 4 位数字，用户从图片抄录时很容易带上空格。
"""

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services.stores._sqlite_tx import begin_write
from services.stores.base import StoreError

logger = logging.getLogger("app.stores.captcha")


class SQLiteCaptchaStore(object):
    """`CaptchaStore` 协议在 SQLite 上的实现。

    构造参数 `session_factory` 语义同 `SQLiteSessionStore`：默认用
    `database.SessionLocal`（继承 T-15 的 engine 契约），测试可注入独立工厂。
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

    def save(self, captcha_id: str, code: str, expires_at: str) -> None:
        """保存一个新验证码。

        `captcha_id` 是 uuid4 十六进制串，正常不会撞；真撞了说明上游有 bug，
        因此这里**不做** `INSERT OR REPLACE`（那会把冲突然静默吞掉），
        而是抛出带 `captcha_id` 的 `StoreError`。
        """
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            s.execute(
                text(
                    "INSERT INTO captcha_store (captcha_id, code, expires_at, used) "
                    "VALUES (:cid, :code, :expires_at, 0)"
                ),
                {"cid": captcha_id, "code": code, "expires_at": expires_at},
            )
            s.commit()
        except IntegrityError as exc:
            s.rollback()
            raise StoreError(
                "保存验证码失败（captcha_id=%r 可能已存在）：%s" % (captcha_id, exc)
            )
        finally:
            s.close()

    def verify_and_consume(self, captcha_id: str, code: str, now: str) -> bool:
        """校验并**在成功时**消费。

        单条 UPDATE 完成全部判断（见模块说明），`rowcount == 1` 才算成功。
        返回 `False` 的任何情形都不消费。
        """
        normalized = (code or "").strip()
        if not captcha_id or not normalized:
            # 空输入直接拒，不产生任何写入（省一次往返，也避免把空串当匹配）
            return False

        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            result = s.execute(
                text(
                    "UPDATE captcha_store SET used = 1 "
                    "WHERE captcha_id = :cid "
                    "  AND used = 0 "
                    "  AND expires_at > :now "
                    "  AND code = :code"
                ),
                {"cid": captcha_id, "now": now, "code": normalized},
            )
            s.commit()
            return (result.rowcount or 0) == 1
        finally:
            s.close()

    def purge_expired(self, now: str) -> int:
        """删除已过期的验证码，返回删除行数（T-22 定时清理调用）。

        `expires_at <= :now` 与 `verify_and_consume` 的有效性判据
        （`expires_at > :now`）严格互补：**被判为过期的就一定清得掉**，
        否则会出现"验证时说过期、清理时又留着"的错位。
        """
        s = self._session()
        try:
            begin_write(s)   # T-15 修订：写路径显式取写锁
            result = s.execute(
                text("DELETE FROM captcha_store WHERE expires_at <= :now"),
                {"now": now},
            )
            s.commit()
            return result.rowcount or 0
        finally:
            s.close()

    # ------------------------------------------------------------------
    # 便于排查/测试的只读查询（不属于协议）
    # ------------------------------------------------------------------

    def get(self, captcha_id: str) -> Optional[dict]:
        """只读取出原始行（`captcha_id` / `code` / `expires_at` / `used`）。

        **刻意不进协议** —— 协议只暴露"校验并消费"，调用方不该能读到明文验证码。
        这个方法供测试与运维排查使用。
        """
        s = self._session()
        try:
            row = s.execute(
                text("SELECT captcha_id, code, expires_at, used "
                     "FROM captcha_store WHERE captcha_id = :cid"),
                {"cid": captcha_id},
            ).fetchone()
            if row is None:
                return None
            return {"captcha_id": row[0], "code": row[1],
                    "expires_at": row[2], "used": row[3]}
        finally:
            s.close()
