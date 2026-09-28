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
> **阶段顺序不调整**：T-44 路由化按原计划执行（用户认定它是根治 Bug 2/Bug 3 的前置）。
> **开工规则（规则 7）**：每次只做一个任务 → 读任务 → 写代码 → 写测试 → 运行测试 → 修复 → 状态改 done → `git commit` → **暂停等"继续下一个任务"**。

## 执行进度

| 任务 | 状态 | 日期 | Commit | 备注 |
|---|---|---|---|---|
| T-01 | ✅ 已完成 | 2026-09-28 | `0d58240` | 回滚点 tag `rollback-before-refactor`；`make_backup.py` / `verify_backup.py`；正向 exit 0、**负向测试 exit 1** 均通过 |
| T-02 | ✅ 已完成 | 2026-09-28 | 见 git log | 移除 `**/test_*.py` 忽略；忽略根目录 `*.docx`；建立 unittest 测试骨架 + **独立测试库隔离**（16 项测试全绿）。**偏差**：PyPI 不可达 → 以 stdlib `unittest` 运行（见 ADR-021R） |
| T-03 | ✅ 已完成 | 2026-09-28 | 见 git log | Vitest+RTL 配置**待激活**（npm registry 不可达）；当前以零依赖 `node:test` 运行**契约测试 7 项全绿**（跳过词一致性 / API 路径对齐 / 匹配器判别力）。`tsc -b` 与 `vite build` 均 EXIT=0（测试文件置于 `src/` 外，不破坏构建） |
| T-04 | ✅ 已完成 | 2026-09-28 | 见 git log | `/api/chat` 挂 `require_user` + 身份改用 `current_user.id`；`ChatRequest.user_id` 已移除。**9 项测试**（含伪造 user_id 无效）；**破坏性验证：移除鉴权后 8/9 失败**。真机验证：无 token→401、伪造 user_id→被忽略 |
| T-05 | ✅ 已完成 | 2026-09-29 | 见 git log | `/api/resume/upload` 挂 `get_current_user`（**刻意不用 `require_user`**，否则 admin 会被 403）。**10 项测试**；**破坏性验证：移除鉴权后 3/10 失败**。真机验证：无 token→401、带 token+txt→400、DOCX→200、**admin→200**；OpenAPI schema 现已声明 security。顺带抽出 `tests/support.py` 消重 |
| T-11 | ✅ 已完成 | 2026-09-29 | 见 git log | 后端移除 `ChatRequest.action` 与 `ReportRequest.user_id`；前端移除对应发送。**后端 7 项 + 前端契约 3 项**（含识别规则自检）；**破坏性验证：加回字段后 2/7 失败**；保留向后兼容（extra 字段被忽略） |
| T-10 | ✅ 已完成 | 2026-09-29 | 见 git log | 新增 `utils/safe_json.py`；修复历史列表/详情 500，并统一 stats、管理端列表、start_interview 非法 JSON→400；前端历史列表加 `report ?` 守卫防白屏。**13 项测试**；**破坏性验证：13 项全部报错** |
| T-09 | ✅ 已完成 | 2026-09-29 | 见 git log | 新增 `utils/upload_validation.py`；四道服务端校验（白名单/边读边限/魔数/统一重命名）+ 替换时清理旧文件；刻意不支持 SVG（XSS）。**15 项测试**；**破坏性验证：10/15 失败**。uploads/ 未被测试污染 |
| T-08 | ✅ 已完成 | 2026-09-29 | 见 git log | 后端新增 `validate_password()` 单一来源并接入注册/改密/重置；前端抽出 `utils/passwordRules.ts` 修掉 3 份重复实现（改密原为 ≥6）。**10 项测试**含三处口径一致性断言；**破坏性验证：10 项 subTest 全失败**。⚠️ 破坏性脚本曾超时残留探针，已还原并改用 try/finally |
| T-07 | ✅ 已完成 | 2026-09-29 | 见 git log | `SECRET_KEY` 移除硬编码回退 + 拒绝 3 个文档占位符 + 长度下限 32。**8 项测试**；**破坏性验证：还原旧写法后 7/8 失败**。回归确认现有 .env 仍可用 |
| T-06 | ✅ 已完成 | 2026-09-29 | 见 git log | `get_current_user` 增加 `is_active` 校验（**改在鉴权链最底层，一处覆盖三条依赖链**）。**10 项测试**；**破坏性验证：移除校验后 10/10 全部失败**（迄今最强）。真机验证：启用→200、禁用→**401「账号已被禁用」**、其他 3 个接口同样 401、重新启用→200。测试期间临时禁用过真实用户 123，**已确认还原为 is_active=1** |

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

> **🔒 冻结范围**：`backend/database.py`、engine 配置、`SessionLocal`、`create_engine`/PRAGMA/连接池、建表语句、`services/stores/`、Alembic 迁移 —— 即 **阶段 2 全部**，以及**阶段 3 全部**（均依赖新存储）。
> **解冻条件**：用户审批本清单 → 移除 🔒 → 按依赖顺序开工。

**工时原则**：每个任务 **1–4 小时**，可独立验收。总计 **56 个任务 / ≈143 小时（≈18 人天）**。

---

## 依赖关系总览

```
阶段0 安全网 (T-01~03)
   │
   ├──▶ 阶段1 后端安全止血 (T-04~13)  ← 不碰数据库，可立即开工
   │
   └──▶ 阶段2 存储层重构 🔒 (T-14~22)   ← 全部冻结
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
| T-12 | **修复 Bug** | 全局异常处理器记录堆栈到日志，对外仍返回通用消息（NFR-2） | 1h | — | ⬜ |
| T-13 | **新功能** | 跳过词单一来源：后端常量 + `GET /api/interview/config`（FR-4.10） | 2h | T-02 | ⬜ |

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
| T-14 | **存储重构** | 引入 Alembic；建立基线（`stamp <基线修订>` → `upgrade head`）+ runbook | 4h | T-02 | 🔒 |
| T-15 | **存储重构** | engine 配置：WAL + `busy_timeout=15000` + `foreign_keys=ON` + `isolation_level=None` + `BEGIN IMMEDIATE` 事件 | 3h | T-14 | 🔒 |
| T-16 | **存储重构** | `services/stores/` 抽象层协议（`SessionStore`/`CaptchaStore`/`RateLimitStore`） | 2h | T-15 | 🔒 |
| T-17 | **存储重构** | 迁移：新增 4 张表（`interview_sessions`/`captcha_store`/`auth_attempts`/`token_blacklist`） | 3h | T-16 | 🔒 |
| T-18 | **存储重构** | 迁移：既有表变更（`client_token` / `must_change_password` / 删死列 `link_url`） | 3h | T-17 | 🔒 |
| T-19 | **存储重构** | `SQLiteSessionStore`：乐观锁 `version` + `seq` 幂等 + 状态机 + 短事务 | 4h | T-18 | 🔒 |
| T-20 | **存储重构** | `SQLiteCaptchaStore`（TTL 300s，一次性） | 2h | T-18 | 🔒 |
| T-21 | **存储重构** | `SQLiteRateLimitStore`（**仅失败计数** + 滑动窗口 10min/5 次） | 2h | T-18 | 🔒 |
| T-22 | **存储重构** | 清理任务 `scripts/cleanup.py` + `cleanup.timer`（单实例，非 APScheduler） | 3h | T-19,T-20,T-21 | 🔒 |

**验收标准**
- T-14：空库执行 `init_db.py` → 全部表建成；既有库备份后 `stamp`+`upgrade` → **数据零丢失**；`alembic check` 无差异
- T-15：`PRAGMA journal_mode` 返回 `wal`；`foreign_keys=ON` 生效（删用户级联删会话）；日志确认发出 `BEGIN IMMEDIATE`
- T-16：调用方只依赖协议；提供 SQLite 实现且**不含任何 Redis 代码**
- T-17：4 张表建成；`UNIQUE(user_id) WHERE status='active'` 生效（同用户第二个 active 被拒，`finished`/`abandoned` 可并存）
- T-18：`client_token` UNIQUE 生效（经 `batch_alter_table`）；死列 `link_url` 已移除；既有数据保留
- T-19：**并发测试**：两请求同 `version` 并发 → 一个成功、一个 **409**（不静默覆盖）；同 `seq` 重发 → 返回缓存 `last_reply` **且不重复调用 AI**
- T-20：验证码 300s 后失效；同一验证码**只能用一次**；多 worker 下均可用
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
