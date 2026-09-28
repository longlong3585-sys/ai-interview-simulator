# T-15 人工验收指令

> **任务**：SQLite engine 运行契约（WAL / `busy_timeout` / `foreign_keys` / `BEGIN IMMEDIATE`）
> **提交**：`93ed0b3` + 补充提交
> **你要求**："这个任务涉及底层配置，做完后请给我一份测试指令，我会再次手动验收。"
>
> 本文件里的**每一条指令、每一个期望输出，我都在本机实跑过**。
> 跑的过程中发现并订正了 4 处我原先写错的说明（见 §8），不是凭想象写的。

---

## 0. 验收前须知（请先读完这 5 条）

| # | 须知 | 说明 |
|---|---|---|
| 1 | **验收 1、2 完全不碰真库** | 验收 2 的工具会把真库**复制一份**再验证；真库只做 `mode=ro` 只读核对。 |
| 2 | **验收 3 会改动真库，但改动可逆且已批准** | 后端启动时会立刻把 `interview.db` 转为 **WAL**，并出现 `interview.db-shm`（约 32 KB）与 `interview.db-wal`（**可能为 0 字节**）。主文件**大小不变**。这是 ADR-002 / ADR-004R 已批准的目标状态，**不是故障**。还原见 §4。 |
| 3 | **不要用真实账号做写操作** | 验收 3 只做"读"（看列表/资料/统计）。要写就先造临时用户 —— T-09 事故后定下的硬规矩。 |
| 4 | **登录必须过图形验证码** | `POST /api/login` 要求验证码，脚本无法识别。所以脚本走"自签令牌"（只读 `SECRET_KEY`，不碰数据库）。想走真登录请用浏览器。 |
| 5 | **先确认端口没有旧进程** | `job_kill` 只杀包装进程、不杀 uvicorn 的 python 子进程。旧进程会让请求打到**改动前的代码**，得到假结论（T-05 踩过）。 |

所有命令请**先执行一次** `chcp 65001`（否则中文输出乱码）。

---

## 1. 验收 1：自动化测试（零风险，约 60 秒）

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'

# 1a. T-15 专项（本任务新增/改动的 13 项）
.\venv\Scripts\python.exe -m unittest tests.test_sqlite_engine_config -v

# 1b. 全量
.\venv\Scripts\python.exe -m unittest discover -s tests -t .
```

**刚跑出来的实际结果**：1a `Ran 13 tests` → `OK`；1b `Ran 148 tests in 57.8s` → `OK`。
（阶段 1 结束时是 114 项，本任务 +34。）

---

## 2. 验收 2：独立复算工具（零风险，**建议重点做这项**）

这是我为本任务写的人工验收工具。它用**原始 sqlite3 + SQLAlchemy 两条独立路径**
重新验证同一批结论（**不 import 测试文件**），所以"测试和工具同时写错"的概率很低。

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe scripts\verify_t15.py
echo $LASTEXITCODE
```

**刚跑出来的实际结果**：`共 29 项，通过 29 项，失败 0 项` / `结论: 全部通过 [ALL PASS]` / 退出码 `0`。

它分 5 段，每段在证明什么：

| 段 | 检查内容 | 为什么需要 |
|---|---|---|
| **1** | `journal_mode=wal`、`busy_timeout=15000`、`synchronous=1`、`foreign_keys=1`；驱动 timeout 与 busy_timeout 同源同值；**连开 3 条新连接**逐个验证；换**原始 sqlite3** 连接看 WAL；DBAPI `isolation_level is None` | PRAGMA 是**连接级**的，验一条不算数；WAL 是**文件级**的，要换工具验 |
| **1b** | 用 `timeout=0.001` 的**裸连接**调用 `_apply_sqlite_pragmas()`，断言 `busy_timeout` 由 `1` 变 `15000`；**全新空库**也被设为 WAL；重复应用幂等 | 见下方 §8 第 1 条 —— 这是本轮唯一返工处 |
| **2** | 让引擎只跑一条 `SELECT`，再用短 timeout 的原始连接抢写锁 —— **被挡住**才说明发的是 `BEGIN IMMEDIATE` 而非 `DEFERRED`；对照组证明 WAL 下**纯读不受影响**；事务结束后写锁释放 | 不看日志，直接看**锁行为**，这是最硬的证据 |
| **3** | 插入不存在用户 → `IntegrityError`；删除仍有记录的用户 → `IntegrityError`；正常写入仍可用 | `foreign_keys` 默认是 **0**，光看 PRAGMA 不够，必须看它**真的拦不拦得住** |
| **4** | 第二个引擎事务被第一个挡住；释放后可继续用 | 把"并发事务串行化"这个**已知代价显式确认**，而不是当意外 |
| **5** | 真库以 `mode=ro` 打开：表清单 / 行数 / 迁移版本 / **0 条孤儿行** / `foreign_key_check` | 证明 T-15 **没有动过你的真库** |

---

## 3. 验收 3：真实服务全链路（**会改动真库 → WAL**，约 3 分钟）

### 3.1 先确认没有旧服务残留

```powershell
netstat -ano | Select-String ":8000\s+.*LISTENING"
# 若有，记下 PID 并确认它是不是你刚启动的
Get-Process -Id <PID> | Select-Object Id, ProcessName, StartTime
# 是旧的就连同它的 python 父子进程一起停掉，再复查端口已空
Stop-Process -Id <PID> -Force
```

### 3.2 启动后端

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

**启动时请留意日志**：不应出现 `journal_mode 未能设为 WAL` 这条 WARNING。
（代码里刻意加了这一条 —— WAL 在忙时可能**静默**失败，必须留痕。实测未出现。）

### 3.3 观察 WAL 落地（**新开一个终端**，后端保持运行）

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe scripts\journal_mode.py --status
```

**刚跑出来的实际结果**（在真库副本上、服务启动后立即测）：

```
journal_mode: wal
-wal        : 0 字节          <- 注意：可能是 0 字节，不代表没生效
-shm        : 32768 字节
主文件大小  : 118784 字节      <- 与转换前一致，没有变小
```

**为什么会自动变 WAL**：`main.py` 的 `@app.on_event("startup")` 会查一次 `users` 表，
建立连接时触发 `connect` 事件 → 执行 `PRAGMA journal_mode=WAL`。
所以**一启动就生效**，不需要先发请求。

### 3.4 全链路只读冒烟（推荐用这个脚本）

```powershell
# 终端 2（后端仍在运行）
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'
.\venv\Scripts\python.exe scripts\smoke_t15.py
echo $LASTEXITCODE
```

**刚跑出来的实际结果**：`共 13 项，通过 13 项，失败 0 项` / 退出码 `0`，其中包含：

| 检查 | 实际结果 |
|---|---|
| `GET /openapi.json` | 200，31 个路径 |
| `GET /api/captcha`（公开） | 200 且带 `X-Captcha-Id` |
| 无令牌访问 `/api/user/profile`、`/api/history`、`/api/user/stats`、`/api/interview/config` | **全部 401** |
| `GET /api/user/profile`（带令牌） | 200，`username=admin` |
| `GET /api/admin/stats` | 200，`total_users=2 total_interviews=5` |
| `GET /api/admin/users` | 200，3 条 |
| **跨层一致性**：接口 `total_interviews` == 直接读库的 `interview_records` 行数 | **接口=5 / 库=5** ✅ |
| `GET /api/history`（管理员） | **403「管理员不能进行面试」**（有意的业务规则） |

> **这个脚本是只读的**（代码里只发 `GET`），可以安全地对真库运行。
> 它**同时**验证了 HTTP 层没坏 + engine 读写正常 —— 因为跨层一致性那一项
> 要求"接口返回的数字"必须等于"用 sqlite3 直接查出来的数字"。

**也可以直接用浏览器**（真登录路径）：打开 **http://localhost:5173/**
（注意是 `localhost`，不是 `127.0.0.1` —— CORS 白名单是 `localhost:5173/5174`），
用 `admin` / `admin123` 登录，能看到资料页与管理端列表即可。
若要用普通用户看历史列表，用 `123` 那个账号。

### 3.5 收尾

```powershell
netstat -ano | Select-String ":8000\s+.*LISTENING"
Stop-Process -Id <PID> -Force
```

---

## 4. 验收 4（可选）：把真库还原为 `delete` 模式

WAL 是**批准过的**目标状态，**保留即可**。若想让工作区回到"干净"状态：

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'

# 必须先停掉后端！
.\venv\Scripts\python.exe scripts\journal_mode.py --to delete
```

**实测行为（两种情况我都跑过）**

| 情况 | 结果 |
|---|---|
| **后端还在运行**时执行 | `切换失败: database is locked` + 明确的排查提示，**退出码 1**（不会留下半改状态） |
| **后端已停止**后执行 | `journal_mode: wal -> delete`，`-wal`/`-shm` 消失；随后 `integrity_check=ok`、`fk_check=[]`、用户与记录数不变 |

该工具会**先** `PRAGMA wal_checkpoint(TRUNCATE)` 把 WAL 内容并回主文件、**再**切换模式 ——
顺序反了会丢数据。这一点已在工具里固化，并会在有其它连接持锁时主动拒绝继续。

事后想再切回去：`--to wal`。

---

## 5. 验收 5（可选）：证明"测试真的有判别力"

本节证明验收 2 的"29/29 通过"**不是空话**"：脚本逐行删除配置、确认工具/测试**确实失败**、
再 `try/finally` 还原，最后用 **sha256** 证明还原是字节级的。

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'

.\venv\Scripts\python.exe scripts\probes\probe_t15_harness.py   # 对验收工具做 6 个探针
.\venv\Scripts\python.exe scripts\probes\probe_t15_tests.py     # 对单元测试做同样 6 个探针
```

**刚跑出来的实际结果（两者都是）**

```
CAUGHT   PROBE-1 remove PRAGMA foreign_keys=ON          failures=5
CAUGHT   PROBE-2 remove dbapi isolation_level=None      failures=2
CAUGHT   PROBE-3 remove begin BEGIN IMMEDIATE           failures=2
CAUGHT   PROBE-4 remove PRAGMA journal_mode=WAL         failures=4(工具) / 3(测试)
CAUGHT   PROBE-5 remove PRAGMA busy_timeout             failures=2
CAUGHT   PROBE-6 remove PRAGMA synchronous=NORMAL       failures=2
结论: 全部探针被抓住，且已还原 [OK]
database.py sha256 BEFORE == AFTER   (af803cef...c0fd6f)
```

退出码应为 `0`。跑完请执行 `git status --short` —— **应为空**（证明探针没留下残留改动）。

> ⚠️ 这两个脚本会**临时修改 `backend/database.py`**。虽然用 `try/finally` 保证还原，
> 但若在运行期间强制中断（Ctrl+C / 关窗口），可能停在"已注入缺陷"的状态。
> 届时执行 `git checkout -- backend/database.py` 还原即可。

---

## 6. 若某项失败，怎么给我反馈

请贴三样东西：

1. **失败的验收编号**（1 / 2 / 3 / 4 / 5）
2. **完整输出**（不要只贴一行结论）
3. `cd backend; git rev-parse HEAD` 的结果（确认你测的是哪个提交）

回滚：本任务只改了 `backend/database.py` 的 engine 段落，`git revert` 即可；
T-01 的 tag `rollback-before-refactor` 与 `backup/` 目录仍在。

---

## 7. 本任务的已知边界（如实说明，不是"验收通过"的营销词）

| 边界 | 说明 |
|---|---|
| **并发事务会串行化** | 全局 `BEGIN IMMEDIATE` 让每个事务（含只读）都申请写锁，因此**经由本引擎的两个并发请求会互相排队**。WAL 下**纯读不受影响**，受影响的是"同时走本引擎的请求"。本项目单机、低并发、事务毫秒级，ADR-002 已裁决可接受。**读多写多时需改为"仅写路径显式 BEGIN IMMEDIATE"**。 |
| `busy_timeout` 的 PRAGMA 行在引擎路径上是**冗余**的 | 真正生效来源是 `connect_args["timeout"]`，两者同源。保留 PRAGMA 是防御性冗余。详见 §8。 |
| 真库当前仍是 `delete` 模式 | 因为 T-15 期间我没有启动过真服务（活体测试全程用副本）。**你第一次启动后会变 WAL**，属正常。 |
| 管理员不能看面试历史 | `/api/history`、`/api/notifications` 对 `role='admin'` 返回 **403**，这是**既有业务规则**，不是 T-15 引入的。 |
| 前端无关 | T-15 纯后端；前端 13 项契约测试不受影响，不必重跑。 |

---

## 8. 我在写这份指令时**订正的 4 处自己的错误**

留在这里，是因为它们正好说明"按想象写指令"有多容易出错 —— 而这正是你要我避免的。

| # | 我原先写的 | 实测事实 | 后果 |
|---|---|---|---|
| 1 | "`PRAGMA busy_timeout` 删掉测试会失败" | **不会**。`sqlite3.connect(timeout=15.0)` 自己就把 busy_timeout 设成 15000（实测 `0.001→1ms`、`5.0→5000ms`），两者同源 → **引擎路径上永远观测不出差别**。这是**真实的判别力缺口**。 | 已补一条绕开驱动层的裸连接用例（`before=1 / after=15000`），并保留该 PRAGMA 作防御性冗余 |
| 2 | "`-wal` 文件存在且**非 0 字节**" | **是 0 字节**（`-shm` 是 32768 字节）。没有未 checkpoint 的写入时，`-wal` 就是空的 | 已在 §3.3 明确标注，避免你把"0 字节"误判为故障 |
| 3 | "`Invoke-RestMethod /api/user/login`" | 路径是 **`/api/login`**（无 `/user` 段），且**必须带图形验证码**，纯 curl 登录不可行 | 改为脚本自签令牌 + 浏览器真登录两条路径 |
| 4 | "curl 打 `/api/interview/config` 看跳过词" | 该接口**需要令牌**（无令牌 401） | 已归入"必须 401"的检查项 |

> 另外还订正了**探针执行器自身的一个 bug**：首版用 `os.system` + 文件重定向抓输出，
> 读取失败时被静默吞掉，把 `journal_mode=WAL` 这个**其实被抓住**的探针误报成"漏网"。
> 改用 `subprocess` 管道后正常。**教训：探针自身坏了，会和"漏网"长得一模一样** ——
> 所以探针的失败也必须被验证。

---

## 9. 命令速查

```powershell
chcp 65001
cd 'D:\AI 驱动的智能面试准备与模拟系统\backend'

# 验收 1：自动化测试
.\venv\Scripts\python.exe -m unittest tests.test_sqlite_engine_config -v
.\venv\Scripts\python.exe -m unittest discover -s tests -t .

# 验收 2：独立复算（零风险，29 项）
.\venv\Scripts\python.exe scripts\verify_t15.py

# 验收 3：真服务全链路
.\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
.\venv\Scripts\python.exe scripts\journal_mode.py --status     # 另一终端
.\venv\Scripts\python.exe scripts\smoke_t15.py                 # 另一终端，只读 13 项

# 验收 4：还原 journal_mode（先停服务！）
.\venv\Scripts\python.exe scripts\journal_mode.py --to delete

# 验收 5：探针（证明测试有判别力）
.\venv\Scripts\python.exe scripts\probes\probe_t15_harness.py
.\venv\Scripts\python.exe scripts\probes\probe_t15_tests.py
```

**本任务新增的文件**

| 文件 | 用途 |
|---|---|
| `backend/scripts/verify_t15.py` | 验收 2：独立复算 29 项（默认在真库副本上跑） |
| `backend/scripts/smoke_t15.py` | 验收 3：真机只读冒烟 13 项（含跨层一致性核对） |
| `backend/scripts/journal_mode.py` | 验收 4：查看 / 切换 journal_mode（带 checkpoint 与拒绝保护） |
| `backend/scripts/probes/probe_t15_harness.py` | 验收 5：对验收工具做 6 个破坏性探针 |
| `backend/scripts/probes/probe_t15_tests.py` | 验收 5：对单元测试做 6 个破坏性探针 |
