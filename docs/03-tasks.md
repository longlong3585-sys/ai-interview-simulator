# 03 · 任务拆解（阶段 5）

> **需求依据**：`docs/01-spec.md` **v2.4**（76 条需求：63 FR + 13 NFR；P0=28 / P1=30 / P2=17 / 范围外 1）
> **架构依据**：`docs/02-architecture.md` v2.1（26 条 ADR）+ `docs/02-architecture-v2.md`（已批准增量）
> **问题来源**：`docs/05-issues-backlog.md`（5 个 Bug）
> **日期**：2026-09-28

## 图例与门禁

| 标记 | 含义 |
|---|---|
| ✅ **已完成** | 已实现 + 测试通过 + 已 commit |
| 🔒 **待启动** | **涉及 `database.py` 或存储层改造**。**须等本任务清单获得人工审批后才解冻开工**（用户指令） |
| ⬜ 可开工 | 不涉及数据库，但仍按阶段顺序执行 |

> **📌 审批范围（2026-09-28，用户裁决）**：**仅批准阶段 0 + 阶段 1（T-01 ~ T-13）**，这 13 个任务**已解冻**。
> **阶段 2 及其后所有 🔒 任务继续保持冻结**，待用户验收阶段 1 实际效果后**逐批解冻**。
> **📌 解冻补充（2026-09-29）**：用户已解冻**阶段 2 第一批**（T-14、T-15）并指定"T-14 先讲计划、T-15 做完给手动验收指令"。
> 本批 **T-14 / T-15 均已完成**；**T-16 起仍为 🔒 冻结**，待用户手动验收 T-15 后再逐批解冻。
> **📌 解冻补充（2026-09-29，第二轮）**：用户验收 T-15 通过，并**授权连续执行 T-16 + T-17**（理由：纯协议图纸 + 纯 DDL 建空表，不涉及业务逻辑）。
> **T-16 / T-17 均已完成，真库已实跑迁移**；**T-18 起仍为 🔒 冻结**，待用户确认后进入 T-18。
> **📌 解冻补充（2026-09-29，第三轮）**：用户验收 T-16 / T-17 通过，**解冻 T-18**（既有表结构变更），并明确要求：
> ① 执行前再次核验 T-01 的单文件备份机制；② 必须按 ADR 要求用重建表实现；③ 完成后交付**针对真库的分步骤人工执行与校验指令（含回滚）**；④ **由用户亲手执行真库变更**。
> **T-18 代码与测试已完成，真库尚未执行**（仍为 `001, 002`）。**T-19 起仍为 🔒 冻结**。
> **📌 解冻补充（2026-09-29，第四轮）**：用户确认 T-18 真库迁移人工执行成功（`003`，14 项自检通过，`verify_t18.py` 26/26），**解冻 T-19** 并指出这是"迁移后第一个真业务代码接入，风险较高"，要求完成后交付人工验证指令。
> **T-19 代码与测试已完成；实现中发现 T-16 协议两处硬伤并修正，另需迁移 004 补 `report` 列 —— 真库仍在 `003`，004 待用户执行**。**T-20 起仍为 🔒 冻结**。
> **📌 解冻补充（2026-09-29，第五轮）**：用户**批准** T-19 的三项裁决（`ReplayLookup` 修正 / **选项 A** 执行迁移 004 / 存储层 JSON 不静默回退），并确认已亲手执行迁移 004（`verify_t19.py --db` 迁移前 3 项 FAIL → 迁移后 6/6 PASS）。**解冻 T-20**。
> **T-20 已完成**（无需迁移）。**T-21 起仍为 🔒 冻结**。
> **阶段顺序不调整**：T-44 路由化按原计划执行（用户认定它是根治 Bug 2/Bug 3 的前置）。
> **开工规则（规则 7）**：每次只做一个任务 → 读任务 → 写代码 → 写测试 → 运行测试 → 修复 → 状态改 done → `git commit` → **暂停等"继续下一个任务"**。

## 执行进度

| 任务 | 状态 | 日期 | Commit | 备注 |
|---|---|---|---|---|
| T-01 | ✅ 已完成 | 2026-09-28 | `0d58240` | 回滚点 tag `rollback-before-refactor`；`make_backup.py` / `verify_backup.py`；正向 exit 0、**负向测试 exit 1** 均通过 |
| T-02 | ✅ 已完成 | 2026-09-28 | 见 git log | 移除 `**/test_*.py` 忽略；忽略根目录 `*.docx`；建立 unittest 测试骨架 + **独立测试库隔离**（16 项测试全绿）。**偏差**：PyPI 不可达 → 以 stdlib `unittest` 运行（见 ADR-021R） |
| T-03 | ✅ 已完成 | 2026-09-28 | 见 git log | Vitest+RTL 配置**待激活**（npm registry 不可达）；当前以零依赖 `node:test` 运行**契约测试 7 项全绿**（跳过词一致性 / API 路径对齐 / 匹配器判别力）。`tsc -b` 与 `vite build` 均 EXIT=0（测试文件置于 `src/` 外，不破坏构建） |
| T-04 | ✅ 已完成 | 2026-09-28 | `035d8f1` | `/api/chat` 挂 `require_user` + 身份改用 `current_user.id`；`ChatRequest.user_id` 已移除。**9 项测试**（含伪造 user_id 无效）；**破坏性验证：移除鉴权后 8/9 失败**。真机验证：无 token→401、伪造 user_id→被忽略 |
| T-05 | ✅ 已完成 | 2026-09-29 | `58d7487` | `/api/resume/upload` 挂 `get_current_user`（**刻意不用 `require_user`**，否则 admin 会被 403）。**10 项测试**；**破坏性验证：移除鉴权后 3/10 失败**。真机验证：无 token→401、带 token+txt→400、DOCX→200、**admin→200**；OpenAPI schema 现已声明 security。顺带抽出 `tests/support.py` 消重 |
| T-06 | ✅ 已完成 | 2026-09-29 | `1bb58e0` | `get_current_user` 增加 `is_active` 校验（**改在鉴权链最底层，一处覆盖三条依赖链**）。**10 项测试**；**破坏性验证：移除校验后 10/10 全部失败**（迄今最强）。真机验证：启用→200、禁用→**401「账号已被禁用」**、其他 3 个接口同样 401、重新启用→200 |
| T-07 | ✅ 已完成（含修正） | 2026-09-29 | `c8f99b8` + 修正提交 | `SECRET_KEY` 移除硬编码回退 + 拒绝 3 个占位符 + 长度下限 32。**14 项测试**（原 8 项全部 mock 掉 load_dotenv，属覆盖盲区；已补 6 项**真实 .env 文件**用例）。**破坏性验证：去掉新支持后恰好 6 项失败**。⚠️ 首版人工验证指令有误（`$env:X=""` 在 Windows 是删除变量，load_dotenv 又从 .env 补回），已订正 |
| T-08 | ✅ 已完成 | 2026-09-29 | `e24bcf5` | 后端新增 `validate_password()` 单一来源并接入注册/改密/重置；前端抽出 `utils/passwordRules.ts` 修掉 3 份重复实现（改密原为 ≥6）。**10 项测试**含三处口径一致性断言；**破坏性验证：10 项 subTest 全失败**。⚠️ 破坏性脚本曾超时残留探针，已还原并改用 try/finally |
| T-09 | ✅ 已完成 | 2026-09-29 | `e486ac1` | 新增 `utils/upload_validation.py`；四道服务端校验（白名单/边读边限/魔数/统一重命名）+ 替换时清理旧文件；刻意不支持 SVG（XSS）。**15 项测试**；**破坏性验证：10/15 失败**。⚠️ 真机验证误用真实账号致其头像被删，已从 T-01 备份字节级还原（见下方事故记录） |
| T-10 | ✅ 已完成 | 2026-09-29 | `da46147` | 新增 `utils/safe_json.py`；修复历史列表/详情 500，并统一 stats、管理端列表、start_interview 非法 JSON→400；前端历史列表加 `report ?` 守卫防白屏。**13 项测试**；**破坏性验证：13 项全部报错** |
| T-11 | ✅ 已完成 | 2026-09-29 | `064558d` | 后端移除 `ChatRequest.action` 与 `ReportRequest.user_id`；前端移除对应发送。**后端 7 项 + 前端契约 3 项**（含识别规则自检）；**破坏性验证：加回字段后 2/7 失败**；保留向后兼容（extra 字段被忽略） |
| T-12 | ✅ 已完成 | 2026-09-29 | `1e44bfe` | 新增 `utils/log_setup.py`；异常处理器记录完整堆栈+请求上下文+`error_id`，对外仍只给通用消息。**7 项测试**；**破坏性验证：4/7 失败**。真机证据：客户端无任何泄露、服务端有完整堆栈且 error_id 对应 |
| T-14 | ✅ 已完成 | 2026-09-29 | 见 git log | **偏离 ADR-009**：PyPI+4 镜像全不可达，Alembic 装不上 → 用户批准自建 runner（ADR-009R）。`migrations/runner.py` + `versions/001_baseline.py` + `scripts/migrate.py`；**零新增依赖**。移除 `database.py` 的 `create_all()`+手写 ALTER。**15 项测试**；破坏性验证抓到 2 个真实弱点（回滚断言过弱、dry-run 写库）并已修正 |
| T-13 | ✅ 已完成 | 2026-09-29 | `46987b1` | 后端提取 `SKIP_WORDS` 常量 + 新增 `GET /api/interview/config`；前端删除硬编码列表改为拉取。**后端 9 项 + 前端 6 项**；**破坏性验证：20 个 subTest 失败**。检测规则含正/负样本自检 |
| T-15 | ✅ 已完成 | 2026-09-29 | `93ed0b3` + 补充提交 | engine 运行契约：WAL + `busy_timeout=15000` + `synchronous=NORMAL` + `foreign_keys=ON` + `BEGIN IMMEDIATE`。**13 项测试**；**破坏性验证：6 个探针全部被抓住**。⚠️ 实测修正 ADR-002 的一处不准确说法：仅 `create_engine(isolation_level=None)` **不够**，须在 connect 事件里设 `dbapi_conn.isolation_level=None`。⚠️ **补充提交**：第二轮探针发现 `PRAGMA busy_timeout` 一行在引擎路径上属"观测冗余"（`sqlite3.connect(timeout=15.0)` 自带同样效果），已补判别性用例 + 独立库测试。⚠️ 已知代价：全局 `BEGIN IMMEDIATE` 使**经由本引擎的并发事务串行化**（WAL 纯读不受影响），已写成用例显式记录。**交付物**：`scripts/verify_t15.py`（人工验收工具，29 项）+ `scripts/probes/`（2 个探针复现脚本）。见 ADR-002R |
| T-16 | ✅ 已完成 | 2026-09-29 | `5893f78` | `services/stores/base.py`：**只有协议与数据类型，零实现**。3 个 `@runtime_checkable` Protocol（`SessionStore` 9 方法 / `CaptchaStore` 3 / `RateLimitStore` 4）+ 4 个 frozen dataclass + 类型化异常 + ISO 时间契约。**26 项测试**（依赖面只在标准库白名单内、禁止 SQL 泄露、方法名与**参数名**逐一断言、鸭子类型可 `isinstance`、frozen 不可变、ISO 字典序==时间序、`is_expired` 边界 `<=`）。**破坏性验证：9 个探针全部被抓住**。⚠️ 其中 2 个探针首轮是"模块导入失败"造成的**假阳性**，已改为精确命中目标断言（见提交说明）。**边界**：`token_blacklist` 表的协议**刻意不定义**，理由见下方注 |
| T-17 | ✅ 已完成 | 2026-09-29 | 见 git log | `migrations/versions/002_sessions_and_stores.py`：4 张新表 + 5 个索引，**全部 `CREATE ... IF NOT EXISTS`**。`ended_reason` 直接写进 CREATE（架构 §4 的 ALTER 形式是"假设表已存在"，实际本表首次创建，结果结构一致）。**真库已实跑迁移成功**（`001` → `001, 002`，原始日志见 `docs/17-migration-log.md`）：既有数据零丢失（users=3 / records=5 不变）、`integrity_check=ok`、`foreign_key_check` 无违规。**25 项专项测试**（另有 `test_migrations.py` 由 15 → 18 项、`test_backup_tools.py` 由 9 → 14 项）。**破坏性验证：11 个探针全部被抓住**（其中 1 个首轮漏网：DDL 的 `DEFAULT 'active'` 从未被执行到 → 已补断言）。⚠️ 顺带发现并修复 3 个问题：迁移日志乱码、`journal_mode.py` 附属文件统计时序错误、**备份产物不是单文件**（见日志 §10） |
| T-18 | ✅ 已完成 | 2026-09-29 | `b17c833` | `migrations/versions/003_existing_tables.py`：`users` 原生 `ADD COLUMN`、`interview_records` **重建表**加 `client_token` + `UNIQUE`、`notifications` **重建表**删死列 `link_url`。**真库已由用户人工执行成功**（`003`，14 项自检通过，`verify_t18.py` 26/26）。**为"重建表"新增三道框架闸**（`runner.py`）：行数不得减少 / 声明式自检 / `foreign_key_check` 必须为空，全部在 COMMIT 前执行、失败即整体回滚。**破坏性验证：13 个探针全部被抓住**（其中 1 个首轮"漏网"实为探针自身针位错误 → 已加"针必须唯一命中"校验）。**交付物**：`scripts/verify_t18.py`、`scripts/probes/probe_t18_migration.py`、`docs/18-manual-migration.md` |
| T-19 | ✅ 已完成 | 2026-09-29 | `f1bdfaa` | `services/stores/sqlite_store.py`：`SessionStore` 的 9 个方法实现（乐观锁 / `seq` 幂等 / 状态机 / 短事务）+ `services/stores/factory.py`（**唯一装配点**，带启动期协议自检）。⚠️ 实现时发现 T-16 协议两处硬伤并修正：`find_replay` 返回 `Optional[str]` 的歧义（空回复重发会被当成新请求 → **重复计费**）改为 `ReplayLookup`；`finish()` 要写报告但 `interview_sessions` 无 `report` 列（ADR-004 与 §6.2 DDL 矛盾）→ 按 ADR-004 补**迁移 004**（用户已批准并手工执行，`verify_t19.py --db` 迁移后 6/6 PASS）。⚠️ 有意偏离 T-10 约定：会话行 JSON 损坏**严格报错**而非容错回退。**40 项专项 + 004 的 12 项**；**15 个探针全部被抓住**。⚠️ **一次真实事故**：首次跑探针时用 `job_kill` 强杀，S7 的缺陷被永久留在工作区（`try/finally` 挡不住强杀），导致测试挂死；已加"运行前另存副本 + 状态文件不符则拒绝运行"的保护。**交付物**：`scripts/verify_t19.py`、`scripts/probes/probe_t19_store.py`、`docs/19-manual-verification.md` |
| T-20 | ✅ 已完成 | 2026-09-29 | 见 git log | `services/stores/sqlite_captcha_store.py`：`CaptchaStore` 的 3 个方法；装配点扩为 `_REGISTRY` 表驱动（T-21 加一行即可）。**核心改进**：原实现是内存字典（多 worker 下各存一份 → 登录随机失败），且"读→判断→写"有 TOCTOU 缝隙；本实现用**单条 UPDATE** 同时完成"存在 + 未用 + 未过期 + 码匹配 + 消费"，`rowcount==1` 才算成功 → **原子**。**31 项测试**（TTL 边界 / 一次性 / 输错不消费 / 输入去空白 / 成功置 `used=1` 而非删除 / 并发恰好一个成功 / 装配点自检 / 事务纪律）。**破坏性验证：10 个探针全部被抓住**（其中 1 个首轮"漏网"→ 发现那个提前返回是**安全判定**而非优化，已补 fail-open 用例）。**无需迁移**（`captcha_store` 表 T-17 已建） |

> **⚠️ 操作教训（T-05 真机验证时踩到，务必记住）**
> `job_kill` **只杀 pwsh 包装进程，不会杀 uvicorn 的 python 子进程**。残留进程会继续占着 8000 端口，导致：
> ① 新起的服务 `[Errno 10048]` 绑定失败并退出；② **curl 打到旧进程（跑着修改前的代码）→ 得到看似成功实则错误的结论**。
> T-05 首次真机验证就因此得到"无 token → 200"的**假结果**，靠"应为 401"的预期不符才发现。
> **正确做法**：每次真机验证前，先确认端口监听者是新进程——
> ```powershell
> netstat -ano | Select-String ":8000\s+.*LISTENING"     # 拿到 PID
> Get-Process -Id <PID> | Select-Object Id, StartTime    # 启动时间必须是刚才
> ```
> 并优先用 **OpenAPI schema 客观证据**（`/openapi.json` 里该接口是否有 `security`）代替纯黑盒判断。
> 停服务时须显式 `Stop-Process` 掉 listener PID 与其 python 父/子进程。

> **🔴 操作事故记录（T-09 真机验证，已完整还原）**
> **经过**：T-09 的活体验证使用了**真实用户 123 的令牌**上传测试图片。上传成功那一步触发了新实现的
> "替换头像时删除旧文件"逻辑，**删除了该用户的原始头像文件**，并把 `users.avatar` 改写为测试图路径。
> **这违反了用户明确的要求**："以后测试涉及改库时，请优先创建临时测试用户，非必要不触碰我的真实数据。"
> **还原**：从 T-01 建立的 `backup/source_*.zip` 中提取原文件（sha256 `df55555a…` **字节级一致**），
> 删除测试文件，并把 `users.avatar` 写回原值。复核确认 uploads 目录恢复为原有 7 张 + `.gitkeep`。
> **教训（后续强制）**：
> 1. 任何会**改库或改文件**的真机验证，**必须先用 `tests/support.py` 造临时用户**，用完即删；
> 2. 涉及"删除旧文件"这类破坏性逻辑时，验证对象**只能是**自己造的临时数据；
> 3. T-01 的回滚备份不仅能救代码，也能救**用户数据** —— 本例即由它挽回。

---

## 阶段 1 完成情况（T-04 ~ T-13）

| 任务 | 提交 |
|---|---|
| T-04 `/api/chat` 鉴权 + 身份改用 `current_user.id` | `035d8f1` |
| T-05 `/api/resume/upload` 挂 `get_current_user` | `58d7487` |
| T-06 `get_current_user` 增加 `is_active` 校验 | `1bb58e0` |
| T-07 `SECRET_KEY` 缺失/弱值启动即失败 | `c8f99b8` |
| T-08 密码策略后端强制 + 三处共用同一规则 | `e24bcf5` |
| T-09 头像上传四道服务端校验 | `e486ac1` |
| T-10 统一 `safe_json_loads()` 容错解析 | `da46147` |
| T-11 死参数清理（后端 + 前端） | `064558d` |
| T-12 全局异常处理器记录堆栈 | `1e44bfe` |
| T-13 跳过词单一来源 + `GET /api/interview/config` | `46987b1` |

**测试规模**：后端 **114 项**（stdlib unittest）＋ 前端 **13 项**（node:test 契约测试），全部通过。
**每个任务均**：写代码 → 写测试 → 跑测试 → **破坏性验证**（临时注入缺陷、确认测试确实会失败）→ 状态置 done → **独立 git commit**（未合并提交）。

**阶段 1 解锁的需求**：FR-1.1、FR-1.5、FR-2.4、FR-2.6、FR-4.9、FR-4.10、FR-6.4、FR-10.4、NFR-1(c)、NFR-2。

> **🔒 冻结范围**：`backend/database.py`、engine 配置、`SessionLocal`、`create_engine`/PRAGMA/连接池、建表语句、`services/stores/`、Alembic 迁移 —— 即 **阶段 2 全部**，以及**阶段 3 全部**（均依赖新存储）。
> **解冻条件**：用户审批本清单 → 移除 🔒 → 按依赖顺序开工。

**工时原则**：每个任务 **1–4 小时**，可独立验收。总计 **56 个任务 / ≈143 小时（≈18 人天）**。

---

## 阶段 2 已完成情况（T-14 ~ T-20）

| 任务 | 提交 | 内容 |
|---|---|---|
| T-14 自建迁移 runner（ADR-009R 偏离审批） | `59b7758` | `migrations/runner.py` + `versions/001_baseline.py` + `scripts/migrate.py`；五道安全闸 |
| T-15 engine 运行契约 | `93ed0b3` + 补充提交 | WAL + `busy_timeout=15000` + `synchronous=NORMAL` + `foreign_keys=ON` + `BEGIN IMMEDIATE`；新增 ADR-002R |
| T-16 存储抽象层协议 | `5893f78` | `services/stores/base.py`：3 个 `runtime_checkable` Protocol + 4 个 frozen dataclass + 类型化异常 + ISO 时间契约 |
| T-17 迁移 002：四张新表 | `74e965c` | `versions/002_sessions_and_stores.py`；真库已实跑（`001` → `001, 002`） |
| T-18 迁移 003：既有表变更 | `b17c833` | `versions/003_existing_tables.py`；首次改动既有表（加列 / 重建表 / 删列）；框架新增三道安全闸。真库已由用户人工执行 |
| T-19 会话存储实现 | `f1bdfaa` | `services/stores/sqlite_store.py` + `factory.py` + 迁移 `004`（补 `report` 列）；修正 T-16 协议两处硬伤。真库 004 已由用户手工执行 |
| T-20 验证码存储实现 | 见 git log | `services/stores/sqlite_captcha_store.py`；单条 UPDATE 实现原子消费；装配点改为表驱动。**无需迁移** |

**测试规模**：后端 **337 项**（阶段 1 结束时 114，阶段 2 已 +223）＋ 前端 **13 项**，全部通过。
**本批偏差/事故均已记录**：T-14 的 `--dry-run` 曾在真库创建空表（已清理并加固为纯只读）；T-15 修正了 ADR-002 关于 `isolation_level` 的不准确说法，并在第二轮探针中补齐 `busy_timeout` 的判别力缺口（见 ADR-002R）。

**T-15 交付物**
| 文件 | 用途 |
|---|---|
| `backend/scripts/verify_t15.py` | **人工验收工具（零风险）**：29 项独立复算，默认在真库副本上跑，真库只做 `mode=ro` 只读核对 |
| `backend/scripts/smoke_t15.py` | **真机只读冒烟**：13 项，走 HTTP→engine→SQLite 全链路，含"接口数字 == 直接读库数字"的跨层一致性核对 |
| `backend/scripts/journal_mode.py` | 查看 / 切换 `journal_mode`（先 checkpoint 再切换；有其它连接时主动拒绝） |
| `backend/scripts/probes/probe_t15_harness.py` | 对验收工具做 6 个破坏性探针（注入缺陷→确认会失败→ `try/finally` 还原→sha256 校验） |
| `backend/scripts/probes/probe_t15_tests.py` | 对 `tests/test_sqlite_engine_config.py` 做同样 6 个探针 |
| `docs/t15-manual-verification.md` | **人工验收指令**（用户明确要求），每条指令均本机实跑过 |

---

## 依赖关系总览

```
阶段0 安全网 (T-01~03)
   │
   ├──▶ 阶段1 后端安全止血 (T-04~13)  ← 不碰数据库，可立即开工
   │
   └──▶ 阶段2 存储层重构 (T-14~22)   ← T-14~T-20 已完成（真库：001~004）；T-21 起仍冻结
             │
             └──▶ 阶段3 Bug修复 🔒 (T-23~33)   ← 依赖新存储
                       │
                       └──▶ 阶段4 前端完善 (T-34~49)   ← 部分可并行
                                 │
                                 └──▶ 阶段5 质量与部署 (T-50~56)

关键路径：T-02 → T-14 → T-15 → T-16 → T-17 → T-18 → T-19 → T-24 → T-26 → T-27 → T-36 → T-40 → T-41 → T-54
发布门禁：T-04/T-05（鉴权）必须与 T-23/T-24/T-25（会话持久化）**同批上线**（Bug 1 明确要求）
```

---

## 阶段 0 · 安全网与脚手架（可立即开工）

| ID | 类别 | 任务 | 工时 | 依赖 | 标记 |
|---|---|---|---|---|---|
| T-01 | 基建 | 备份代码与 `interview.db`，打 tag 建立回滚点 | 1h | — | ✅ |
| T-02 | 基建 | 移除 `.gitignore:45` 的 `**/test_*.py`；建立 pytest 骨架 + 独立测试库 | 2h | T-01 | ✅ |
| T-03 | 基建 | 建立 Vitest + React Testing Library 骨架 | 2h | T-01 | ✅ |

**验收标准**
- T-01：`git tag rollback-before-refactor` 存在；`backup/interview_YYYYMMDD.db` 可被 `sqlite3 .restore` 恢复
- T-02：`pytest` 可运行且**用例文件能被 `git add`**（当前被 ignore）；测试用独立文件 SQLite（非内存，以覆盖 WAL/事务）
- T-03：`npm run test` 可运行；一个示例组件测试通过

---

## 阶段 1 · 后端安全止血（不碰数据库，可立即开工）

| ID | 类别 | 任务 | 工时 | 依赖 | 标记 |
|---|---|---|---|---|---|
| T-04 | **修复 Bug** | `/api/chat` 挂 `require_user`，改用 `current_user.id`（不再读请求体） | 2h | T-02 | ✅ |
| T-05 | **修复 Bug** | `/api/resume/upload` 挂 `get_current_user` | 1h | T-04 | ✅ |
| T-06 | **修复 Bug** | `get_current_user` 增加 `is_active` 校验（FR-2.6） | 1h | T-02 | ✅ |
| T-07 | **修复 Bug** | `SECRET_KEY` 缺失即启动失败，移除硬编码回退（NFR-1c） | 1h | — | ✅ |
| T-08 | **修复 Bug** | 抽出 `validate_password()`，注册/改密共用同一规则（FR-1.1/1.5） | 3h | T-02 | ✅ |
| T-09 | **修复 Bug** | 头像上传后端校验：≤2MB + 魔数 + 扩展名白名单 + 统一重命名（FR-10.4） | 3h | T-02 | ✅ |
| T-10 | **修复 Bug** | 统一 `safe_json_loads()`，修复 `user.py:110,118` 空值保护（FR-6.4） | 2h | T-02 | ✅ |
| T-11 | **修复 Bug** | 死参数清理：后端删 `action`（`ChatRequest.user_id` 已于 T-04 移除）与 `ReportRequest.user_id`，前端同步移除 | 2h | T-04 | ✅ |
| T-12 | **修复 Bug** | 全局异常处理器记录堆栈到日志，对外仍返回通用消息（NFR-2） | 1h | — | ✅ |
| T-13 | **新功能** | 跳过词单一来源：后端常量 + `GET /api/interview/config`（FR-4.10） | 2h | T-02 | ✅ |

**验收标准**
- T-04：无 token 调 `/api/chat` → **401**；请求体传他人 `user_id` **不产生任何影响**；伪造 id 无法读写他人会话
- T-05：无 token 调 `/api/resume/upload` → 401；admin 仍可上传（不被 `require_user` 挡下）
- T-06：管理员禁用某用户后，用其**旧 token** 请求 → 立即 **401/403**（当前仍可访问）
- T-07：不设 `SECRET_KEY` 启动 → **进程直接失败**并给出明确错误；不再使用 `dev-secret-change-in-production`
- T-08：绕过前端直发 6 位密码注册 → **400**；改密与注册使用**同一校验函数**（前后端规则一致）
- T-09：上传 10MB 图片 → 400；`.jpg` 扩展名但内容为文本 → 400；文件名被统一重命名
- T-10：`report` 为 NULL 的记录，历史列表与详情**不再 500**，返回 `null`
- T-11：全库 grep 无 `req.action` / `ReportRequest.user_id` 读取；前端不再发送这两个字段
- T-12：制造未捕获异常 → 日志含完整 traceback，响应体仍为通用消息
- T-13：`GET /api/interview/config` 返回 23 个跳过词；前端不再硬编码该列表

---

## 阶段 2 · 存储层重构 🔒（全部待启动）

> **本阶段所有任务均涉及数据库，全部标记 🔒。**

| ID | 类别 | 任务 | 工时 | 依赖 | 标记 |
|---|---|---|---|---|---|
| T-14 | **存储重构** | 引入 Alembic → **改为自建轻量 runner**（见 ADR-009R）；建立基线 + runbook | 4h | T-02 | ✅ |
| T-15 | **存储重构** | engine 配置：WAL + `busy_timeout=15000` + `foreign_keys=ON` + `isolation_level=None` + `BEGIN IMMEDIATE` 事件 | 3h | T-14 | ✅ |
| T-16 | **存储重构** | `services/stores/` 抽象层协议（`SessionStore`/`CaptchaStore`/`RateLimitStore`） | 2h | T-15 | ✅ |
| T-17 | **存储重构** | 迁移：新增 4 张表（`interview_sessions`/`captcha_store`/`auth_attempts`/`token_blacklist`） | 3h | T-16 | ✅ |
| T-18 | **存储重构** | 迁移：既有表变更（`client_token` / `must_change_password` / 删死列 `link_url`） | 3h | T-17 | ✅ |
| T-19 | **存储重构** | `SQLiteSessionStore`：乐观锁 `version` + `seq` 幂等 + 状态机 + 短事务 | 4h | T-18 | ✅ |
| T-20 | **存储重构** | `SQLiteCaptchaStore`（TTL 300s，一次性） | 2h | T-18 | ✅ |
| T-21 | **存储重构** | `SQLiteRateLimitStore`（**仅失败计数** + 滑动窗口 10min/5 次） | 2h | T-18 | 🔒 |
| T-22 | **存储重构** | 清理任务 `scripts/cleanup.py` + `cleanup.timer`（单实例，非 APScheduler） | 3h | T-19,T-20,T-21 | 🔒 |

**验收标准**
- T-14：空库执行 `init_db.py` → 全部表建成；既有库备份后 `stamp`+`upgrade` → **数据零丢失**；`alembic check` 无差异
- T-15：`PRAGMA journal_mode` 返回 `wal`（且文件属性级验证）；`busy_timeout=15000`、`synchronous=NORMAL`、`foreign_keys=ON` 逐连接生效；**行为判别测试**证明发出的是 `BEGIN IMMEDIATE`（短 timeout 原始连接被锁挡住、纯读连接不受阻）。
  ⚠️ 原始验收语"删用户级联删会话"**本任务范围内不成立**：`interview_sessions` 表到 T-17 才建；且 `foreign_keys=ON` 只会**拒绝**违约操作，不会自动级联（除非 `ON DELETE CASCADE`，见 T-19 设计）。本任务实际验收为：**插入不存在用户的记录被拒 + 删除仍有记录的用户被拒**；开启 FK 前已勘察确认真库**0 条孤儿行**。
- T-16：协议层**只依赖标准库**（AST 断言 import 白名单 = {dataclasses, datetime, typing}）；把 `redis`/`sqlalchemy` 等设为 `sys.modules[...]=None` 后仍能导入；无 SQL 关键字泄露（剔除 docstring 后逐行扫描）；3 个协议的方法名与**参数名**逐一断言；不继承也能通过 `isinstance`；dataclass 全部 `frozen=True`；ISO 串字典序 == 时间序。
  ⚠️ **原始验收语"提供 SQLite 实现"不属于本任务**：实现是 T-19 / T-20 / T-21。T-16 的可验证等价物是"**协议层不绑定任何存储**"（上面那组断言），"不含任何 Redis 代码"则由"只依赖标准库 + 无 Redis API 词汇"共同保证。
  📌 **刻意不定义 `token_blacklist` 的协议**：该表在 T-17 建，但它挂在 **ADR-003 选 B（P1）** 之下，且 `jti` 是否"续期沿用同一条"（ADR-016 第 5 点）尚未落地。在没有调用方的情况下提前定接口，会把未定的语义**锁死**。等 ADR-003-B 真正开工时再补协议 —— 届时它与 `create_access_token` 的改动属同一批。
- T-17：4 张表建成；`UNIQUE(user_id) WHERE status='active'` 生效（同用户第二个 active 被拒，`finished`/`abandoned` 可并存）
  **实际验收**：真库已实跑迁移（`001` → `001, 002`，**日志见 `docs/17-migration-log.md`**）；四张表 + 5 个索引齐备；部分唯一索引实测三条语义（第二个 active 被拒 / 置 `abandoned` 后立刻可重开 / 多条终态并存）；`ON DELETE CASCADE` 实测级联；既有表**零改动**（无 `client_token`、仍有 `link_url`）、既有数据零丢失（users=3 / records=5）、`integrity_check=ok`、`foreign_key_check` 无违规。
  **跨层一致性**：`interview_sessions` 的列集合必须恰好等于 T-16 `SessionSnapshot` 的字段集合 —— 两个方向各有一条断言（`test_session_columns_match_protocol_snapshot` 与 `test_snapshot_fields_match_interview_sessions_columns`），只改一边会立刻报错。
  ⚠️ **形式偏离（结果一致）**：架构 §4 写的是 `ALTER TABLE interview_sessions ADD COLUMN ended_reason`，那是假设该表已存在。实际上本表在 T-17 才首次创建，故该列**直接写进 `CREATE TABLE`**。已在迁移文件里记录唯一的风险场景（若某环境曾手工建过 v2.1 版本的表，`IF NOT EXISTS` 会跳过建表导致缺列），并有断言兜底。
  📌 **框架安全闸的一处口径修正**：原 `test_baseline_contains_no_destructive_sql` 用子串匹配，会被 `REFERENCES users(id) ON DELETE CASCADE` **误报**。已改为**按语句首关键字**判断（并先按 `;` 切分），且把只查基线扩展到**遍历全部迁移** —— 原先新增迁移时存在检查盲区。
- T-18：`client_token` UNIQUE 生效（经 `batch_alter_table`）；死列 `link_url` 已移除；既有数据保留
  **实际做法**：本项目不用 Alembic，`batch_alter_table` 的等价物是**手写 SQLite 12 步法子集**（`CREATE x_new` → `INSERT ... SELECT` 显式列名 → `DROP x` → `RENAME` → 重建索引）。实测确认：`ADD COLUMN ... UNIQUE` 被 SQLite 拒绝（`Cannot add a UNIQUE column`），故必须重建。
  ⚠️ **刻意不做官方 12 步法的第 1/12 步（切 `PRAGMA foreign_keys`）**：实测该 pragma 在事务内是**真正的 no-op**（先设 ON、进事务、再设 OFF，PRAGMA 仍报 1 且孤儿行仍被拒），本框架的事务结构里**无法表达**；而被重建的两张表**无任何子表引用**，因此不需要。
  ⚠️ **`users` 只加列、不重建**：它是三张表的父表，重建时 `DROP TABLE` 会触发 `FOREIGN KEY constraint failed`（已实测）。
  **真库执行**：⏸ **待用户人工执行**，步骤与回滚见 `docs/18-manual-migration.md`。
  **配套框架改动（安全闸 6/7/8）**：`ALLOWS_DATA_LOSS` / `VERIFY_STATEMENTS` / `ALLOWS_TABLE_REBUILD` + `REBUILD_REASON` 三项可选声明；不声明即走最严默认值。静态闸门同步收紧：`DROP` 只在显式声明重建**且**给出理由的迁移里允许，且只允许 `DROP TABLE <表名>` 一种形态。
- T-19：**并发测试**：两请求同 `version` 并发 → 一个成功、一个 **409**（不静默覆盖）；同 `seq` 重发 → 返回缓存 `last_reply` **且不重复调用 AI**
  **实际验收**：`scripts/verify_t19.py` 走查 29/29（含"陈旧 version 被拒且库内容未被覆盖"、"空回复的重发仍判为重发"、"每个方法之后独立连接都能取到写锁"）；专项测试 40 项；**破坏性验证 15 个探针全部被抓住**。
  **框架/协议修正**：① `find_replay` 的 `Optional[str]` 歧义 → `ReplayLookup`（否则空回复重发会被当成新请求，**重复调用 AI**）；② `finish(report_json)` 需要 `interview_sessions.report` 列，而 ADR-004 与 §6.2 DDL 矛盾 → 以 ADR-004 为准，迁移 004 纯加列补上。
  ⚠️ **真库仍在 `003`**：存储层的每个方法都 `SELECT ... report ...`，**004 未执行前无法在真库上工作**（当前无接口调用它，线上不受影响）。执行指令见 `docs/19-manual-verification.md` §4。
  📌 **一处有意偏离既有约定**：T-10 定下"读 JSON 文本列一律走 `safe_json_loads()`（容错回退）"；存储层**不遵守** —— 会话行损坏必须当场报错，否则回退成 `[]` 会让评分看到"0 题 0 答"，产出**看起来正常实则错误**的报告（ADR-007R 要消除的误导性 0 分）。
  📌 **装配点唯一**：业务代码只能从 `services/stores/factory.py` 拿协议对象，不得直接 import 实现；装配期用 `isinstance(store, SessionStore)` 做启动自检（`@runtime_checkable` 在此真正发挥作用）。
- T-20：验证码 300s 后失效；同一验证码**只能用一次**；多 worker 下均可用
  **实际验收**：31 项测试覆盖 TTL 边界（恰好到期算过期，与 T-19 同口径）、成功后再用失败、**输错不消费**（可在有效期内改错重输）、输入去首尾空白、成功后置 `used=1` 而非删除、`purge_expired` 与 `verify` 的判据**严格互补**。
  **"多 worker 可用"的验证方式**：本实现用单条 `UPDATE ... WHERE used=0 AND expires_at > :now AND code = :code`，`rowcount==1` 才算成功 → 并发消费**恰好一个成功**（已用 5 条独立连接真并发打、断言成功次数为 1）。原实现在多 worker 下连正确性都不成立（每个进程一份内存字典），这正是本任务存在的理由。
  ⚠️ **一处理论行为差异**（已在模块 docstring 与用例中写明）：过期判定由原来的严格 `<` 改为 `<=`（口径与 T-19 会话存储统一），差异宽度为一个瞬间。
  📌 **无需迁移**：`captcha_store` 表 T-17 已建，本任务不动数据库。
- T-21：成功登录**不写**该表；第 5 次失败触发拒绝；10 分钟窗口过期后恢复
- T-22：定时任务**只运行一个实例**；过期会话被置 `abandoned`；过期验证码/限流记录被清除

---

## 阶段 3 · Bug 修复 🔒（依赖阶段 2）

| ID | 类别 | 任务 | 工时 | 依赖 | 标记 |
|---|---|---|---|---|---|
| T-23 | **修复 Bug** | 会话键统一 + 无会话返回 **409 + 指引**（Bug 1 收口） | 3h | T-19 | 🔒 |
| T-24 | **修复 Bug** | 新增 `GET /api/interview/session` + `POST /api/interview/abandon`（Bug 2） | 4h | T-19 | 🔒 |
| T-25 | **修复 Bug** | `start_interview` 唯一入口 + 过期行自愈 + 409 携带会话摘要（Bug 2） | 3h | T-24 | 🔒 |
| T-26 | **修复 Bug** | 报告改以**服务端会话**为准，前端不再传 `messages`（Bug 3A） | 3h | T-24 | 🔒 |
| T-27 | **修复 Bug** | 评分提示词区分"未及作答/答不上" + 报告新增 `ended_reason`（FR-4.5） | 3h | T-26 | 🔒 |
| T-28 | **修复 Bug** | 后端超时兜底：会话置 `abandoned`，**释放唯一锁**（FR-4.12） | 2h | T-19 | 🔒 |
| T-29 | **修复 Bug** | 修复 admin 启动竞态（`init_db.py` + `ExecStartPre`） | 2h | T-14 | 🔒 |
| T-30 | **修复 Bug** | 管理员口令 env 注入 + `must_change_password` + 改密 CLI（ADR-017） | 3h | T-18 | 🔒 |
| T-31 | **修复 Bug** | 审核状态与通知**同事务**（FR-8.3） | 2h | T-18 | 🔒 |
| T-32 | **修复 Bug** | 删记录时清理其通知（同事务）（FR-7.6/FR-8.6） | 2h | T-18 | 🔒 |
| T-33 | **修复 Bug** | 趋势图零填充 7 天 + 时区统一 `Asia/Shanghai`（FR-8.1） | 2h | T-18 | 🔒 |

**验收标准**
- T-23：无活跃会话调 `/api/chat` → **409 + "请重新开始"**，**不再静默降级为角色扮演**
- T-24：刷新页面后可 `GET` 回完整会话状态；`abandon` 后唯一锁释放，可立刻开新面试
- T-25：重复点"开始面试" → 409 **且响应体带 `current_index`/`last_seq`/`total`**；过期 active 行被自愈为 `abandoned`，**不出现"409 却查不到活跃会话"**
- T-26：报告内容与**服务端会话**一致；前端不再发送 `messages`；篡改前端数据不影响报告
- T-27：超时结束的报告 `ended_reason='timeout'`；**未答题不计入扣分**（构造"零作答超时"用例，**不得直接给全 0 分**）
- T-28：超时后 `/api/chat` → 409；会话状态为 `abandoned`；唯一锁已释放
- T-29：`--workers 2` 空库首启，**无 `IntegrityError`**，两个 worker 均正常启动
- T-30：`ADMIN_INIT_PASSWORD` 未设时生成随机口令并打印一次；首登强制改密；`admin123` 不再可用
- T-31：在通知写入处注入异常 → 状态**回滚**，不出现"状态已变但无通知"
- T-32：删除面试记录后，关联通知一并清除，**无 `target_id` 悬挂**
- T-33：近 7 天趋势**恒返回 7 个数据点**（无记录的日子为 0）；跨 UTC+8 凌晨的数据归入正确日期

---

## 阶段 4 · 前端完善

| ID | 类别 | 任务 | 工时 | 依赖 | 标记 |
|---|---|---|---|---|---|
| T-34 | **修复 Bug** | API 基地址改**相对路径** + Vite `server.proxy` + `VITE_API_BASE_URL`（Bug 4，**P0**） | 2h | — | ⬜ |
| T-35 | **修复 Bug** | CORS 允许来源配置化，`expose_headers` 加 `X-Refreshed-Token`（NFR-10a） | 1h | T-34 | ⬜ |
| T-36 | **新功能** | api 层收敛：**24 处裸 `fetch` 全部迁入**统一层（ADR-016 前置） | 4h | T-03,T-34 | ⬜ |
| T-37 | **新功能** | Toast 组件 + 全局错误呈现（**禁用 `alert`**）（NFR-13） | 3h | T-03 | ⬜ |
| T-38 | **新功能** | `ConfirmDialog` 替换 3 处 `confirm`/`prompt`（NFR-13） | 3h | T-37 | ⬜ |
| T-39 | **新功能** | `ErrorBoundary` 包裹路由出口（NFR-11） | 1h | T-03 | ⬜ |
| T-40 | **新功能** | `AuthContext`：集中并**持久化 token + userId**（Bug 2 前端） | 3h | T-36 | ⬜ |
| T-41 | **修复 Bug** | 挂载时拉取会话并重建视图（Bug 2 前端） | 3h | T-24,T-40 | ⬜ |
| T-42 | **修复 Bug** | 超时强制闭环：`useRef` 持最新回调 + **锁定 UI/销毁输入区**（Bug 3A/3B） | 3h | T-40 | ⬜ |
| T-43 | **修复 Bug** | 超时报告文案标注"因超时自动结束，仅基于已答部分评分"（FR-4.5） | 1h | T-27,T-42 | ⬜ |
| T-44 | **新功能** | 启用 `react-router-dom` + 守卫移路由层（顺带修 **FR-11.3 条件 Hook**） | 4h | T-40 | ⬜ |
| T-45 | **新功能** | 拆分 `AdminPanel` 组件 | 3h | T-44 | ⬜ |
| T-46 | **新功能** | 拆分 `InterviewRoom` 组件 | 4h | T-44 | ⬜ |
| T-47 | **新功能** | 拆分 `ReportView`/`ProfilePanel`/`NotificationCenter`/`QuestionBank` | 4h | T-44 | ⬜ |
| T-48 | **新功能** | 死代码清理（`historyListRef`、`QuestionBank.tsx` 处置、`_passwordError`） | 2h | T-47 | ⬜ |
| T-49 | **修复 Bug** | `q.tags?.map` 可选链，题库缺字段不崩（FR-9.2） | 1h | T-47 | ⬜ |

**验收标准**
- T-34：`vite.config.ts` 含 `server.proxy`；**构建产物中 grep 不到 `127.0.0.1`**；预览/HTTPS 环境下**所有按钮可点**
- T-35：CORS 来源来自环境变量；响应头含 `X-Refreshed-Token`；**来源不得为 `*`**
- T-36：全库 grep **裸 `fetch(\`${API_BASE_URL}` 为 0**；401 统一登出；续期头集中处理
- T-37：任一请求失败 → 出现 **Toast**；**全库 grep `alert(` 为 0**；Toast 可被测试断言
- T-38：全库 grep `confirm(`/`prompt(` 为 0；删除与重置密码走模态确认
- T-39：注入子组件异常 → 显示兜底 UI，**不白屏**
- T-40：刷新后 `userId` 仍存在；登出清理干净
- T-41：刷新页面 → **恢复当前面试进度**，答案落在正确题目上
- T-42：15 分钟归零 → **UI 锁定、输入区销毁**，无法继续答题
- T-43：超时报告页显示标注文案；非超时报告不显示
- T-44：`AdminPanelContent` 不再在 `useState` 前 `return`；令牌由有到无不抛错
- T-45~T-47：**单文件 ≤400 行**；`App.tsx` 降为路由装配
- T-48：上述死代码全库 grep 为 0
- T-49：构造缺 `tags` 的题目，题库页不崩

---

## 阶段 5 · 质量、契约与部署

| ID | 类别 | 任务 | 工时 | 依赖 | 标记 |
|---|---|---|---|---|---|
| T-50 | **新功能** | JWT 滑动续期 + **8h 绝对上限** + `auth_time`（ADR-016，**P0**） | 4h | T-36 | ⬜ |
| T-51 | **新功能** | `jti` 声明 + `token_blacklist` 真登出（ADR-003 选 B） | 3h | T-22,T-50 | 🔒 |
| T-52 | **新功能** | 备份脚本 + `backup.timer`（ADR-019） | 2h | T-22 | 🔒 |
| T-53 | **新功能** | Nginx：`client_max_body_size 8m` + XFF 转发 + HTTPS/Certbot（ADR-012/015） | 3h | T-34 | ⬜ |
| T-54 | 质量 | 后端 P0 接口测试补齐（鉴权/越权/409/降级）（NFR-6） | 4h | 阶段 1–3 | ⬜ |
| T-55 | 质量 | 前端关键测试（AuthContext / 401 / Toast / 超时锁定）（NFR-6） | 3h | 阶段 4 | ⬜ |
| T-56 | 质量 | 并发与幂等专项测试（乐观锁 / `seq` / 版本冲突）（NFR-6） | 4h | T-19 | 🔒 |

**验收标准**
- T-50：剩余有效期 <50% 时响应头回写新 token；超 8h 拒绝续期并返回 `X-Token-Expired: absolute`；前端提示重新登录**且保留后端会话**
- T-51：登出后旧 token → 401；黑名单 TTL **≥8h**（不早于续期链失效，否则令牌复活）
- T-52：每日生成备份；保留 7 份；**恢复演练成功**
- T-53：上传 >5MB 简历返回 400（**不是 413**）；后端能拿到真实客户端 IP；`http://` 301 跳 `https://`
- T-54：28 条 P0 相关接口均有可执行测试且全绿
- T-55：前端关键路径测试全绿；**无一处依赖 `alert`**
- T-56：并发用例稳定通过（重复运行 100 次无随机失败）

---

## 分类汇总（对应用户要求的三类）

| 类别 | 任务数 | 工时 | 说明 |
|---|---|---|---|
| **【修复现有 Bug】** | **26** | **56h** | 覆盖 `05-issues-backlog.md` 全部 5 个 Bug + 阶段 0 逆向发现的缺陷 |
| **【重构/替换数据库】** | **9** | **26h** | 阶段 2 全部（🔒 待启动） |
| **【完善新功能】** | **15** | **45h** | 前端工程化、Toast、路由、续期、备份、Nginx |
| 【基建】 | 3 | 5h | 备份回滚点、pytest 骨架、Vitest 骨架（支撑上述三类） |
| 【质量】 | 3 | 11h | 后端 P0 测试、前端测试、并发幂等专项 |
| **合计** | **56** | **143h（≈18 人天）** | 已脚本核验：56 个唯一 ID，工时加总 143h |

**🔒 待启动任务：共 23 个**
`T-14 ~ T-33`（阶段 2 全部 9 个 + 阶段 3 全部 11 个）+ `T-51`、`T-52`、`T-56`

---

## 与 5 个 Bug 的对应关系（可追溯）

| Bug | 覆盖任务 |
|---|---|
| Bug 1 接口裸奔 | **T-04、T-05**（止血）+ **T-23**（会话键收口） |
| Bug 2 刷新报废 | **T-19、T-24、T-25**（后端）+ **T-40、T-41**（前端） |
| Bug 3A 超时闭包 | **T-26、T-27**（后端）+ **T-42**（前端） |
| Bug 3B 状态未重置 | **T-28**（后端兜底）+ **T-42、T-43**（前端锁定与文案） |
| Bug 4 绝对路径 | **T-34、T-35** |
| Bug 5 健康度 | 5-a→**T-14/T-18**；5-b→**T-36/T-44~T-48**；5-c→**T-07/T-30/T-08**；5-d→**T-37/T-38** |

---

## 下一步

**本清单待你验收。** 确认后：
1. 移除 🔒 标记，**阶段 0 与阶段 1 可立即开工**（共 13 个任务，≈18h）；
2. 阶段 2 起按依赖顺序推进；
3. 按你的规则 7 —— **每个任务完成后运行测试、更新状态、暂停等你确认**。
