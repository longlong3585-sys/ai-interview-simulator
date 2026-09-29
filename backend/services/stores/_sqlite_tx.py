"""SQLite 写事务的**唯一入口**（T-15 修订：写路径显式取锁）。

## 背景：为什么不能全局 BEGIN IMMEDIATE

T-15 最初在每个事务的 `begin` 事件里无条件发 `BEGIN IMMEDIATE`，
理由是 ADR-004 的并发控制依赖它。后果是**连只读事务也持写锁**：

    任何经由本引擎的请求，从第一条 SELECT 起就独占写锁，直到请求结束。

单机低并发时看不出问题，直到 T-23 把存储层接进路由才炸：
`get_current_user` 先做一次 SELECT（拿锁），处理函数里再调存储层
（要写）→ 同一请求内自锁 → 等满 `busy_timeout=15s` 后
`database is locked`。**WAL 模式下"读不阻塞写"的设计初衷被彻底浪费**。

## 现在的模型

| 路径 | 事务 | 锁 |
|---|---|---|
| 读（`get` / `count_failures` / …） | 不显式 BEGIN，语句级自动提交 | 不持写锁；多读并发互不阻塞 |
| 写（`create` / `commit_turn` / …） | 写前显式 `BEGIN IMMEDIATE` | 立刻取写锁，避免"读→写升级"时的 SQLITE_BUSY |

### 为什么写路径仍然必须是 IMMEDIATE（ADR-004 的论证保留）

WAL 允许"多读 + 单写"，但**读事务不能升级为写事务**：
若一个事务先读、再想写，而期间别人提交过，SQLite 返回 `SQLITE_BUSY`
**且不调用 busy handler** —— 也就是 `busy_timeout=15000` 完全失效。
这正是 ADR-002 想消灭的 `database is locked`。

用 `BEGIN IMMEDIATE` 在事务**一开始**就取写锁，就不存在"升级"这一步：
拿不到锁时走的是正常的 busy handler（等待并重试），而不是立即失败。

### 代价（如实记录）

写事务之间仍然串行（单写者模型，SQLite 的固有限制）。
但**读不再被写挡住、写也不再被读挡住** —— 这才是选 WAL 的理由。
"""

from sqlalchemy import text


def begin_write(target):
    """显式开启写事务（`BEGIN IMMEDIATE`）。

    **所有写路径都必须在第一条写语句之前调用一次。** 三条仓储实现
    （session / captcha / rate_limit）都从这里取锁，不再各写一份。

    参数
    ----
    target:
        SQLAlchemy 的 `Session` 或 `Connection`，两者都支持 ——
        仓储内部用 Session，而测试/运维脚本往往直接用 Connection。

    为什么必须显式：engine 的 `isolation_level=None` 会让 DBAPI 进入
    自动提交模式，SQLAlchemy 不会自动发 BEGIN。不显式取锁的话，
    多语句写入**不具备原子性**（每条语句各自提交），
    而且写事务会在第一条写语句时才试着升级锁 ——
    那正是 ADR-004 要避免的 `SQLITE_BUSY` 绕过 busy handler 的场景。

    ⚠️ 为什么落到**原始 DBAPI 连接**上去执行、并先归零事务状态：
    实测踩到 `cannot start a transaction within a transaction` ——
    当 `BEGIN IMMEDIATE` 是 Session 的第一个动作时，取连接那一步
    已经把事务开起来了（执行过别的语句之后再发反而没事，所以极易漏测）。
    先 `rollback()` 到干净状态再取锁即可；SQLAlchemy 自己那层逻辑事务
    不受影响，`session.commit()` 依旧能收尾
    （pysqlite 的 commit 会查 `sqlite3_get_autocommit()` 再决定是否 COMMIT）。
    """
    conn = target.connection() if hasattr(target, "get_bind") else target
    dbapi = getattr(conn, "dbapi_connection", None)
    if dbapi is None:                       # 旧版本 SQLAlchemy 的取值路径
        dbapi = conn.connection.dbapi_connection
    if dbapi.in_transaction:
        dbapi.rollback()
    dbapi.execute("BEGIN IMMEDIATE")
