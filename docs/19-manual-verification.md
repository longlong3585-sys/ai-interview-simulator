# T-19 人工验证指令

> **任务**：`SQLiteSessionStore` —— 乐观锁 `version` + `seq` 幂等 + 状态机 + 短事务
> **状态**：代码与测试已完成；**真库尚未执行迁移 004**（真库仍在 `003`）。
> **本文件里的每条命令与预期输出，我都已在本机实跑过。**

---

## 0. 先说两件必须你拍板的事

实现 T-19 的过程中发现 T-16 那份**已批准的协议**有两处硬伤 —— 都不是风格问题，
第二处直接导致"报告无处可写"。我按架构文档做了取舍，但**需要你确认**。

### 0.1 `find_replay` 的返回类型有歧义（会导致**重复计费**）

| | |
|---|---|
| T-16 原协议 | `def find_replay(session_id, seq) -> Optional[str]` |
| 问题 | 用 `None` 同时表示两件事：①"这不是重发，请调 AI"；②"**是**重发，但上次回复恰好为空"。两者撞车 |
| 后果 | 调用方把 ② 当成 ①，就会**再调一次 AI** —— 而 ADR-023 的幂等机制存在的唯一目的就是避免这个 |
| 我的处置 | 改成显式结构体 `ReplayLookup(is_replay: bool, reply: Optional[str])`；`None` 只保留"会话不存在"一个含义 |
| 影响面 | 零（当时还没有任何调用方；改动集中在 `base.py` + 新增 4 条用例） |

### 0.2 `finish()` 要写报告，但 `interview_sessions` **没有 report 列**（架构文档自相矛盾）

| | |
|---|---|
| ADR-004（行为契约） | 明确写 `UPDATE interview_sessions SET report=:json, status='finished', version=version+1 WHERE ...` |
| §6.2（建表语句） | `interview_sessions` 的 DDL 里**没有 `report` 列**；v2 增量文档 §4 也只补了 `ended_reason` |
| T-16 协议 | 已经声明 `finish(session_id, expected_version, report_json, ended_reason, now)` —— 与 ADR-004 一致，与 §6.2 冲突 |
| 我的取舍 | **以 ADR-004 为准，新增迁移 004 补列**（纯 `ADD COLUMN`，1 条语句，不重建表、不搬运数据） |
| 理由 | ① 契约优先于实现细节；② ADR-024R ⑭ 要求 `generate_report` 改以**服务端会话**为准，报告不落会话就无处可依；③ ADR-023 规定 `client_token` 幂等键属于 `save_interview`，所以不能靠"finish 时直接插记录"绕过；④ 成本极低 |

**三个选项与我的推荐（请你选）**

| 选项 | 做法 | 代价 |
|---|---|---|
| **A（我推荐的，代码已按此写好）** | 迁移 004 给 `interview_sessions` 加 `report TEXT`（可空、无默认值） | 一次纯加列；真库需执行 004 |
| B | 不改结构；`finish()` 去掉 `report_json` 参数，报告改由 T-26 另找落点 | 要**改回** T-16 已批准的协议；T-26 仍要面对同一个问题 |
| C | 报告落 `interview_records`（finish 同事务插记录） | 改变 `save_interview` 的归档语义，与 ADR-023 的 `client_token` 设计打架，明显超出 T-19 范围 |

> 若你选 B 或 C，需要改的只有 `finish()` 的实现与它对应用例 + 删掉 004。
> 其余 8 个方法与全部测试都不受影响。

### 0.3 一处有意偏离既有约定（已写进代码注释）

T-10 定下"读 JSON 文本列一律走 `safe_json_loads()`（容错、坏数据回退默认值）"。
**存储层刻意不遵守**：`question_status` 一旦被静默回退成 `[]`，评分就会看到
"0 道题、0 个回答"，产出一份**看起来正常、实则完全错误**的报告 ——
正是 ADR-007R 要消除的"误导性 0 分"。会话行损坏属数据完整性事故，
应当**当场炸掉**并带上 `session_id`。
`interview_records` 那种"一条坏记录拖垮整个列表"的场景才是容错的适用场合。

---

## 1. 验收 1：自动化测试（零风险，约 90 秒）

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'

# 1a. T-19 专项（存储层 40 项）
.\venv\Scripts\python.exe -m unittest tests.test_sqlite_session_store -v

# 1b. 相关模块（协议 30 + 迁移 004 12 + 框架 25）
.\venv\Scripts\python.exe -m unittest tests.test_stores_protocol tests.test_migration_004 tests.test_migrations

# 1c. 全量
.\venv\Scripts\python.exe -m unittest discover -s tests -t .
```

**期望**：1a `Ran 40 tests` → `OK`；1c `Ran 305 tests` → `OK`（约 70 秒）。

---

## 2. 验收 2：可跑走查（零风险，**推荐重点做这项**）

T-19 是**库**，还没有任何接口在用它（接入是 T-23 之后），所以没法"点界面上手验"。
替代方案是把四组语义跑一遍、并打印**可观测证据** —— 每一步都同时给出
"store 说了什么"和"用独立连接直接读库读到什么"。

```powershell
.\venv\Scripts\python.exe scripts\verify_t19.py
echo $LASTEXITCODE
```

**刚跑出来的实际结果**：`共 29 项，通过 29 项，失败 0 项` / 退出码 `0`。

| 段 | 验的是什么 | 关键证据 |
|---|---|---|
| 1 | 唯一锁：同用户只能有一个 `active` | 第二个 `create` 抛 `ActiveSessionExists` |
| 2 | **`seq` 幂等** | `重发 seq=1 -> is_replay=True`；**`seq=2 回复为空 -> is_replay=True reply=None`**（这条就是 §0.1 那个修正） |
| 3 | **乐观锁** | `陈旧 version=2 的提交 -> applied=False`；且**库中内容未被覆盖** |
| 4 | 状态机 + 报告落库 | `report='{"overall_score": 8.5}'`；终态后可立刻开新会话 |
| 5 | 过期自愈 | 自愈 1 行、`ended_reason=timeout`、自愈后可立刻重开 |
| 6 | **短事务** | 4 个方法之后，**独立连接都能立刻取到写锁** |
| 7 | 损坏数据 | 坏 JSON 抛 `StoreError`，不静默降级 |

**它自己也有判别力（已实测）**：去掉乐观锁的 `version` 条件后，
同一工具从 29/29 变成 **3 项 FAIL**（陈旧 version 被拒 / 库内容未被覆盖 / user_answers 未被污染）。
不注入缺陷时又回到 29/29，文件 sha256 与注入前一致。

---

## 3. 验收 3：破坏性验证（零风险，约 4 分钟）

证明"测试全绿"不是空话 —— 逐条注入缺陷、确认测试**确实会失败**、再还原。

```powershell
.\venv\Scripts\python.exe scripts\probes\probe_t19_store.py
echo $LASTEXITCODE
```

**期望**：15 个探针全部 `CAUGHT`，三个文件的 `sha256 BEFORE == AFTER`，退出码 `0`。

| 组 | 探针 |
|---|---|
| 乐观锁 | S1 去掉 `version` 条件 / S2 去掉 `status='active'` / S3 去掉过期守卫 / S4 不递增 version |
| **seq 幂等** | S5 把 `find_replay` 退回 T-16 的裸 `str` 语义（**空回复会重复计费**） |
| 短事务 | S6 不提交 / S7 不关闭 session（事务泄漏） |
| 状态机 | S8 给 `abandon` 加过期守卫（超时兜底会永远失败）/ S9 不校验 `ended_reason` |
| 其他 | S10 用异常文本判冲突 / S11 JSON 容错回退 / P1 快照去掉 report / M1、M2 破坏 004 |

> ⚠️ **运行请勿中断**（见 §5）。脚本已为此加了保护，但中断仍可能留下注入态。

---

## 4. 验收 4：真库迁移 004（**需要你按按钮**）

### 4.1 为什么必须做

存储层的每个方法都 `SELECT ... report ...`。**真库在 003 时没有这一列**，
所以 `SQLiteSessionStore` 现在**无法在真库上工作**。
（目前没有接口调用它，所以线上不受影响；但 T-23 接入前必须补上。）

### 4.2 先看清将要做什么

```powershell
.\venv\Scripts\python.exe scripts\migrate.py --status
#   期望 applied: 001, 002, 003 / pending: 004

.\venv\Scripts\python.exe scripts\migrate.py --dry-run
#   期望 待应用: 004 / [dry-run] 未做任何修改
```

### 4.3 执行

```powershell
.\venv\Scripts\python.exe scripts\migrate.py
```

**期望输出（逐字）**

```
  [备份] 迁移前备份 -> pre-migration_20260929_XXXXXX.db
  [备份] 完成，integrity_check=ok，行数={...}
  状态      : managed（已应用：001, 002, 003）
  待应用    : 004
  应用 004 : interview_sessions 补 report 列（ADR-004 报告路径所需，§6.2 DDL 遗漏）
     自检: 声明式 9 项通过 | 行数无减少（8 张表）| foreign_key_check 无违规
  完成，应用了 1 个迁移
```

三个确认点：`[备份] integrity_check=ok`、`自检: 声明式 9 项通过`、`完成，应用了 1 个迁移`。

### 4.4 校验

**先跑一次"真库前置结构检查"**（只读；迁移前跑应当**失败**，迁移后应当**全过**）：

```powershell
.\venv\Scripts\python.exe scripts\verify_t19.py --db interview.db
```

**迁移前**（真库现在就是这个状态）—— 期望 **失败**，3 项 FAIL：

```
  迁移版本: 001, 002, 003
  interview_sessions 列(15): session_id, ... , ended_reason
  [FAIL] 存储层需要的 16 列齐备  -> 缺少: ['report']
  [FAIL] 迁移 004 已应用（report 列存在）  -> 未应用 004 时存储层无法工作
  [FAIL] report 可为 NULL 且无默认值  -> report 定义=None
  ...
  结论: 该库尚不可用 [FAILED]
```

**迁移 004 之后**再跑同一条命令 —— 期望 **6/6 PASS**，结论为
`该库可用于 SQLiteSessionStore [ALL PASS]`。

这就是本次迁移的**验收判据**：存储层能不能在这个库上工作。

其余确认：

```powershell
.\venv\Scripts\python.exe scripts\migrate.py --status
#   期望 applied: 001, 002, 003, 004 / pending: (none)

.\venv\Scripts\python.exe scripts\verify_t17.py interview.db
#   期望 21/21 通过，且 users=3 / interview_records=5 / notifications=0
#   （004 没碰这三张表，所以这条应当原样通过 —— 用来证明"只动了该动的"）
```

`verify_t18.py` 断言的是 `users` / `interview_records` / `notifications`
三张表，004 不涉及它们，所以它应当**继续 26/26 通过**。

### 4.5 应用级验证

```powershell
.\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
# 另一个终端：
.\venv\Scripts\python.exe scripts\smoke_t15.py --db interview.db
#   期望 13/13 通过
```

---

## 5. 一次真实事故与已加的保护（请务必知道）

**事故经过**：我第一次跑 T-19 探针时，因为 15 个探针逐个跑太慢
（其中"事务泄漏"探针会让后续测试每个操作都等 15 秒 `busy_timeout`），
我用 `job_kill` 掐掉了它。**掐掉时探针正停在 S7**——
于是 `get()` 里那句 `try/finally: s.close()` 被**永久留在被删除的状态**。

**后果**：此后 `test_sqlite_session_store` 从 3.4 秒变成**永久挂死**。
而且探针脚本的"基线保护"也报不出真正原因 —— 因为基线本身已经坏了，
它只会说"基线未通过"，看起来像是测试有问题。

**两个教训**：

1. **`try/finally` 挡不住强杀。** 进程被 `kill` 时 `finally` 不会执行。
2. **未跟踪的新文件没有 git 救生网。** 我在 T-18 的说明里写过
   "中断就 `git checkout -- backend/database.py` 还原" ——
   那条**只对已跟踪文件有效**；当时被探针修改的 `003_existing_tables.py`
   是新增的未跟踪文件，`git checkout` 根本救不回来。

**已加的保护**（`scripts/probes/probe_t19_store.py`）：

- 运行前把三个目标文件的原始内容**另存到临时目录**（不依赖 git），并打印路径；
- 写一个状态文件记录"改之前的 sha256"。**下次启动时若发现内容与记录不符，
  直接拒绝运行**，并打印恢复命令 —— 而不是在已经损坏的工作区上继续做实验。

**给 T-15 / T-16 / T-17 / T-18 那几个探针脚本的提醒**：它们**没有**这层保护。
若在运行期间强行中断，请用 `git status` 检查工作区，
并注意**未被 git 跟踪的新文件无法用 `git checkout` 还原**（需要手工重写）。

---

## 6. 回滚

### 6.1 回滚迁移 004

004 只加了一列。回滚 = 用迁移前备份恢复：

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统'

# ① 确认后端已停止
netstat -ano | Select-String ":8000\s+.*LISTENING"     # 期望无输出

# ② 校验备份可用
.\backend\venv\Scripts\python.exe backend\scripts\verify_backup.py --db backup\pre-migration_20260929_XXXXXX.db
#    期望 VERIFY_OK

# ③ 当前库另存一份，再覆盖
Copy-Item backend\interview.db backend\interview.db.after-004
Copy-Item backup\pre-migration_20260929_XXXXXX.db backend\interview.db -Force

# ④ 确认回到 003
.\backend\venv\Scripts\python.exe backend\scripts\migrate.py --status
```

> 注意：SQLite 不支持 `DROP COLUMN` 之外的回退语义，本项目也没有 downgrade
> （ADR-009R：只前进）。**回滚一律走备份恢复**，这也是每次迁移都强制备份的原因。

### 6.2 回滚代码

```powershell
git revert <T-19 的 commit>
```

---

## 7. 命令速查

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'

# 验收 1：自动化测试
.\venv\Scripts\python.exe -m unittest tests.test_sqlite_session_store -v
.\venv\Scripts\python.exe -m unittest discover -s tests -t .

# 验收 2：可跑走查（只用临时库）
.\venv\Scripts\python.exe scripts\verify_t19.py

# 验收 3：破坏性验证（**请勿中断**）
.\venv\Scripts\python.exe scripts\probes\probe_t19_store.py

# 验收 4：真库迁移 004
.\venv\Scripts\python.exe scripts\migrate.py --status
.\venv\Scripts\python.exe scripts\migrate.py --dry-run
.\venv\Scripts\python.exe scripts\migrate.py
.\venv\Scripts\python.exe scripts\verify_t19.py --db interview.db   # 迁移前 FAIL / 迁移后 PASS
.\venv\Scripts\python.exe scripts\migrate.py --status
```

**本任务新增/改动的文件**

| 文件 | 说明 |
|---|---|
| `backend/services/stores/sqlite_store.py` | **核心交付物**：9 个方法的实现 |
| `backend/services/stores/factory.py` | **唯一装配点**：业务代码从这里拿协议对象，不直接 import 实现 |
| `backend/services/stores/base.py` | 协议修正：`ReplayLookup` + `SessionSnapshot.report` |
| `backend/migrations/versions/004_session_report.py` | 补 `report` 列（**待你执行**） |
| `backend/scripts/verify_t19.py` | 验收走查（29 项）+ `--db` 只读前置结构检查（6 项） |
| `backend/scripts/probes/probe_t19_store.py` | 15 个探针（含中断保护） |
| `backend/tests/test_sqlite_session_store.py` | 40 项 |
| `backend/tests/test_migration_004.py` | 12 项 |
