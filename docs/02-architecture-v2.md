# 02 · 架构设计 v2（增量修订）

> **性质**：**增量修订**，不是重写。基准为 `docs/02-architecture.md` **v2.1**（26 条 ADR），本文只记录**因外部 Bug 反馈与用户裁决而产生的差异**。
> **需求依据**：`docs/01-spec.md` **v2.4**（76 条需求）｜**反馈来源**：`docs/05-issues-backlog.md`
> **日期**：2026-09-28
>
> ## ✅ 审批状态：**本轮审批全部通过**（2026-09-28，用户人工裁决）
> **ADR-004R 已批准：采用选项 A（SQLite + WAL）** —— 理由：项目为**单机**阿里云 ECS 部署，本轮**不需要多机扩展**，无需引入 Redis。
> 随之批准：`services/stores/` 抽象层与逃生舱阈值、ADR-022R、ADR-007R、ADR-025R、ADR-024R、§4 DDL。
>
> ### 🔒 第二道门禁（当前生效）
> **数据库相关任务在 `docs/03-tasks.md` 任务清单获得人工审批前，仍标记为 `🔒 待启动`，不得开工。**
> 冻结范围：`backend/database.py`、engine 配置、`SessionLocal` 生命周期、任何 `create_engine` / PRAGMA / 连接池参数、任何建表语句、`services/stores/` 与 Alembic 迁移。
> **本文档的角色是"已完成审批的决策记录"，实施启动以任务清单审批为准。**

---

## 1. 本轮修改点总览

| # | 修改点 | 类型 | 影响 ADR / 章节 | 是否需审批 |
|---|---|---|---|---|
| **M-1** | **会话/验证码/限流存储载体选型** | 🔴 **关键决策** | **ADR-004R（新增）** | ✅ **必须审批** |
| M-2 | 超时后会话置 `abandoned` 并**释放唯一锁**，允许立刻重开 | 行为变更 | ADR-022R | ✅ 需审批（**T-28 已实现**，见 §3.2 的"T-28 实现注记"） |
| M-3 | 超时报告评分口径：未答题**不计零分** + 前端标注 | 业务规则 | ADR-007R | ✅ 需审批 |
| M-4 | 错误呈现统一 **Toast**，**禁用 `alert`/`confirm`/`prompt`** | 前端架构 | ADR-025R | ✅ 需审批 |
| M-5 | 报告契约新增 `ended_reason`；报告改以服务端会话为准 | 契约变更 | ADR-024R | ✅ 需审批 |
| M-6 | `interview_sessions` 增 `ended_reason` 列 | DDL | §4 | 随 M-1 一并审批 |
| M-7 | NFR-10 提级 P0（绝对地址导致生产全站按钮失效） | 优先级 | ADR-005/ADR-011 | 否（已在 spec 落实） |
| M-8 | 死代码清理范围确认（`react-router-dom` 启用、`QuestionBank.tsx` 处置） | 技术债 | ADR-005 | 否 |
| M-9 | **测试运行方式：PyPI 不可达 → 以 stdlib `unittest` 兜底** | 环境约束 | **ADR-021R** | 已记录（§3.5） |

**未受影响的 ADR**：ADR-001、002、003、008、009、010、012~021、023、026 —— **保持不变**，仍以 `02-architecture.md` v2.1 为准。

---

## 2. ✅ ADR-004R：会话 / 验证码 / 限流存储载体【已批准：选项 A】

> **审批结果（2026-09-28）**：**批准选项 A（SQLite + WAL）**。项目采用单机阿里云 ECS 部署，本轮不需要多机扩展，无需引入 Redis。
> 同时采纳 `services/stores/` 抽象层，认可 §2.8 的逃生舱触发阈值。
>
> 本条**取代** `02-architecture.md` 中 ADR-004 的"载体"部分（其**并发控制设计**仍然有效，见 §2.9）。

### 2.1 背景与硬约束

**需要持久化的四类状态**（当前全部存于进程内存，多 worker / 重启即失效）：

| 状态 | 数据量级 | 生命周期 | 写频率 | 丢失后果 |
|---|---|---|---|---|
| 面试会话 | 每活跃用户 1 行，字段含 3 个 JSON 列 | 2 小时 | 每答一题 1 次 | **用户面试进度全丢**（严重） |
| 验证码 | 每次请求 1 行 | 300 秒 | 每次取验证码 1 次 | 用户重新取码即可（轻微） |
| 登录失败限流 | 每次失败 1 行 | 10 分钟 | 仅失败时写 | 限流失效（中等，安全） |
| 令牌黑名单 | 每次登出 1 行 | 8 小时 | 仅登出时写 | 无法吊销（中等，安全） |

**部署硬约束**（来自 `02-architecture.md` ADR-002/006）：
- 生产为**单机**阿里云 ECS（2C2G），`systemd` 托管 `uvicorn --workers 2`
- 单人维护，无专职运维
- **写并发极低**：日均面试个位数；峰值并发写请求数 ≈ 个位数

### 2.2 选项 A：SQLite + WAL（扩展既有数据库）

**做法**：新增 4 张表到现有 `interview.db`；开启 `journal_mode=WAL` + `busy_timeout=15000` + `foreign_keys=ON`；由 `services/` 层封装读写；systemd timer 定时清理。

| 维度 | 评估 |
|---|---|
| **写入模型** | WAL 允许"多读并发 + 单写串行"。因 AI 调用已在事务外，写事务仅 1 条 `UPDATE`（**<50ms**），理论写吞吐数百/秒，远超需求 |
| **持久性** | ✅ **天然持久**。与业务数据同库同文件，进程崩溃/重启/断电后会话不丢 |
| **多进程** | ✅ 支持同机多 worker（这是 ADR-006 的目标）；❌ **不支持多机**（需共享文件系统） |
| **原子性** | ✅ 与业务数据（面试记录、通知）**同一事务**可保证一致性（如"报告落库 + 会话置 finished"原子提交） |
| **备份** | ✅ 现有 `sqlite3 .backup` 脚本**天然覆盖**会话数据，零新增 |
| **失败模式** | 与既有业务库**同生共死**，不引入新的独立故障点 |

**人力成本（估算，单人开发）**

| 工作项 | 人天 |
|---|---|
| Alembic 迁移（4 新表 + 3 处既有表变更） | 0.5 |
| engine 配置（WAL / busy_timeout / foreign_keys / `isolation_level=None` / BEGIN IMMEDIATE 事件） | 0.5 |
| 会话服务层：内存 dict → DB（含乐观锁 `version` + `seq` 幂等 + 状态机） | 2.0 |
| 验证码 + 限流改造（仅失败计数） | 1.0 |
| 清理任务（`scripts/cleanup.py` + systemd timer） | 0.5 |
| 测试（并发读写、锁竞争、幂等、重启恢复） | 1.5 |
| **合计** | **≈ 6 人天** |

**部署复杂度**：**零新增组件**。无新进程、无新端口、无新配置文件。仅需一条 `cleanup.timer`（systemd 自带能力）。
**维护成本**：**低**。无新监控项；备份沿用现有脚本；无新版本升级负担。**日常新增维护动作 = 0。**

### 2.3 选项 B：直接上 Redis

**做法**：部署 `redis-server`，`requirements.txt` 增 `redis` 客户端，会话/验证码/限流全部改为 Redis 读写，靠 `EXPIRE` 做 TTL。

| 维度 | 评估 |
|---|---|
| **写入模型** | ✅ 无锁竞争，性能远优于 SQLite |
| **TTL** | ✅ 原生 `EXPIRE`，**可省掉清理任务**（本项目的唯一实质优势） |
| **持久性** | ⚠️ **默认不持久**。RDB 快照会丢最近写入；**必须显式配置 AOF（`appendfsync everysec`）**才能达到与 SQLite 相当的"会话不丢"保证 |
| **多进程 / 多机** | ✅ 同机多 worker 与**未来多机横向扩展**均支持（这是相对 A 的唯一结构性优势） |
| **原子性** | ❌ **无法与业务数据同事务**。"报告落库（SQLite） + 会话置 finished（Redis）"变成跨存储的**分布式一致性问题**，需补偿逻辑或接受最终一致 |
| **备份** | ❌ 需**额外**备份策略（RDB/AOF 文件），与现有 SQLite 备份脚本是两套 |
| **失败模式** | 🔴 **引入新的单点故障**：Redis 挂 → 验证码、限流、会话**全部不可用 → 应用整体不可用** |

**人力成本（估算，单人开发）**

| 工作项 | 人天 |
|---|---|
| 服务层改造（接口与 A 相同，但换 Redis 客户端 + 序列化 + 连接管理 + 健康检查） | 3.0 |
| Redis 部署与配置（systemd unit、`redis.conf`：bind/`requirepass`/`maxmemory`/淘汰策略/AOF） | 1.5 |
| 持久化与备份方案（AOF 调优 + 独立备份脚本 + 恢复演练） | 0.5 |
| 监控告警（内存、连接数、淘汰次数、命中率） | 0.5 |
| 本地开发 + CI 环境需引入 Redis（Docker 或本机安装），开发者体验成本 | 0.5 |
| 单点故障降级策略与故障演练（Redis 不可用时的行为定义） | 1.0 |
| **合计** | **≈ 7 人天**（一次性） |

**部署复杂度**：🔴 **显著增加**。新增 1 个常驻服务 + 1 个端口（6379，须限制内网/加密码）+ 1 份配置文件 + 1 个 systemd unit + 内存上限规划（2C2G 机器上还要与 uvicorn 争内存）。
**维护成本**：**中高**。需持续关注：内存增长与淘汰策略、AOF 重写与磁盘占用、版本升级与安全补丁、**新增的告警与值班面**。**日常新增维护动作 > 0。**

### 2.4 选项 C：混合（会话走 SQLite，验证码/限流走 Redis）

**做法**：持久且需事务的会话留 SQLite；短 TTL、高频、可丢失的验证码/限流放 Redis。

- **优点**：让每种存储做它最擅长的事；会话保持事务一致性；验证码享受原生 TTL。
- **缺点**：**两套存储都要做**（A 的会话改造 + B 的 Redis 部署），一次性成本 ≈ **8 人天**；**仍然引入 Redis 及其单点故障**；且本项目验证码/限流量级极小，把这点负载分出去**收益几乎为零**。
- **结论**：**不推荐**。它付出了 B 的全部运维代价，只为省掉一个 0.5 人天的清理任务。

### 2.5 三维度成本对比

| 维度 | A · SQLite + WAL | B · Redis | C · 混合 |
|---|---|---|---|
| **一次性人力成本** | **≈ 6 人天** | ≈ 7 人天 | ≈ 8 人天 |
| **新增依赖** | 无（Alembic 已在计划内） | `redis` 客户端 + `redis-server` 服务 | 两者 |
| **部署复杂度** | **零新增组件** | 新服务 + 端口 + 配置 + systemd unit + 内存规划 | 同 B |
| **日常维护成本** | **0 新增动作**（备份沿用现有） | 内存/淘汰/AOF/升级/告警 | 同 B |
| **持久性** | ✅ 天然 | ⚠️ 需配 AOF 才等价 | 会话 ✅ / 临时数据 ⚠️ |
| **与业务数据同事务** | ✅ 支持 | ❌ 不支持（跨存储一致性） | 部分 |
| **多机横向扩展** | ❌ 不支持 | ✅ 支持 | 部分 |
| **新增单点故障** | ✅ 无 | 🔴 有（Redis 挂 = 全站挂） | 🔴 有 |
| **性能上限** | 足够（写 <50ms，需求为个位数并发） | 过剩 | 过剩 |

### 2.6 决策矩阵

| 判据 | 权重 | A | B |
|---|---|---|---|
| 满足当前需求（多 worker + 重启不丢会话） | 高 | ✅ | ✅ |
| 满足**未来 12 个月**需求（单机、单人维护、日均个位数） | 高 | ✅ | ✅（过剩） |
| 一次性成本 | 中 | ✅ 更低 | ❌ |
| 部署复杂度 | **高** | ✅ **零新增** | ❌ 显著增加 |
| 日常维护成本 | **高** | ✅ **0 新增** | ❌ |
| 故障面（新增单点） | **高** | ✅ **无** | ❌ **有** |
| 数据一致性（跨存储事务） | 中 | ✅ | ❌ |
| 多机扩展 | 低（当前无此需求） | ❌ | ✅ |

### 2.7 推荐：**选项 A —— SQLite + WAL**

**理由（按重要性排序）**

1. **不引入新的单点故障（决定性理由）**。Redis 一旦不可用，验证码、限流、会话全部失效 → **整个应用不可用**。而当前架构里 SQLite 是应用**本来就必须依赖**的组件，会话放进同一个文件**不增加任何新的故障面**。对单人维护的系统，"少一个会坏的东西"的价值高于性能余量。
2. **维护成本差一个量级**。A 的日常新增维护动作是 **0**；B 需要长期照看内存、淘汰策略、AOF 重写、版本升级与告警。用户明确要求评估"维护成本"，这一项 A 完胜。
3. **持久性开箱即得**。面试会话必须跨重启存活。SQLite 天然满足；Redis 必须额外配置 AOF 才等价，**默认配置下会丢会话**——而这正是本轮要修的 Bug 2。
4. **可与业务数据同事务**。"报告落库 + 会话置 finished/abandoned"必须在同一事务（ADR-004 已定），SQLite 直接支持；Redis 会把它变成跨存储一致性问题。
5. **性能余量充足**。写事务 <50ms、并发为个位数，SQLite 的"单写者"模型**远未成为瓶颈**。Redis 的性能优势在本项目**没有可兑现的场景**。
6. **多机扩展是伪需求**。当前部署明确是单机 ECS；为"未来可能的多机"提前付出运维代价不符合增量迭代原则。

**✅ 同时接受 A 的已知代价（诚实声明）**：
- **不支持多机横向扩展**（见 §2.8 逃生舱）
- **写操作串行化**：若未来有开发者把 AI 调用误放回写事务，会重新引发 `database is locked` → 需以测试与代码评审守住（ADR-021 已含并发测试）

### 2.8 风险与逃生舱（何时应切到 Redis）

为降低"选错"的代价，**无论最终选 A 还是 B，都要求先把存储访问抽象为接口**（见下），使未来切换只改一个实现类。

**建议先落地 A，并在以下任一条件出现时**重启本 ADR 迁移到 Redis：

| 触发条件 | 建议阈值 |
|---|---|
| 部署形态变化 | 需要**多台**应用服务器（A 直接不可用） |
| 写竞争 | 日志出现 `database is locked` 的频率 > **每日 1 次**（且 busy_timeout 已 15s） |
| 写量增长 | 峰值写请求 > **50/秒**（当前为个位数） |
| 会话规模 | 同时活跃会话 > **1000** |

**抽象接口（无论选哪个都建议做）**：
```
services/stores/
├─ base.py        # SessionStore / CaptchaStore / RateLimitStore 协议定义
├─ sqlite_store.py    # 选项 A 的实现
└─ redis_store.py     # 选项 B 的实现（未来需要时新增）
```
调用方只依赖协议 → 切换成本从"重写业务逻辑"降为"新增一个实现类 + 改一行装配"。

### 2.9 门禁与保留项

- 🔴 **在 ADR-004R 获批前，禁止修改任何数据库连接逻辑**（`database.py`、engine 配置、PRAGMA、连接池、建表语句）。
- ✅ **ADR-004 的并发控制设计继续有效且必须保留**（与载体选择无关）：AI 调用移出事务、乐观锁 `version`、`UPDATE` 覆盖全部变更列、`seq` 幂等前置去重、冲突返回 409、`WHERE status='active' AND expires_at > :now`。
- ✅ **ADR-002 的 SQLite 配置契约条件生效**：仅当选 A 时需落地（WAL / `busy_timeout=15000` / `foreign_keys=ON` / `isolation_level=None` + `BEGIN IMMEDIATE` 事件）。
- ⚠️ **选 B 的连带影响**：ADR-006 的部署拓扑、ADR-019 的备份策略、ADR-026 的清理任务**均需同步重写**。

---

## 3. 受 Bug 反馈影响的 ADR 修订

### 3.1 ADR-022R · 超时闭环与会话释放（修订 ADR-022）

**变更点**（依 `05-issues-backlog.md` Bug 3B + 用户裁决）：

| 项 | v2.1 原设计 | **v2 修订** |
|---|---|---|
| 超时后会话状态 | 未明确（笼统写"置 `finished`/`abandoned`"） | **明确置 `abandoned`**（不是 `finished`——面试并未正常完成） |
| 唯一锁处理 | 未明确 | **置 `abandoned` 即自动释放** `UNIQUE(user_id) WHERE status='active'` 的部分唯一索引 → **用户可立刻重开新面试** |
| 用户被锁风险 | 曾担忧"修完 Bug 3B 变成锁死用户" | ✅ 已消除：`abandoned` 释放锁 + `abandon` 接口 + 409 带会话摘要 |

**状态机（最终版）**
```
active ──报告生成成功──────────▶ finished     （释放锁）
   │
   ├──15 分钟超时归零───────────▶ abandoned    （释放锁，仍生成报告，见 ADR-007R）
   ├──用户点击「放弃并重开」─────▶ abandoned    （释放锁）
   └──expires_at 到期 ──────────▶ abandoned    （释放锁，惰性 + timer）
```
**关键不变量**：任何非 `active` 状态都**不占用**唯一锁 → 用户永远能开新面试。

### 3.2 ADR-007R · 超时报告的评分口径（修订 ADR-007）

**用户裁决**：超时后**仍要出报告**，但：
1. 前端必须**显式标注**"因超时自动结束，仅基于已答部分评分"；
2. **未作答的题目不得按零分处理**。

**架构含义（这是本条的重点——现有评分提示词会违反该裁决）**：
- 现有 `generate_report` 的提示词含"**如果候选人对你提出的问题一个都没有给出有效回答…则所有分数均为 0**"（`interview.py:204-205`），且把 `question_status` 全量喂给模型影响打分。**超时场景下，未答题是"未及作答"而非"答不上"，必须区分**。
- **实现要求**：
  - 评分提示词须新增明确指令：`status='pending'` 的题目属**因超时未及作答**，**不得计入扣分**；`answered` 与 `skipped` 仍按现行规则计分；
  - 报告新增 **`ended_reason`** 字段（`completed` / `timeout` / `manual`）；
  - 当 `ended_reason='timeout'` 且 `answered_count=0` 时，**不得**直接给全 0 分，而应标注"未及作答，无法评分"（避免 FR-5.2 想修的"误导性 0 分"以另一种形式复现）。
- **与 FR-5.2 的关系**：两者共同保证"评分失败/未作答"都不被伪装成"真实低分"。

**T-27 实现注记（2026-09-30，不改裁决，只补落地细节）**：

1. **超时会话的报告路径**：ADR-022R 要求超时 → `abandoned`，ADR-007R 要求超时仍要出报告。
   两条同时成立意味着 **`abandoned` + `report` 非空是一个合法组合** ——
   报告与状态是**两个正交的事实**（`status` 说明怎么结束的，`report` 说明评分算过没有）。
   因此存储层新增两个方法（T-16 协议第 2 次修正，第 1 次见 T-19）：
   - `get_last_ended(user_id)`：取最近结束的一场。**必要性**：超时后 `get_active()` 永远
     返回 `None`，`generate_report` 修复前只能 409 —— "超时不出报告"在存储层的根因就在这里。
   - `attach_report(session_id, expected_version, report_json, now)`：**只写报告、不动状态**，
     条件 `status='abandoned' AND report IS NULL AND version=:expected`。
     条件里的 `report IS NULL` 是**防覆盖**守卫：重算要多花一次 AI 调用，还会覆盖用户看过的结论。
   - **不**复用 `finish()`：它会把状态置 `finished`，等于用报告路径污染状态语义
     （库里会出现"用户从未完成的面试被记成完成"）。
2. **`ended_reason` 与两个计数由服务端裁决**：模型输出会被覆盖，客户端自报一律无效
   （否则前端说一句"我超时了"就能换到按宽松口径打的分数）。零作答超时的
   "未及作答，无法评分"标注同样由服务端**兜底补齐** —— 它是 MUST，不能只写在提示词里赌模型遵守。
3. **标注条件比本条原文收窄一处**：原文写 `ended_reason='timeout'` 且 `answered_count=0`；
   实现额外要求 `skipped=0`。因为"全部主动跳过"也让 `answered_count=0`，但那是候选人
   **明确拒绝回答**，贴上"未及作答，无法评分"就成了**反向误导**（把放弃说成没机会）。
   这是对"未及作答"本义的收紧，不改变裁决意图。
4. **实现边界（T-28 的前置）**：本任务只保证"会话一旦处于 `abandoned(timeout)`，
   报告就是 `timeout` 口径"。**服务端在 15 分钟到点时主动把会话置为 `abandoned` 属 T-28**；
   在 T-28 接线前，若前端计时器先到点而服务端会话仍是 `active`，那份报告仍会被标成 `completed`
   —— 这一点已在 `docs/27-manual-verification.md` 的"已知边界"中明示。

**T-28 实现注记（2026-09-30，落实 FR-4.12 的"服务端同步兜底"，接上 T-27 的第 4 条边界）**：

1. **超时时刻的归属**：死线 = 会话行 `created_at`（服务端写入的列，客户端不可伪造）
   + `INTERVIEW_DURATION_SECONDS`（`config.py`，默认 15 分钟，可用环境变量覆盖以便验收）。
   判定方法 `SessionSnapshot.interview_deadline()` / `is_timed_out()` 落在**数据模型**层
   （`services/stores/base.py`），**存储层零改动、无需迁移**。
2. **为什么必须新增常量而不是复用 `expires_at`（2h TTL）**：`expires_at` 是 ADR-022 的
   **锁卫生**上限（回答"这行数据还值不值得当成活跃会话"），业务死线回答"这场面试还允许
   答题吗"。把两者压成一个字段必然二选一地制造事故：要么允许用户答 2 小时，
   要么一次刷新/断网就让 15 分钟的面试作废。（T-22 曾因把 TTL 多减一次而把
   ADR-022R 的承诺废掉，同类教训。）
3. **兜底是惰性判定，且必须覆盖每一个会话入口**：`routers/interview.py` 的
   `_enforce_interview_timeout()` 在 `/api/chat`、`/api/skip_question`、
   `GET /api/interview/session`（刷新即结算）、`/api/interview/abandon`
   （到点后才点"放弃"的，`ended_reason` 记 `timeout` 而非 `manual`）、
   `/api/generate_report`（**必须在判定 `ended_reason` 之前**跑，否则超时面试会被
   报告路径判成 `completed` + `finished`）、`/api/start_interview`（插入前释放，
   兑现 ADR-022R"不得把用户锁死在门外"）六处调用。
   只拦 `/api/chat` 会留下同型漏洞：换一条写路径（skip）或换一个理由（报告）即可绕过。
4. **接口附加项（均为附加，不破坏既有契约）**：到点后的 409 带
   `code="interview_timeout"`（与 `no_active_session` 明确区分）+ `ended_reason="timeout"`
   + `actions` + 会话摘要；`GET /api/interview/session` 增加 `last_ended`
   （超时后 `session=null`，前端据此给出 FR-4.12 第③条要求的**明确反馈**）；
   `/api/interview/config` 与 `session` 下发 `duration_seconds` / `deadline_at` /
   `interview_remaining_seconds`，使前端倒计时以**服务端**死线为准（不再硬编码 15 分钟）。
5. **与定时清理（T-22）的分工**：`cleanup.py` 继续只管 2 小时 TTL。业务死线要求
   "用户一点下去就立刻被拦住"，那是惰性兜底的职责；定时器最小粒度是分钟级，
   靠它兜底意味着超时后还能再答一小时。

### 3.3 ADR-025R · 错误呈现统一（修订 ADR-025）

**用户裁决**：统一用 **Toast**，**禁止 `alert`**。

| 项 | v2.1 原设计 | **v2 修订** |
|---|---|---|
| 崩溃兜底 | `ErrorBoundary` | 保留 `ErrorBoundary` |
| 错误提示 | 仅笼统写"统一错误组件" | **明确统一 Toast 组件**（`ToastProvider` + `useToast()`），支持 success/error/info，可被自动化测试断言 |
| 原生对话框 | 未禁 | 🔴 **明令禁止 `alert` / `confirm` / `prompt`** |
| 连带改造 | 未涉及 | **`confirm()` 2 处**（管理端删除确认 `App.tsx:154,269`）与 **`prompt()` 1 处**（重置密码 `:248`）**必须替换为模态确认组件** |

**理由**：`alert`/`confirm`/`prompt` 是**阻塞式**且**无法被自动化测试断言**，与 NFR-6（每个 P0/P1 至少一条可执行测试）直接冲突。

**新增组件**：`ConfirmDialog`（Promise 化，`await confirmDialog(...)`），替换全部原生确认。

### 3.4 ADR-024R · 契约变更补充（修订 ADR-024）

在本轮 13 处契约变更基础上，**追加 3 处**（共 **16 处**）：

| 序号 | 变更 |
|---|---|
| ⑭ | `generate_report` **请求体不再传 `messages`**，改为以服务端会话为准（Bug 3A） |
| ⑮ | 报告响应新增 **`ended_reason`**（`completed`/`timeout`/`manual`） |
| ⑯ | 渲染层禁止原生对话框，改由 Toast / `ConfirmDialog` 承载（属前端内部契约，但影响 E2E 测试写法） |

---

### 3.5 ADR-021R · 测试运行方式（修订 ADR-021）

**背景（T-02 实测发现）**：本机 **PyPI 不可达** —— DNS 解析正常、`pypi.org:443` TCP 可通，但 **TLS 握手被重置**；注册表中配置的本地代理 `127.0.0.1:7897` 已失效（端口无监听，仅残留 `FIN_WAIT_2`）。因此 **pytest 无法安装**（`pip install pytest` 挂起至超时，venv 内 44 个包中无 pytest）。

| 项 | ADR-021 原设计 | **ADR-021R 修订** |
|---|---|---|
| 单元测试框架（后端） | pytest + FastAPI TestClient | **主框架仍为 pytest（未来）**；当前以 **stdlib `unittest`** 运行 |
| 组件测试（前端） | Vitest + React Testing Library | **不变（待激活）**；当前以 **零依赖契约测试（`node:test`）** 运行 |
| 运行命令（后端） | `pytest` | **`cd backend && python -m unittest discover -s tests -t . -v`** |
| 运行命令（前端） | `vitest` | **`cd frontend && npm run test:node`**（`npm run test` 保留给 Vitest） |
| 测试库 | 独立文件 SQLite（非内存） | **不变**（已实现，见下） |

**⚠️ 更正一处预测错误**：本 ADR 初稿（T-02 时）曾写"前端 Vitest 不受 PyPI 影响，Node 侧依赖已装齐，应当能正常安装运行"。**该判断在 T-03 被实测推翻**：

- **npm registry 同样不可达**（与 PyPI 同一故障模式：DNS 解析正常、`registry.npmjs.org:443` TCP 可通，但 **TLS 握手被重置**）；
- `node_modules` 的 170 个包中**无任何测试运行器**（无 vitest / jest / mocha / ava，也无 jsdom / happy-dom / esbuild）；
- 本机虽有 **pnpm 12.4.2** 与 **1.9 GB 的 pnpm store**（`@localappdata%\pnpm\store\v11`），其中**确实缓存了** `vitest@4.1.8`、`jsdom@29.1.1`、`@testing-library/react@16.3.2`、`@testing-library/dom@10.4.1`、`happy-dom@20.11.6`，但**不含 `vite` 本身**（该 store 来自另一个 Vue 项目）。`pnpm install --offline` 仍需解析 `vite` 等传递依赖 → **离线安装不可行**。
- **未采用的方案**：把项目切换到 pnpm 以复用该 store —— 这会引入 `pnpm-lock.yaml`、改变 `node_modules` 结构，属**未被请求的包管理器变更**，收益不抵风险，故不做。

**前端的兼容性做法**：
- 契约测试写成 `tests/**/*.test.mjs`（`node:test`），可用 `npm run test:node` **立即运行**；
- Vitest 配置与示例组件测试写成 `vitest.config.ts` + `tests/setup.ts` + `tests/example.test.tsx`，**待激活**；
- **刻意全部放在 `src/` 之外** —— `tsconfig.app.json` 的 `include` 是 `["src"]`，若放进 `src`，`npm run build`（`tsc -b`）会因缺少 vitest 类型而失败。已实测：加入这些文件后 `tsc -b` 与 `vite build` **均 EXIT=0**。

→ **网络恢复后，前端只需 `npm install -D vitest @testing-library/react @testing-library/dom jsdom`，无需改动任何测试代码。**

**T-02 已落地的环境隔离机制（后端，本轮核心交付）**：
- `tests/__init__.py` 在**导入任何应用模块之前**把 `DATABASE_URL` 指向临时文件库。
  依据：`database.py:4` 的 `load_dotenv()` **默认 `override=False`**，故先设置的环境变量不会被 `.env` 覆盖。
- `tests/test_environment_isolation.py` 把"隔离是否真的生效"变成**可执行断言**：
  写入探针后比对真实库的 `(mtime, size)` 指纹，若有变化即判失败。
  这防止了未来"跑一次测试就改写线上数据"的事故。

**影响**：NFR-6 的验收方式不变（"可执行测试 + 可跑"），仅运行器不同。

**T-03 前端契约测试已覆盖的内容**（零依赖，7 项全绿）：
- 跳过词列表前端 ↔ 后端**完全一致**（守 FR-4.10 的"双份硬编码易漂移"风险）
- 跳过词无重复/空项；数量快照 23
- 前端每个 API 路径在后端均有对应路由（守"运行期 404 而类型系统无感"的隐患）
- **匹配器判别力自检**：断言不存在的路径匹配不上，防止"什么都匹配"导致假通过
- 除 `config.ts`（Bug 4 / T-34 的修复范围）外，源码不得硬编码 `127.0.0.1`

**负向验证已执行**：临时在 `App.tsx` 植入一个不存在的 API 调用 → 测试由 7 通过变为 **1 失败并精确报出该路径**；还原后恢复全绿，`App.tsx` 干净还原。

---

### 3.6 ADR-009R · 迁移机制：自建轻量 runner（修订 ADR-009）

**背景（T-14 实测）**：ADR-009 原计划「引入 Alembic」。但本机 **PyPI 与清华/阿里/中科大/腾讯 4 个镜像全部 TLS 被重置**，`alembic` / `Mako` / `MarkupSafe` **均无法安装**（venv 无、requirements 无、pip 缓存无）。网络是白名单式的——只有 `api.deepseek.com` 可达。

**用户裁决（2026-09-29）**：**采用选项 B —— 自建轻量迁移机制**，并要求先出方案再动代码。方案已确认。

| 项 | ADR-009 原设计 | **ADR-009R 实际实现** |
|---|---|---|
| 工具 | Alembic | **自建** `backend/migrations/` + `scripts/migrate.py` |
| 新增依赖 | alembic + Mako + MarkupSafe | **零** |
| 版本化 | `alembic_version` 表 | `schema_migrations` 表（revision / description / applied_at） |
| 顺序执行 | revision 链 | 按 revision 升序，校验重复与首版必须是基线 |
| 既有库接管 | `stamp <基线修订>` | **`legacy` 分支：只写版本记录，不执行任何 DDL** |
| 事务 | 迁移级事务 | `BEGIN IMMEDIATE` 包裹，失败整体 `ROLLBACK` |
| 备份 | 需自行配置 | **框架内置强制备份**（失败即中止，安全闸 1） |
| 只读预检 | — | `detect_state()` 五态判定 + `partial` 拒绝自动处理 |
| autogenerate | ✅ | ❌ 手写 SQL（本项目迁移仅 3~4 个，优势体现不出来） |
| downgrade | ✅ | ❌ 只前进；回滚用 T-01 备份恢复 |

**SQLite 特有约束**：`executescript()` 会隐式 COMMIT，破坏事务性，因此迁移脚本以**语句列表**（`UPGRADE_STATEMENTS`）声明，逐条 `execute()`，确保可整体回滚。

**连带改动（本轮风险最高处）**：`database.py` 中 import 期的 `create_all()` + 手写 `ALTER TABLE` 已**移除**——import 有副作用正是结构失控的根源（`notifications.link_url` 死列即由此产生）。`tests/__init__.py` 改为在建测试库时显式调用迁移 runner。**已实测：该项移除未影响既有 120 项测试**（全量 135 项通过）。

**T-14 事故记录（已修复并还原）**：runner 初版中 `applied_revisions()` 会调用建表函数，导致 `--dry-run`（使用读写连接）在**真库**上创建了一张空的 `schema_migrations` 表。已修为纯只读，并删除真库上的残留空表（删除前已备份，业务数据未受影响）。该 bug 现由 `test_dry_run_changes_nothing` 守住——破坏性验证确认：恢复 buggy 版本后该用例立即失败。

---

### 3.7 ADR-002R · SQLite 运行契约落地（T-15 实测修正）

ADR-002 的配置契约已按 T-15 落地，**其中一处需要修正**：

| 项 | ADR-002 原文 | **实测结论** |
|---|---|---|
| `isolation_level=None` | 传给 `create_engine` 即可 | ⚠️ **不够**。它只把 SQLAlchemy 层置为 autocommit，底层 pysqlite 仍是 legacy 模式（实测 `dbapi_connection.isolation_level == ''`）。**必须在 `connect` 事件里执行 `dbapi_conn.isolation_level = None`**，否则 legacy 模式会在 DDL 前自动 COMMIT、并在 DML 前自行 BEGIN，与手工发的 BEGIN IMMEDIATE 冲突。 |
| `BEGIN IMMEDIATE` 由谁发出 | `begin` 事件 | ✅ 正确。实测确认：去掉 `begin` 事件后 BEGIN IMMEDIATE 不再发出，行为测试立即失败。 |
| `busy_timeout` 与驱动 timeout 同值 | 应一致 | ✅ 已落地为 `SQLITE_BUSY_TIMEOUT_MS = 15000` 单一来源，测试直接断言二者同源。 |
| `foreign_keys=ON` | 应开启 | ✅ 已开启。**开启前已勘察：现有数据无孤儿行**（`interview_records`/`notifications` 的 `user_id` 全部有效），因此不会让既有数据违约。 |

**实测确认的代价（如实记录）**：全局 BEGIN IMMEDIATE 会让**每个事务（含只读）都申请写锁**，
因此**经由本引擎的并发事务会串行化**——两个 API 请求即使都是只读也会互相排队。
精确边界：WAL 下 RESERVED 锁**不阻塞**其它连接的纯读，所以外部只读工具不受影响；
受影响的只是"同时走本引擎的两个请求"。本项目单机、低并发、事务毫秒级，可接受（ADR-002 的裁决）。
**若将来读多写多**，应改为"仅写路径显式 BEGIN IMMEDIATE"。

**已验证的连带安全性**：`dbapi_connection.isolation_level = None` 之后，
pysqlite 的 `commit()` 仍然有效（其内部查 `sqlite3_get_autocommit()` 再发 COMMIT，
不依赖自身簿记）——由 `BeginImmediateTests` 的用例覆盖，确认 commit 后写锁确实释放。

**补充实测（破坏性验证第二轮发现，2026-09-29）**：把 6 行配置逐行删除做探针，
发现 **`cursor.execute("PRAGMA busy_timeout=%d")` 这一行在引擎路径上是「观测冗余」的**：

| 事实 | 实测数据 |
|---|---|
| `sqlite3.connect(timeout=N)` **自身**就会设置 busy_timeout | `timeout=0.001 → 1ms`；`timeout=5.0 → 5000ms`；`timeout=15.0 → 15000ms` |
| 两者同源于 `SQLITE_BUSY_TIMEOUT_MS` | `SQLITE_CONNECT_ARGS["timeout"] = SQLITE_BUSY_TIMEOUT_MS / 1000` |
| 结论 | 删掉该 PRAGMA，引擎上观测到的 busy_timeout **仍是 15000** → 任何引擎级断言都抓不到 |

**处置**（不删该行，而是补足判别力）：
- **保留**该 PRAGMA 作为防御性冗余 —— 若将来有人只改 `connect_args` 而不动这里，
  PRAGMA 仍能兜住；且它让"期望值"在代码里显式可见。
- **新增判别性用例**：用 `timeout=0.001` 的**裸连接**（绕开驱动层 timeout）调用
  `_apply_sqlite_pragmas()`，断言 `busy_timeout` 从 `1` 变为 `15000`。
  判别力已在探针中验证：删掉该行后此用例失败（`before=1 after=1`）。
- **测试用独立的一次性库文件**，不与连接池争锁 —— 池持有的连接在 WAL 下会阻止
  同一文件上的 `PRAGMA journal_mode=DELETE`（实测 `database is locked`）；
  独立文件同时多验证了"**全新空库也会被设为 WAL**"。

**⚠️ 第二轮修订（T-23 实测触发，用户裁决「仅写路径取锁」）**

| 项 | 修订前（T-15 首版） | **修订后** |
|---|---|---|
| `BEGIN IMMEDIATE` 适用范围 | **全局**：`begin` 事件里无条件发 | **仅写路径**：`services/stores/_sqlite_tx.begin_write` 显式调用 |
| 只读事务 | 也持写锁 → 同一请求内「鉴权读 + 业务写」**必然自锁** | 不持写锁，走语句级自动提交 |
| 并发读 | 经由本引擎的读**互相排队** | 互不阻塞（WAL 的读并发终于用上了） |
| 写保护 | 有效 | **仍然有效**（写事务开始前就取锁） |

**ADR-004 的论证没有丢，只是收窄了适用对象。** 原文说"读事务升级为写时
SQLite 返回 `SQLITE_BUSY` 且不调用 busy handler，导致 `busy_timeout` 失效"
—— 这条**只对写路径成立**：用 `BEGIN IMMEDIATE` 在事务**开始前**取写锁，
就没有"升级"这一步。原先把该论证套用到全部事务，代价是"只读也持写锁"，
既浪费了 WAL 的读并发，又制造了死锁：T-23 把存储层接进路由后，
`get_current_user` 的 SELECT 拿住写锁，处理函数里的 `store.create()`
等满 15 秒后 `database is locked`。

**实现上的一个坑（已写进 `_sqlite_tx.py`）**：`BEGIN IMMEDIATE` 作为 Session 的
**第一条语句**直接 `session.execute()` 会报
`cannot start a transaction within a transaction`（取连接那一步已把事务开起来了；
执行过别的语句之后再发反而正常，因此极易漏测）。`begin_write` 因此落到原始
DBAPI 连接上执行，并先把可能已存在的事务归零。

**已知代价**：写事务之间仍串行（SQLite 单写者模型，固有）；
多语句写的原子性依赖写方法**显式**调用 `begin_write`（三个仓储的 12 个写方法
已全覆盖，并有清单式用例守住）。

---

**第二轮探针结果（6 个缺陷全部被抓住）**：

| 探针（删除的配置） | 单元测试失败数 | 验收工具失败数 |
|---|---|---|
| `PRAGMA foreign_keys=ON` | 5 | 5 |
| `dbapi_conn.isolation_level = None` | 2 | 2 |
| `begin` 事件的 `BEGIN IMMEDIATE` | 2 | 2 |
| `PRAGMA journal_mode=WAL` | 3 | 4 |
| `PRAGMA busy_timeout=N` | 2 | 2 |
| `PRAGMA synchronous=NORMAL` | 2 | 2 |

> ⚠️ 首轮探针曾把 `journal_mode=WAL` 与 `busy_timeout` 报为「漏网」。复查后：
> `journal_mode` 实为**探针执行器的缺陷**（`os.system` + 文件重定向的读取失败被
> 静默吞掉，误显示为"无汇总行"），改用 `subprocess` 管道后正常抓住；
> `busy_timeout` 则是**真实的判别力缺口**，已按上述方式补齐。
> 教训：**探针自身的失败也必须被验证**，否则"漏网"与"探针坏了"无法区分。

---

## 4. 数据模型 DDL 变更

**仅 1 处新增列**（其余表结构沿用 v2.1；**因涉及数据库，须随 ADR-004R 一并审批**）：

```sql
-- interview_sessions：记录结束原因，驱动前端文案与评分口径
ALTER TABLE interview_sessions ADD COLUMN ended_reason VARCHAR;
-- 取值：'completed' | 'timeout' | 'manual' | NULL(仍在进行中)
```

**同步说明**：
- `interview_records.report` 为 JSON 文本列，`ended_reason` **同时写入报告 JSON**（供历史列表渲染），**无需改列**。
- v2.1 已定义的 `status ∈ {active, finished, abandoned}`、`version`、`last_seq`、`last_reply`、`expires_at`、`UNIQUE(user_id) WHERE status='active'` **全部保持**，本 ADR 只补充其**语义裁决**（超时 → `abandoned`）。
- **`ALTER TABLE ... ADD COLUMN` 无唯一约束**，故本次新增列**不受** SQLite 的 `ADD COLUMN ... UNIQUE` 限制（v2.1 已指出 `client_token UNIQUE` 必须走 `batch_alter_table`）。

---

## 5. 部署与运维影响

**若选 A（推荐）**：部署拓扑 **无变化**，仅新增一个 `cleanup.timer`。
**若选 B**：需重写 ADR-006 拓扑（新增 Redis 服务、端口、内存规划）、ADR-019 备份（新增 Redis 持久化文件备份）、ADR-026 清理（TTL 改由 Redis 原生处理）—— 工作量已在 §2.3 计入。

---

## 6. 审批清单（**已于 2026-09-28 全部通过**）

| 编号 | 待审批事项 | 推荐 | **裁决结果** |
|---|---|---|---|
| **1** | **ADR-004R：存储载体选 A（SQLite+WAL）还是 B（Redis）？** | **A** | ✅ **批准 A** —— 单机 ECS 部署，本轮不需多机扩展，无需引入 Redis |
| 2 | 是否采纳 `services/stores/` 抽象层（为未来切换留逃生舱）？ | **采纳** | ✅ **采纳** |
| 3 | 逃生舱触发阈值（§2.8）是否认可？ | 认可 | ✅ **认可** |
| 4 | ADR-022R：超时 → `abandoned` 并释放唯一锁 | 认可 | ✅ **批准** |
| 5 | ADR-007R：超时未答题不计零分 + `ended_reason` | 认可 | ✅ **批准** |
| 6 | ADR-025R：统一 Toast，禁用 `alert`/`confirm`/`prompt` | 认可 | ✅ **批准** |
| 7 | ADR-024R：追加 3 处契约变更（共 16 处） | 认可 | ✅ **批准** |
| 8 | §4 DDL：`interview_sessions` 增 `ended_reason` 列 | 认可 | ✅ **批准** |

### 6.1 批准后的门禁变更

| 门禁 | 批准前 | **批准后（当前）** |
|---|---|---|
| ADR-004R 决策 | 待审批 | ✅ **已批准**，选项 A 生效 |
| 数据库代码改动 | 🔴 一律禁止 | 🟡 **允许开工，但须等任务清单审批** |
| `docs/03-tasks.md` 中的 DB 任务 | — | 🔒 **标记 `待启动`**，解冻条件 = 任务清单人工审批 |

> **解冻流程**：用户审批 `docs/03-tasks.md` → 移除 `🔒 待启动` 标记 → 按依赖顺序开工。
> **未解冻前不得开始**：T-13 ~ T-29 及一切标注 🔒 的任务（见 `03-tasks.md`）。
