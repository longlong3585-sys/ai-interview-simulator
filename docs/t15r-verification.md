# T-15 修订 · 验证指令（BEGIN IMMEDIATE 改为「仅写路径」）

> **背景**：T-23 把存储层接进路由时暴露了一个必现死锁 —— `get_current_user` 的
> 只读事务在 T-15 的**全局** `BEGIN IMMEDIATE` 下会持有写锁直到请求结束，
> 于是同一请求内的存储层写入只能等满 `busy_timeout` 后失败
> （`database is locked`）。你裁决采用**选项 B**：把取锁从全局剥离，只放在写路径。
>
> **本文件是你验收这次修正的指令。** 每条命令与预期输出我都在本机实跑过。

---

## 0. 这次改了什么（3 处，共 4 类文件）

| 文件 | 改动 |
|---|---|
| `backend/database.py` | **删除**全局 `begin` 事件里的 `BEGIN IMMEDIATE`。`isolation_level=None` 保留（把"何时 BEGIN"的决定权拿回来） |
| `backend/services/stores/_sqlite_tx.py`（新） | `begin_write(target)` —— **写事务的唯一入口**，接受 Session 或 Connection |
| 三个仓储的 **12 个写方法** | 在第一条写语句前显式调用 `begin_write`（读方法一律不加） |
| `backend/tests/support.py` | 测试 engine **同步**去掉全局监听器（否则测试跑的是一套与生产不同的并发语义，还会掩盖上面那个死锁） |
| `backend/tests/test_sqlite_engine_config.py` | 判别性测试重做（见 §3） |

**读方法清单（刻意不加锁，这是本修订的重点）**：
`get` / `get_active` / `find_replay` / `count_failures`。

**写方法清单（显式取锁）**：
`create` / `commit_turn` / `finish` / `abandon` / `abandon_expired_for_user` /
`abandon_all_expired` / `save` / `verify_and_consume` / `purge_expired` /
`record_failure` / `clear` / `purge_older_than`。

---

## 1. 验收 1：自动化测试（零风险，约 80 秒）

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'

# T-15 专项（含本次重做的并发语义用例）
.\venv\Scripts\python.exe -m unittest tests.test_sqlite_engine_config -v

# 全量
.\venv\Scripts\python.exe -m unittest discover -s tests -t .
```

**期望**：专项 `Ran 18 tests` → `OK`；全量 `Ran 438 tests` → `OK`。

---

## 2. 验收 2：亲手证明「读不再持写锁、写仍然持写锁」

这是本次修正的**核心行为**，用一条命令直接观察（不经过测试框架）：

```powershell
.\venv\Scripts\python.exe scripts\probe_t15r.py
```

**期望输出（本机实跑结果，逐字）**

```
临时库: ...\t15r-XXXX\probe.db

======================================================================
1) 只读事务是否仍持写锁？（修订后应当 **不持**）
======================================================================
  [PASS] 只读期间另一连接能写入（读不持写锁）

======================================================================
2) 显式 begin_write 之后呢？（应当 **持锁**）
======================================================================
  [PASS] 写事务期间另一连接被挡住（写保护仍在）

======================================================================
3) 提交后写锁是否释放？
======================================================================
  [PASS] 提交后另一连接能写入（锁已释放）

  共 3 项，通过 3 项，失败 0 项

  结论: 全部通过 [ALL PASS]
```

**三项的判读**：
- 第 1 项若失败 → **修订没生效**（读还持写锁，T-23 的死锁会复现）
- 第 2 项若失败 → **写保护丢了**（ADR-004 的并发控制失效，比死锁更危险）
- 第 3 项若失败 → 锁没释放，后续请求会一直排队

> 该脚本只用**临时库**，不碰 `backend/interview.db`。

---

## 3. 验收 3：判别性测试重做（你要的"重做 T-15 判别性测试"）

旧的两条用例断言的是**旧行为**（"只读事务持写锁"、"并发事务串行化"），
本次**反转**并补齐。新用例与它们各自能抓住什么：

| 用例 | 断言 | 缺陷被注入时会失败 |
|---|---|---|
| `test_concurrent_reads_do_not_block_each_other` | 三个并发只读连接互不阻塞，且期间他人能取写锁 | 一旦恢复全局 IMMEDIATE → 失败 |
| `test_read_transaction_does_not_block_a_writer` | 只读期间另一连接能写入 | 同上（这是 T-23 死锁的直接探针） |
| `test_explicit_write_lock_blocks_other_writers` | `begin_write()` 之后另一写者被挡 | 删掉 `begin_write` 调用 → 失败 |
| `test_begin_write_takes_the_write_lock_immediately` | 同上，Connection 形态 | 同上 |
| `test_write_lock_is_released_after_commit` | 提交后写锁释放 | 不调 `commit()` → 失败 |
| `test_begin_write_works_as_the_very_first_statement` | `begin_write` 作为 Session 首条动作不得报错 | 去掉内部的事务归零 → 报 `cannot start a transaction within a transaction` |
| `test_reads_still_see_wal_concurrency` | WAL 下 RESERVED 锁不阻塞纯读（防改坏） | — |

自己复核判别力（**可选**）：把 `begin_write` 改成空操作，或把全局 `begin`
事件加回 `database.py`，然后重跑 §1 的专项测试 —— 上表中打 ✔ 的用例应当变红，
`test_concurrent_reads_do_not_block_each_other` 与
`test_explicit_write_lock_blocks_other_writers` 会直接失败。
改完记得还原（这两处都在 git 跟踪范围内，`git checkout --` 即可）。

---

## 4. 验收 4：原作者场景 —— T-23 的死锁确实没了

这是修订的**唯一目的**。用同一进程内"鉴权读 + 业务写"的模式复现：

```powershell
.\venv\Scripts\python.exe scripts\probe_t15r.py --deadlock-scenario
```

**期望输出（本机实跑结果，逐字）**

```
======================================================================
T-23 场景：同一「请求」内先读用户、再调存储层写
======================================================================
  读用户（模拟 get_current_user）      : ok (id=1)
  紧接着写会话（模拟 store.create）    : ok (session=t15r-1)
  [PASS] 同请求内『鉴权读 + 业务写』不再自锁

  共 1 项，通过 1 项，失败 0 项

  结论: 全部通过 [ALL PASS]
```

**修复前的表现**：第二步行会阻塞约 15 秒（`busy_timeout`）后抛
`sqlite3.OperationalError: database is locked`。

---

## 5. ADR 层面的重新论证（你的要求 #2）

`docs/02-architecture-v2.md` 的 **ADR-002R** 已追加修订条目，要点：

> **ADR-004 的"读→写升级"论证依然成立，但适用对象收窄到写路径。**
> WAL 允许"多读 + 单写"，但**读事务不能升级为写事务**：若一个事务先读、
> 再想写，而期间别人提交过，SQLite 返回 `SQLITE_BUSY` **且不调用 busy handler**
> —— `busy_timeout=15000` 完全失效，正是 ADR-002 要消灭的 `database is locked`。
> 用 `BEGIN IMMEDIATE` 在事务**开始前**取写锁就没有"升级"这一步，
> 拿不到锁时走正常的 busy handler。
>
> **修订点**：这条论证只对**写路径**成立。原先把它套用到全部事务，
> 代价是"只读也持写锁"——既浪费了 WAL 的读并发，又制造了
> "同一请求内鉴权读 + 业务写自锁"的死锁（T-23 实测）。

---

## 6. 已知代价与边界（如实说明）

| 项 | 说明 |
|---|---|
| 写之间仍串行 | SQLite 单写者模型，`BEGIN IMMEDIATE` 之间互斥。这是固有限制，不是本修订引入的 |
| 多语句写的原子性 | 依赖写方法**显式**调用 `begin_write`；漏调会让该方法的多次写退化为逐条自动提交。三个仓储的写方法已全部覆盖，并有清单式测试守住（见 §3 的用例 3/4） |
| `begin_write` 的取值路径 | 走原始 DBAPI 连接，并先把可能已存在的事务归零 —— 原因见 `_sqlite_tx.py` 的 docstring（首条语句直接 `BEGIN` 会报 `cannot start a transaction within a transaction`） |
| **T-23 尚未接线** | 本轮按你的要求"确认修复后再继续 T-23"，因此 T-23 的路由改动**已回退**，代码里仍是原先的内存字典。修订验收通过后我再接着做 |

---

## 7. 回滚

本轮改动只涉及后端代码，无数据库结构变更、无迁移。回滚：

```powershell
cd 'D:\AI 驱动的智能面试准备与模拟系统'
git revert <本次 commit>
```

回滚后 `BEGIN IMMEDIATE` 会恢复为全局行为 —— **T-23 的死锁也会随之回来**，
所以回滚只应作为"修订本身有问题"时的临时手段。
