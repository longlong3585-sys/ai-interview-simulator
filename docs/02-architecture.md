# 02 · 架构设计说明书（v2.1）

> **阶段 3 产物** — 架构设计｜**修订**：v2.1（依 `docs/02-arch-review.md` 第 1、2 轮评审整改）
> **基线**：`main` @ `13c0321`｜**需求依据**：`docs/01-spec.md` **v2.1**（本版已承接其全部裁决）
> **结构**：AS-IS → 问题诊断与反向追溯 → TO-BE → **ADR-001~025** → 数据模型 → 安全 → 部署
> ⚠️ **全部 ADR 需你确认后才作为阶段 5 的依据**（规则 6）。

**v2.1 相对 v2 的修订（均为第 2 轮复审确认的缺陷）**

| # | v2 的错误 | v2.1 的更正 |
|---|---|---|
| 1 | 与 spec v2.1 基线脱节 | 全文承接 **FR-2.4 的 N2 裁决**（`start_interview` 为唯一会话入口、无会话分支废弃）与 **FR-4.2(a)**（反问不推进索引） |
| 2 | ADR-004 把 25s 的 AI 调用圈进写事务 | **AI 调用移出事务**；写事务只做版本复核 + 全部变更列更新；**重试不重放 AI** |
| 3 | `alembic stamp head` | 改为 **`stamp <基线修订>`**，并定义空库路径与 `batch_alter_table` |
| 4 | 称「`.gitignore:37` 忽略该目录」 | **该规则是根锚定的 `uploads/avatars/*`，根本不匹配 `backend/uploads/avatars/`**（实测 exit 1）→ 已更正并给出真正修法 |
| 5 | 称「必须开 `--proxy-headers` 否则 IP 恒为 127.0.0.1」 | **错误因果**：uvicorn 0.33.0 默认 `proxy_headers=True`、`forwarded_allow_ips="127.0.0.1"`。真正前置是 **Nginx 转发 `X-Forwarded-For`** |
| 6 | 称「`services/api.ts` 已收敛全部请求」 | **虚假**：`App.tsx` 有 **24 处裸 `fetch`**，仅 **8 处** 走 `authFetch`（合计 **32 处调用点**） |
| 7 | ADR 编号含撇号（`001'`/`017'`），017 空缺 + 3 处悬空引用 | **去撇号，重排为 001~025 连续编号**，补上缺失的「管理员口令轮换」ADR |
| 8 | 「覆盖全部 14 条 P0」 | spec v2.1 实为 **26 条 P0**（已实测清点） |
| 9 | `useState` 73 处 | 实测 **72 处调用**（73 含 import 行） |
| 10 | 会话删除语义矛盾、409 会把用户锁在门外、清理触发未定 | 统一为 `status` 状态机；新增 **放弃/恢复** 接口；清理改 **systemd timer 单实例** |

---

## 1. 架构目标与约束

| 目标 | 说明 |
|---|---|
| G-1 增量可上线 | 不重写、不换主栈，改造分批上线（A-1） |
| G-2 堵住安全缺口 | 全接口鉴权、密码策略后端强制、禁用即时生效（NFR-1、FR-2.4、FR-2.6） |
| G-3 主流程可靠 | 会话可恢复、超时必出报告、评分失败不误导、落库失败可见（FR-4.4/4.5、FR-5.2、FR-6.2） |
| G-4 可多进程部署 | 去掉对进程内存的强依赖（NFR-5、A-5） |
| G-5 前端可维护 | 拆掉 2322 行单文件、72 处 `useState` |

**约束**：单人维护；DeepSeek 唯一 AI 供应商；现有 3 表数据须保留；生产为**单机** ECS（2C2G）。

---

## 2. AS-IS 现状架构

### 2.1 系统上下文

```
   普通用户 ─┐                      ┌─▶ DeepSeek API (deepseek-chat)
             ├─▶ AI 智能面试模拟系统 ─┤
   管理员 ───┘                      └─▶ SQLite (interview.db)
```

### 2.2 运行时组件

```
┌──────────────── 浏览器 (React 19 SPA) ────────────────┐
│  App.tsx  2322 行 · 72 处 useState · 无路由            │
│   ├─ AdminPanelContent（条件 Hook 违规）               │
│   └─ 请求发起：8 处 authFetch ＋ 24 处裸 fetch ⚠️       │
│  localStorage: token / role / username（无 userId）    │
└──────────────────────┬────────────────────────────────┘
                       │ 直连 http://127.0.0.1:8000（无 Vite proxy）⚠️
┌──────────────────────▼────────────────────────────────┐
│            FastAPI 单进程 (uvicorn :8000)              │
│  routers/  auth_router · interview · admin · user      │
│  鉴权依赖  get_current_user⚠️(不校验 is_active)         │
│  ┌──────────────────────────────────────────────────┐ │
│  │ ⚠️ 进程内存态（多 worker / 重启即失效）             │ │
│  │  captcha_store{}  ip_attempts{}  (auth.py)        │ │
│  │  interview_sessions{}          (interview.py)     │ │
│  └──────────────────────────────────────────────────┘ │
│  utils/ai_helpers.py → OpenAI SDK → DeepSeek（同步）    │
└──────┬───────────────────────────────┬────────────────┘
       ▼                               ▼
 ┌───────────────┐            ┌──────────────────────┐
 │ SQLite        │            │ uploads/avatars/     │
 │ users / records│           │ ⚠️ 7 头像已被 git 跟踪 │
 │ notifications │            │ 且 .gitignore 并未覆盖 │
 └───────────────┘            └──────────────────────┘
```

### 2.3 后端分层与耦合点

| 层 | 文件 | 备注 |
|---|---|---|
| 入口 | `main.py` | 装配、CORS（硬编码）、静态挂载、全局异常处理、**启动期建管理员（竞态）** |
| 路由 | `routers/*.py` | **面试状态机、60 行评分提示词、文件解析均写在路由里** |
| 鉴权 | `auth.py` | JWT（**无 `jti`**）、bcrypt、验证码、IP 限流、三个依赖 |
| 模型 | `database.py`、`models/schemas.py` | ORM + Pydantic（含死参数） |
| 配置 | `config.py` | **`SECRET_KEY` 有硬编码回退** |

### 2.4 关键数据流：一次面试

```
①上传简历 POST /api/resume/upload (⚠️无鉴权) → 生成 7 题（默认硬编码）
②开始面试 POST /api/start_interview (require_user)
        → interview_sessions[current_user.id] = {...}   ⚠️静默覆盖
③逐轮答题 POST /api/chat (⚠️无鉴权, user_id 取自请求体)
        → idx=session["current_index"] → 【AI 调用 25s】 → idx+=1 → 写回  ⚠️非原子
④生成报告 POST /api/generate_report → 仅用前端 messages → 删会话
⑤自动落库 POST /api/save_interview → pending  ⚠️无幂等
⑥管理员审核 PATCH /api/admin/interviews/{id} → 通知（⚠️3 次独立 commit）
⑦用户收通知 GET /api/notifications（30 秒轮询）
```

---

## 3. 问题诊断与反向可追溯矩阵

### 3.1 问题诊断（D-1~D-17）

| # | 问题 | 根因 | 需求 |
|---|---|---|---|
| D-1 | 两接口无鉴权；`/api/chat` 信任请求体 `user_id` | 路由签名缺 `Depends` | FR-2.4 |
| D-2 | 刷新后进度失效 | `_userId` 不持久化 + **无接口可取回会话** | FR-4.4 |
| D-3 | 超时/自动结束不出报告 | 闭包捕获旧 `messages`；报告只依赖前端 | FR-4.5 |
| D-4 | 评分失败返回全 0 分 | 异常分支伪造了与真 0 分无法区分的响应 | FR-5.2 |
| D-5 | 无法多进程部署 | 验证码/限流/会话存模块级字典 | NFR-5、FR-1.3 |
| D-6 | 前端不可维护 | 2322 行、72 `useState`、无路由 | NFR-4 |
| D-7 | 建表靠 `create_all` + 手写 ALTER | 无版本化迁移，已产生死列 `link_url` | NFR-8 |
| D-8 | 排障困难 | 全局异常处理器吞堆栈 | NFR-2 |
| D-9 | AI 调用可能挂起 | 评分与角色扮演调用无 timeout | NFR-3 |
| D-10 | 核心卖点失效 | 简历问题实际为硬编码 7 题 | FR-3.4 |
| D-11 | 落库失败静默 | 仅 `console.error` | FR-6.2 |
| D-12 | 零自动化测试 | 无框架，`.gitignore:45` 忽略 `test_*.py` | NFR-6 |
| D-13 | **禁用不生效** | `get_current_user` 不校验 `is_active` | FR-2.6 |
| D-14 | **密码策略仅前端** | 后端注册只判空；改密规则前后端冲突 | FR-1.1、FR-1.5 |
| D-15 | **密钥与初始口令** | `SECRET_KEY` 硬编码回退；`admin123` 硬编码且对既有库不生效 | NFR-1 |
| D-16 | **上传面失控** | 头像无大小校验；7 头像被跟踪且未被 ignore 覆盖 | FR-10.4 |
| D-17 | **跨域与端点未配置化** | CORS 硬编码；前端 API 基地址为绝对地址 | NFR-10 |

### 3.2 反向可追溯矩阵（全部 26 条 P0）

> 实测清点：spec v2.1 共 **74 条需求**（62 FR + 12 NFR），其中 **P0 = 26 / P1 = 30 / P2 = 17 / 范围外 = 1**。

| 需求 | P | 诊断 | 设计落点 | 验收方式 |
|---|---|---|---|---|
| FR-1.1 后端强制密码策略 | P0 | D-14 | **ADR-018** 抽出 `validate_password()`，注册/改密共用 | 绕过前端直发弱密码须 400 |
| FR-1.2 登录 | P0 | — | **ADR-016** 令牌有效期单一来源 + 滑动续期 | 有效期取自 `ACCESS_TOKEN_EXPIRE_MINUTES` |
| FR-1.3 验证码与限流 | P0 | D-5 | **ADR-004** 载体 + **ADR-014** 算法 + **ADR-015** 客户端 IP | 多 worker 下可用；XFF 生效 |
| FR-1.5 改密规则统一 | P0 | D-14 | **ADR-018** 同一 `validate_password()` | 6-7 位前后端一致拒绝 |
| FR-2.1 角色模型 | P0 | — | 保持 | 回归 |
| FR-2.3 管理端鉴权 | P0 | — | 保持 | 非 admin 403 回归 |
| FR-2.4 全接口鉴权 | P0 | D-1 | §4.2 挂鉴权 + `/api/chat` 改用 `current_user.id` + **承接 N2 裁决** | 无令牌 401；传他人 user_id 无效 |
| FR-2.5 越权防护 | P0 | — | 保持 `user_id` 过滤 + NULL 姿态 | 回归 |
| FR-2.6 禁用即时生效 | P0 | D-13 | `get_current_user` 增加 `is_active` 校验 | 禁用后旧令牌立即 401 |
| FR-3.1 上传简历 | P0 | — | **ADR-013** 上传安全 | 回归 + 大小/类型校验 |
| FR-3.4 简历驱动问题 | P0 | D-10 | **ADR-010**；`RESUME_QUESTION_MODE` 默认启用 | 含 "Kubernetes" 简历生成含该词问题 |
| FR-4.1 开始面试 | P0 | — | **ADR-004** + **ADR-023** 幂等 + **ADR-022** 恢复/放弃 | 重复开始返回 409 且可放弃重开 |
| FR-4.2 反馈与反问 | P0 | D-2 | §4.2 会话路径统一；**反问走"不推进索引"路径** | 会话中反问被回应且不跳题 |
| FR-4.4 会话可恢复 | P0 | D-2 | **ADR-022** + `GET /api/interview/session` 契约 | 刷新后续答，答案落在正确题目 |
| FR-4.5 超时必出报告 | P0 | D-3 | 报告改以**服务端会话**为准；定时器用 ref 持最新回调 | 15 分钟归零后必出报告 |
| FR-5.1 评分契约 | P0 | — | **ADR-007** 响应契约 | 回归 |
| FR-5.2 失败不误导 | P0 | D-4 | **ADR-007**：503 + `Retry-After` + 前端重试组件 | 注入失败后界面提示可重试，非 0 分 |
| FR-6.1 自动落库 | P0 | — | **ADR-023** 幂等键 | 回归 |
| FR-6.2 落库失败可见 | P0 | D-11 | §9 统一错误提示 | 注入失败后用户可见提示 |
| FR-7.1 状态通知 | P0 | — | **ADR-020** 参数固化 | 回归 |
| FR-8.3 面试审核 | P0 | — | §4.2 状态与通知同事务 | 中间失败不留"状态已变无通知" |
| FR-8.4 报告详情 | P0 | — | 保持 | 回归 |
| NFR-1 安全基线 | P0 | D-1/D-13/D-14/D-15/D-16 | **ADR-011/013/015/017/018** + §7 | 白名单外全 401；SECRET_KEY 缺失启动失败 |
| NFR-3 AI 超时 | P0 | D-9 | **ADR-007** 统一超时 ≤30s | 注入超时后阈值内返回 |
| NFR-5 多进程 | P0 | D-5 | **ADR-002/004/006** | 多 worker 下 FR-1.3、FR-4.1~4.5 正常 |
| NFR-6 自动化测试 | P0 | D-12 | **ADR-021** | pytest + Vitest 可跑，覆盖 P0 |

### 3.3 P1 需求的设计落点（补齐 v2 的覆盖缺口）

v2 仅引用 62 条 FR 中的 32 条。下表为**第 2 轮评审点名的 9 条**（原无任何设计）给出落点；v2.1 全文覆盖提升至 **38/62 FR**。

**⚠️ 表述范围收窄（R-11）**：本表**不声称覆盖全部 P1**。尚有 8 条 P1（FR-1.6、FR-1.8、FR-2.2、FR-3.2、FR-3.3、FR-5.3、FR-6.3、FR-8.2）**未在本架构中单独展开**——它们**全部为 `REG`（保持现状 + 回归保护）**，不引入新设计，其验收完全由 `01-spec.md` 的既有行为描述 + ADR-021 的回归测试承担。**不再扩大架构文档范围。**

| 需求 | 设计落点 |
|---|---|
| FR-4.3 跳过（0/1 基口径） | §4.2 统一索引语义：**`current_index` 恒为"下一个待答索引"，前端一律 0 基读写**；跳过与作答共用同一推进函数 |
| FR-4.6 进度状态 | 同上；`question_status` 数组与 `questions` 等长同序，由服务端返回，前端只渲染 |
| FR-4.9 死参数收敛 | `models/schemas.py` 删除 `action`、`ReportRequest.user_id`；前端同步移除（`App.tsx:606,825`） |
| FR-4.10 跳过词单一来源 | 后端常量 → `GET /api/interview/config`，前端启动时获取 |
| FR-6.4 `json.loads` 保护 | `services/` 内统一 `safe_json_loads(text, default=None)`，替换 `user.py:110,118` |
| FR-7.6 删记录清通知 | 与 FR-8.6 同一事务内 `DELETE FROM notifications WHERE target_type='interview_record' AND target_id=?` |
| FR-8.1 趋势零填充 + 时区 | 服务端补 7 个日期点（缺补 0）；**日期基准统一为 `Asia/Shanghai`**，不再用 `func.date(utcnow)` |
| FR-8.6 删记录事务化 | 见 FR-7.6 |
| FR-11.3 条件 Hook | §4.3：守卫移至路由层，`AdminPanelContent` 不再在 `useState` 前 return |

---

## 4. TO-BE 目标架构

### 4.1 组件

```
┌──────────────── 浏览器 (React SPA + 路由) ────────────────┐
│  react-router-dom（已装未用→启用）；守卫在路由层            │
│  AuthContext（token/role/userId 集中 + 持久化）             │
│  统一 api 层（**24 处裸 fetch 全部迁入**）+ 401 拦截        │
│  ErrorBoundary（ADR-025）                                  │
│  领域组件：InterviewRoom / ReportView / NotificationCenter  │
│           ProfilePanel / QuestionBank / AdminPanel          │
│  基地址：相对路径 ''（生产同源）；开发走 Vite server.proxy   │
└──────────────────────┬────────────────────────────────────┘
                       │ HTTPS 同源 /api/*
┌──────────────────────▼────────────────────────────────────┐
│  Nginx :443 → 静态 dist + /api/* 反代 + /uploads/* 反代     │
│  必须转发 X-Forwarded-For / X-Forwarded-Proto（ADR-015）    │
└──────────────────────┬────────────────────────────────────┘
┌──────────────────────▼────────────────────────────────────┐
│  FastAPI（systemd 托管，uvicorn --workers 2）               │
│  core/ config · security · log_setup · errors             │
│  routers/ 薄 HTTP 层                                       │
│  services/ interview · ai · notification · resume · cleanup│
│  持久化态：验证码 · 限流 · 面试会话（ADR-004）               │
└──────┬───────────────────────────────┬────────────────────┘
       ▼                               ▼
 SQLite(WAL, foreign_keys=ON)+Alembic   uploads/（本机磁盘）
```

### 4.2 后端改造要点

| 点 | 做法 |
|---|---|
| 路由瘦身 | 状态机 → `services/interview_service.py`；AI → `ai_service.py`；通知 → `notification_service.py`；解析 → `resume_service.py`；清理 → `cleanup_service.py` |
| 鉴权补齐 | `/api/chat` 挂 **`require_user`**；`/api/resume/upload` 挂 **`get_current_user`**（避免管理员被 `require_user` 挡下）。**（更正 v2 的自相矛盾：矩阵与正文统一为此口径）** |
| **承接 N2 裁决** | `/api/start_interview` 为**唯一**会话创建入口；**`/api/chat` 的无会话角色扮演分支本轮废弃**；`/api/chat` 在无会话时返回 **409 + 明确指引**（"未检测到进行中的面试，请重新开始"），**不得静默降级** |
| **上线顺序（R-8）** | **FR-2.4（加鉴权 + 废弃无会话分支）必须与 FR-4.4（会话恢复）同批上线**。若先单独上线 FR-2.4，前端首条消息仍走旧路径，将直接收到 409 → 主流程不可用。spec 已就此给出裁决，架构在此重申为**发布门禁**。 |
| 索引语义唯一化 | `current_index` 恒为"下一个待答索引"；前端 0 基读写；**反问命中时走"不推进"分支**（回应但不改索引、不标记 answered） |
| 会话键统一 | 一律 `current_user.id`；`/api/chat` 不再读请求体 `user_id` |
| 禁用即时生效 | `get_current_user` 增加 `is_active` 校验 |
| 统一超时 | 所有 DeepSeek 调用经 `ai_service`，`timeout=30s` + 一次重试 + 明确降级 |
| 事务边界 | 审核（状态+通知）同事务；报告+会话状态同事务；**AI 调用一律在事务外**（ADR-004） |
| 幂等 | `save_interview` 幂等键；`start_interview` 活跃会话唯一（ADR-023） |
| 迁移治理 | 引入 Alembic；移除 `create_all()` 与手写 ALTER；**`stamp <基线修订>` → `upgrade head`**（ADR-009） |
| 日志 | 全局异常处理器记录堆栈，对外通用消息 |

### 4.3 前端改造要点

| 点 | 做法 |
|---|---|
| 路由化 | 启用 `react-router-dom`；**守卫放路由层**（`<Navigate>`），根治 `App.tsx:6` 的渲染期守卫（FR-11.3） |
| Auth 集中 | `AuthContext` 持有并**持久化 `userId`**；修正 D-2 的必要条件 |
| 会话重建 | 挂载时调 `GET /api/interview/session` 重建视图；无活跃会话时提供"继续 / 放弃重开"（ADR-022） |
| **API 层收敛** | **24 处裸 `fetch` 全部迁入统一 api 层**（v2 误称已收敛）——这是 ADR-016 滑动续期与统一 401 处理的**前置条件** |
| 视图拆分 | 按 §4.1 拆分，**单文件 ≤400 行** |
| 错误边界 | `ErrorBoundary` 包裹路由出口（ADR-025） |
| 缺陷修复 | 定时器改 `useRef` 持最新回调；`q.tags?.map` 可选链 |
| 基地址 | `config.ts` 默认 **相对路径 `''`**；开发期 `vite.config.ts` 的 `server.proxy` 指向 8000 |

---

## 5. 关键设计决策（ADR-001~025）

> 每条：**选项 → 推荐 → 理由 → 影响**。推荐均为明确结论。

### 5.1 数据与状态

#### ADR-001 技术栈
- **选项**：A 保持 FastAPI+React+TS+Tailwind｜B 换后端｜C 换前端框架
- **推荐**：**A**
- **理由**：D-1~D-17 全部是**实现层**缺陷，与框架选型无关；更换栈无法消除其中任何一条。
- **影响**：无需求变更。

#### ADR-002 数据库与 SQLite 运行契约
- **选项**：A 保持 SQLite + 补全运行契约｜B 迁移 PostgreSQL｜C 保持 SQLite + 专用串行写进程（+IPC）
- **推荐**：**A**
- **理由**：数据量与写并发极低（单机、日均面试个位数）。SQLite 的真实约束是**写并发数 + 锁持有时间**，因此必须落实下列配置；**并且要求所有写事务保持短小**（配合 ADR-004 把 AI 调用移出事务）。
- **配置契约**：
  ```python
  engine = create_engine(
      DATABASE_URL,
      connect_args={"check_same_thread": False, "timeout": 15},  # 与 busy_timeout 取同一值
      pool_pre_ping=True,
      isolation_level=None,      # ← v2.1 补：交出事务控制权，否则发不出 BEGIN IMMEDIATE（见下）
  )

  @event.listens_for(engine, "connect")
  def _sqlite_pragmas(dbapi_conn, _):
      cur = dbapi_conn.cursor()
      cur.execute("PRAGMA journal_mode=WAL")
      cur.execute("PRAGMA busy_timeout=15000")
      cur.execute("PRAGMA synchronous=NORMAL")
      cur.execute("PRAGMA foreign_keys=ON")
      cur.close()

  @event.listens_for(engine, "begin")
  def _begin_immediate(conn):
      # ADR-004 依赖 BEGIN IMMEDIATE 显式取写锁；SQLAlchemy 默认发 "BEGIN (implicit)"
      conn.exec_driver_sql("BEGIN IMMEDIATE")
  ```
  - **⚠️ v2.1 关键补正（R-6）**：实测 SQLAlchemy 2.0.50 + 原 `connect_args` 下，驱动 `isolation_level=''`，SQLAlchemy 发出的是 `BEGIN (implicit)`，**永远不会发 `BEGIN IMMEDIATE`**。而 ADR-004 的并发控制明确依赖它。若缺此配置：WAL 下**读事务升级为写**时，若期间他人已提交，SQLite 返回 `SQLITE_BUSY` **且 busy handler 不被调用** → `busy_timeout=15000` 完全无效，正是 ADR-002 想消除的 `database is locked`。**ADR-002 与 ADR-004 二者必须同时落地。**
  - `journal_mode` 设置后**校验返回值**（忙时可能静默失败），失败则记警告日志。
  - **worker 数上限 2~4**，依据是写并发而非 CPU。
  - 仍频繁 `database is locked` 时，回退单 worker 并评估选项 C（选项 C 需新增独立写进程 + IPC，与"零新增运维组件"目标冲突，须单独评估工作量）。
- **影响**：决定 ADR-004/006 是否成立。**若你预期写并发远超当前量级，应选 B**（则 ADR-004/006 需重做，工作量显著增加）。

#### ADR-004 会话 / 验证码 / 限流载体与并发控制 ⭐
- **选项**：A 保持进程内存｜**B 落 SQLite 表**｜C 引入 Redis
- **推荐**：**B**（显式依赖 ADR-002 的 WAL + busy_timeout；若选 C 则本节并发控制同样适用）
- **理由**：零新增运维组件达成多 worker；写并发极低。

**⚠️ 并发控制（v2.1 重写；v2 把 25s 的 AI 调用圈进写事务，会导致大面积 `database is locked`）**

`/api/chat` 的正确时序——**外部 AI 调用必须在事务外**，且**冲突时不得重放陈旧载荷**：

```
0. 【事务外·幂等前置】校验 seq：
     SELECT last_seq, last_reply FROM interview_sessions WHERE session_id=:sid
     若 :seq <= last_seq  → 直接返回已存的 last_reply，**不调用 AI、不推进**（真正实现 ADR-023）
1. 无锁 SELECT 取会话快照（含 version、current_index、status、expires_at、questions…）
     若 status != 'active' 或 expires_at <= now → 409 + 指引（见 ADR-022）
2. 【事务外】调用 DeepSeek 生成 feedback          ← 25~30s，不持任何写锁
3. 短事务（BEGIN IMMEDIATE，目标 <50ms）：
     UPDATE interview_sessions
        SET current_index   = :expected_next,     -- 基于快照的显式期望值，非 current_index+1
            question_status = :new_status,        -- 覆盖全部变更列
            user_answers    = :new_answers,
            last_seq        = :seq,               -- v2.1 补：last_seq 的写入点
            last_reply      = :reply,             -- v2.1 补：供幂等重放
            version         = version + 1,
            updated_at      = :now
      WHERE session_id = :sid
        AND version    = :snapshot_version        -- 乐观锁
        AND status     = 'active'                 -- v2.1 补：废弃/完成期间不得写入
        AND expires_at > :now                     -- v2.1 补：过期期间不得写入
4. rowcount == 0 → **不重试写**，直接返回 409 并携带最新 current_index/last_seq；
     前端据此重新拉取会话状态后由用户重发（ADR-022）。
     ⚠️ v2.1 关键更正：v2 的"重取快照并重试该 UPDATE"会用**陈旧载荷**（基于旧快照算出的
        user_answers/question_status 整列）覆盖他人已提交的新状态 → 多 worker 下静默串题、
        答案落到错误题目上。**冲突必须显式暴露为 409，绝不能静默重放。**
```

补充约束：
- **反问路径**：命中反问时**不推进索引**，仅返回 AI 回应（FR-4.2(a)），同样在事务外调用 AI。
- **`seq` 幂等（v2.1 补齐实现）**：v2 只建了 `last_seq` 列却无写入/比较点，属死列；现由第 0 步（前置去重，避免重复计费）与第 3 步（同一条 UPDATE 内更新 `last_seq`/`last_reply`）闭环。
- **报告生成路径同样需要 version 守卫**（v2.1 补）：ADR-007 所称"报告天然幂等"**仅在**"评分结果随会话持久化 + 置 `finished` 时带 version 条件"成立时才成立。做法：`generate_report` 在事务外调 AI 评分 → 短事务内 `UPDATE ... SET report=:json, status='finished', version=version+1 WHERE session_id=:sid AND version=:snapshot_version AND status='active'`；`rowcount==0` 返回 409。
- **事务边界**：报告落库与"会话置为 finished"同事务；AI 评分本身在事务外。
- **验证码/限流**：只对**失败**写入（成功不写），避免把高频路径变成写热点（ADR-014）。

- **影响**：**本轮最大架构改动**。决定 FR-1.3、FR-4.4、FR-4.11、NFR-5 的实现方式。

#### ADR-022 会话生命周期、TTL 与恢复/放弃 ⭐
- **选项**：A 无 TTL（现状）｜**B 惰性 + 定时清理**｜C 仅惰性
- **推荐**：**B**（写入 `expires_at`，读取时惰性判定；另有 systemd timer 定时清理）
- **理由**：惰性保证正确性，定时回收空间，无额外组件。
- **状态机（统一 v2 的**`finished` 布尔列 / `status` / "删会话" 三处矛盾）**：
  - `status ∈ {active, finished, abandoned}`，**删除**会话改语义为**置为 `finished`/`abandoned`**；`finished` 布尔列**删除**，只保留 `status`。
  - 生成报告成功 → `finished`；用户主动放弃或 TTL 到期 → `abandoned`。
- **TTL 参数**：会话 **2 小时**；验证码 300 秒；限流窗口 10 分钟；**黑名单 8 小时**（v2.1 修正：必须 ≥ ADR-016 的续期绝对上限，否则被吊销令牌会复活 —— 见 ADR-016）。
- **⚠️ 必须同时提供"解除锁定"的能力（v2 遗漏，否则用户被锁在门外）**：
  `UNIQUE(user_id) WHERE status='active'` + 2 小时 TTL 会导致**浏览器崩溃/断网后 2 小时内无法开新课**（409）。因此必须提供：
  1. `GET /api/interview/session` → 返回当前活跃会话（无则 `null`）；
  2. `POST /api/interview/abandon` → 将活跃会话置 `abandoned`；
  3. 前端在收到 409 时**明确询问**"继续上次面试 / 放弃并重新开始"，绝不静默失败。
- **⚠️ 过期行自愈（v2.1 补，R-10）**：惰性判定存在时序缺口 —— 一行可能 `expires_at` 已过但 `status` 仍为 `active`，它**仍占着部分唯一索引**，导致"`start_interview` 返回 409，但随后 `GET /api/interview/session` 又惰性返回 `null`"的自相矛盾。**修法**：`start_interview` 在插入前先执行自愈
  `UPDATE interview_sessions SET status='abandoned' WHERE user_id=:uid AND status='active' AND expires_at <= :now`，
  再尝试插入；**且 409 响应体必须直接携带会话摘要**（`current_index`/`last_seq`/`total`），使前端无需额外一次往返即可恢复。
- **影响**：FR-4.1、FR-4.4、FR-4.11。

#### ADR-023 幂等与重试语义
- **选项**：A 不引入｜**B 关键写操作引入幂等键**｜C 全接口 `Idempotency-Key` 头
- **推荐**：**B**
- **理由**：引入统一重试后，`save_interview` 无幂等保护会产生重复记录；AI 评分重试会重复计费。
- **做法**：
  - `interview_records.client_token VARCHAR UNIQUE`（**须进 `SaveInterviewRequest` 契约**，v2 遗漏）；SQLite 下**不能用 `ADD COLUMN ... UNIQUE`**，须 `batch_alter_table` 重建表（ADR-009）。
  - `start_interview`：`UNIQUE INDEX(user_id) WHERE status='active'`；冲突返回 **409**，前端按 ADR-022 提供"继续/放弃"。
  - `chat`：客户端 `seq` 幂等。
- **影响**：FR-4.1、FR-6.1。

### 5.2 接口与契约

#### ADR-003 鉴权方案
- **选项**：A 无状态 JWT + 补齐接口鉴权｜B JWT + 服务端黑名单｜C 服务端 Session + Cookie
- **推荐**：**A 作 P0 止血 + B 作 P1**
- **理由**：真正的漏洞是接口裸奔（D-1），先止血。
- **B 的实现前提**：`create_access_token`（`auth.py:109-117`）**当前无 `jti`**，须增加；`token_blacklist` 需 `INDEX(jti)` + **8 小时 TTL 清理**（须 ≥ ADR-016 的续期绝对上限，否则被吊销令牌会复活）。
- **关于选项 C**：v2 曾称"已收敛全部请求、改动面仅 1 个文件 + 8 处调用"——**该论断为假**。实测 `App.tsx` 有 **24 处裸 `fetch`**（23 处 `${API_BASE_URL}` 模板串 + **`:936` 的 `fetch(url)`**，该 `url` 由登录/注册分支拼出）+ **8 处 `authFetch`**（**合计 32 处调用点**）。故 C 的改动面需按 **32 处**重估；**仍不推荐 C**，理由是收益（CSRF 防护）与改动面不匹配，**而非**"改动小"。
- **影响**：FR-1.4、FR-1.5。

#### ADR-007 AI 调用模型与降级契约
- **选项**：A 同步无超时｜**B 同步 + 统一超时/重试/降级**｜C 任务队列异步化
- **推荐**：**B**
- **理由**：C 需 Redis/RabbitMQ 与 worker，超增量范围；B 即消除 D-4、D-9 两个 P0。
- **降级契约**：`POST /api/generate_report` 失败返回 **HTTP 503 + `Retry-After`**（而非 v1 的 `{"error": ...}`，那会让前端字段全 `undefined`）；前端走统一错误组件 + 重试按钮。
  - 重试**仅限幂等操作**；生成报告以**服务端会话**为准，故天然幂等。
  - 超时预算：单次 ≤30s；前端等待上限 45s 并显示进度。
- **影响**：NFR-3、FR-5.2。

#### ADR-016 JWT 有效期、续期与绝对上限 ⭐（v2.1 重写为可执行）
- **选项**：A 30 分钟固定｜**B 30 分钟 + 滑动续期 + 绝对上限**｜C 短 token + 刷新令牌
- **推荐**：**B**
- **理由**：一次面试本身 15 分钟，加上上传与报告生成，固定 30 分钟会导致长面试被踢出，而 401 即统一登出 → 丢进度。
- **v2.1 补出可执行细节（v2 仅写"响应头回写新 token"，不可执行）**：
  1. **回写头名固定为 `X-Refreshed-Token`**；
  2. **CORS 必须暴露该头**：`main.py:24` 的 `expose_headers` 当前只有 `X-Captcha-Id`，须加入 `X-Refreshed-Token`，否则跨域下前端读不到；**且 CORS 来源不得配置为通配 `*`**（浏览器在 credentials 模式下不允许读取通配来源的响应头）；
  3. **前端落库路径**：由统一 api 层读取该头并写 `localStorage` —— 因此 **ADR-005 的"32 处调用点收敛"是本条的前置条件**；
  4. **绝对上限**：token 增加 `auth_time` 声明，**自首次签发起 8 小时内可续期**，超过则拒绝续期并要求重新登录（防止被窃 token 无限续命）；
  5. **`jti` 语义**：续期**沿用同一 `jti`**，使 ADR-003 的黑名单能一次性吊销整条续期链。
- **v2.1 补出 3 处落地细节（R-4/R-9）**：
  - **续期触发条件**：仅在请求携带有效 token 且**剩余有效期 < 50%（<15 分钟）**时才回写新 token，避免每次请求都刷新与放大写放大。
  - **超 8h 的 UX**：续期被拒时返回 `401 + X-Token-Expired: absolute`，前端**必须先提示"登录已超时，请重新登录"并保留面试进度（会话已在服务端，可恢复）**，不得静默跳登录页丢进度。
  - **🔴 安全漏洞修正**：黑名单清理 TTL 必须 **≥ 绝对上限（8 小时）**。v2.1 初稿的 `token_blacklist` 清理为 30 分钟（ADR-003/ADR-022/§6.3），而续期链可达 8 小时 → **被吊销的令牌会在 30 分钟后"复活"**，吊销形同虚设。已同步修正为 **8 小时**。
  - **顺序依赖（须知悉）**：`jti` 当前**不存在**（`auth.py:109-117`），且它挂在 ADR-003 **选 B（P1）** 之下；若 ADR-003 不选 B，则 ADR-016 第 5 点（沿用 jti 以支持吊销）**无落点** —— 此时应明确接受"**本轮无服务端吊销能力**"的取舍。
- **影响**：FR-1.2、FR-1.4。**这是 v1 完全遗漏、v2 未写清执行、v2.1 补齐落地的 P0 级风险。**

#### ADR-018 密码策略单一来源
- **选项**：A 前后端各写一套｜**B 抽出共享规则 + 后端强制**｜C 仅后端校验
- **推荐**：**B**
- **理由**：现状注册规则前端 8-16/≥2 类/无 6 连重（`App.tsx:2196-2203`），后端只判空（`auth_router.py:44-48`）；改密前端 6 位（`App.tsx:1173`）对后端 8 位（`user.py:100`）。B 同时修复 FR-1.1 与 FR-1.5。
- **影响**：FR-1.1、FR-1.5。

#### ADR-010 简历问题生成
- **选项**：A 保持硬编码 7 题｜**B 默认启用 AI + 失败降级**｜C 仅对技术栈段落 AI 抽取 + 模板化生成
- **推荐**：**B**（成本敏感则 C）
- **理由**：A 使核心卖点失效（D-10）。
- **影响**：FR-3.4、FR-3.5。**题目数单一来源常量** `QUESTION_COUNT`，供 `generate_questions`、提示词、以及**前端经响应体的 `total` 字段**共用（跨进程无法共享常量，v2 措辞已更正）。`RESUME_QUESTION_MODE` 改为**默认启用**。成本：每次上传 1 次调用（约 3k input token）。

#### ADR-024 API 版本化与契约稳定性
- **选项**：A 无版本前缀（现状）｜**B 无前缀 + 契约测试固化**｜C 引入 `/api/v1`
- **推荐**：**B**
- **理由**：前后端**同仓同发**，无外部消费者。
- **本轮破坏性/新增契约变更实为 13 处**（v2 误称 3 处，v2.1 初稿误称 6 处）：
  ① `/api/chat` 取消请求体 `user_id`；② 新增 `session_id`；③ 新增 `seq`（含幂等语义）；④ 新增 `GET /api/interview/session`；⑤ 新增 `POST /api/interview/abandon`；⑥ 新增 `GET /api/interview/config`（跳过词等前端常量）；⑦ 报告失败改 **503 + `Retry-After`**；⑧ 无会话/版本冲突改 **409**（响应体携带最新 `current_index`/`last_seq`）；⑨ 响应头新增 **`X-Refreshed-Token`** 并加入 CORS `expose_headers`；⑩ 新增 **`auth_time`** 声明；⑪ `interview_records` 新增 **`client_token`**；⑫ `users` 新增 **`must_change_password`**；⑬ 续期被拒时新增 **`X-Token-Expired: absolute`**。
  → **必须全部回写到 `docs/01-spec.md` 的验收标准**（spec §9 规则 5）。
- **影响**：阶段 9 的对照基准。

### 5.3 前端

#### ADR-005 前端工程化程度与测试框架 ⭐
- **选项**：A 保持单文件｜**B 渐进重构**（路由 + 拆视图 + AuthContext + API 层收敛，不引入状态库/组件库）｜C 全面重构
- **推荐**：**B**
- **理由**：`react-router-dom` 已在 `package.json:15` 且 `src/` 0 命中 → 零新增依赖；全局态只有登录态一项，Context 足够。
- **⚠️ v2.1 新增前置**：**24 处裸 `fetch` 必须迁入统一 api 层**（合计 32 处调用点）——这是 ADR-016（滑动续期）与统一 401 处理的**硬前置**，工作量须计入（v2 误以为已收敛）。
- **测试**：**Vitest + React Testing Library**（与 Vite 8 同源）。覆盖：AuthContext 持久化、401 拦截、`AdminPanelContent` 修复后渲染、`q.tags` 缺失不崩。
- **构建门禁**：`tsc -b && vite build` 须纳入验收；**产物中不得出现 `127.0.0.1` 字面量**。
- **量化目标**：单文件 ≤400 行；`App.tsx` 降为路由装配。
- **影响**：NFR-4、NFR-6。

#### ADR-025 错误边界与前端错误呈现
- **选项**：A 不引入｜**B 顶层单一 ErrorBoundary**｜C 路由级 + 组件级多层
- **推荐**：**B**（关键视图可局部加）
- **理由**：当前任一 `map` 抛错即整页白屏（全项目 0 命中）。
- **影响**：NFR-11。

### 5.4 部署与运维

#### ADR-006 部署方式 ⭐
- **选项**：A `nohup uvicorn` 单进程｜**B systemd + 多 worker**｜C 全量 Docker Compose
- **推荐**：**B**
- **理由**：最小改动达成"多进程 + 开机自启 + 崩溃重启"。
- **⚠️ 处置 admin 启动竞态**：`main.py:43-58` 的 `@app.on_event("startup")` 在**每个 worker** 执行 check-then-insert；空库首启时 N 个 worker 同时 INSERT，`users.username` UNIQUE 会让除一个外全部抛 `IntegrityError` → worker 启动失败。
  **处置**：移到 `ExecStartPre=` 的一次性脚本 `scripts/init_db.py`（同时承担 ADR-009 的 `upgrade head`）。
- **systemd 单元**：
  ```ini
  [Service]
  WorkingDirectory=/root/ai-interview-simulator/backend
  ExecStartPre=/root/.../venv/bin/python -m scripts.init_db
  ExecStart=/root/.../venv/bin/uvicorn main:app \
      --host 127.0.0.1 --port 8000 --workers 2
      # 不需要 --proxy-headers：uvicorn 0.33.0 默认 proxy_headers=True
      # 且 forwarded_allow_ips 默认取 env FORWARDED_ALLOW_IPS 或 "127.0.0.1"
  Restart=always
  ```
  - 监听 `127.0.0.1`，由 Nginx 唯一对外。
  - **须确保 `FORWARDED_ALLOW_IPS` 未被设为 `*`**（否则可伪造来源 IP）。
  - 日志落 journald。
- **影响**：NFR-5、NFR-8。强依赖 ADR-002（写事务短小）与 ADR-015（Nginx 转发头）。

#### ADR-011 CORS / 同源策略
- **选项**：A 保持硬编码 `localhost:5173/5174`｜**B 允许来源走环境变量**｜C 生产同源后移除
- **推荐**：**B**（生产同源可为空；`expose_headers` 须含 `X-Captcha-Id` 与 **`X-Refreshed-Token`**）
- **理由**：生产经反代后同源，CORS 不再需要；开发期须白名单。
- **影响**：NFR-10、FR-1.2（续期）。

#### ADR-012 HTTPS / TLS 落地
- **选项**：A 仅 HTTP｜**B Certbot + 自动续期**｜C 云托管证书
- **推荐**：**B**
- **理由**：验证码、JWT、密码均在传输中。
- **步骤**：Nginx 443 server 块 → `certbot --nginx` → HTTP→HTTPS 301 → `certbot renew` 定时任务。HSTS 首期不上。
- **影响**：NFR-1、NFR-10。

#### ADR-013 上传文件安全策略 ⭐（v2.1 更正事实错误）
- **选项**：A 保持现状（仅 MIME）｜**B 服务端全量校验 + 配额 + 孤儿清理**｜C 对象存储
- **推荐**：**B**
- **理由**：简历有大小校验（`interview.py:286`），**头像完全没有**（`user.py:53-69` 只查客户端可控的 `content_type`），`shutil.copyfileobj` 直写磁盘 → 无界磁盘写。
- **做法**：大小上限（简历 5MB / 头像 2MB）；**按文件头（魔数）校验**；扩展名白名单；统一重命名；替换头像时删除旧文件；删用户时清理其头像；目录总量配额告警。
- **⚠️ git 跟踪问题的正确修法（v2 判断错误）**：
  - 实测：`.gitignore:37` 是 **`uploads/avatars/*`（含斜杠 → 锚定仓库根）**，**不匹配** `backend/uploads/avatars/*`。`git check-ignore --no-index backend/uploads/avatars/x.jpg` → **exit 1（未被忽略）**。
  - 因此仅 `git rm --cached` 不够——新头像会变成"未跟踪且未忽略"，**脏工作区问题不会消失**。
  - **正确做法**：① `.gitignore` 增加 **`backend/uploads/avatars/*`**，并把现有 `:38` 的 `!uploads/avatars/.gitkeep` **对称改为 `!backend/uploads/avatars/.gitkeep`**（否则否定规则与新路径不同源）；② `git rm --cached` 时**显式列出**头像文件（勿用宽 glob —— 已干跑证实 `backend/uploads/avatars/*` 会匹配 **8 条含 `.gitkeep`**）；③ 历史提交中的 7 张真实头像（含 **1 张 1.4MB PNG**，说明历史上无任何大小约束）无法靠 `rm --cached` 消除，**须明确取舍并给出推荐**：**推荐接受残留**（单人私有仓库，重写历史收益不抵成本）；若确需清除则用 `git filter-repo`，**注意会重写全部 commit SHA**；④ 与 FR-10.4 的配额/孤儿清理联动。
- **影响**：FR-3.1、FR-10.4、NFR-1。

#### ADR-014 限流载体与算法
- **选项**：A 内存字典｜**B DB + 滑动窗口**｜C Redis + 令牌桶
- **推荐**：**B**（与 ADR-004 同载体）
- **理由**：限流是高频写操作，故必须 ① 只对**失败**计数；② `INDEX(ip, attempted_at)`；③ 定期清理。覆盖注册、登录、上传、`chat`。
- **影响**：FR-1.3、NFR-1。

#### ADR-015 反向代理下的客户端 IP ⭐（v2.1 更正错误因果）
- **选项**：A 依赖 uvicorn 默认（现状）｜**B Nginx 转发头 + 显式声明可信代理**｜C 应用层手工解析
- **推荐**：**B**
- **⚠️ v2 的错误因果更正**：v2 称"必须开 `--proxy-headers`，否则 `request.client.host` 恒为 127.0.0.1，属 P0 阻断点"。**实测 uvicorn 0.33.0 的 `Config.__init__`：`proxy_headers=True`（默认已开），`forwarded_allow_ips=None` 时回落到 `os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")`** —— 两个 flag 都等于默认值，故**不加也**会解析 `X-Forwarded-For`。该论断是**错误诊断**。
- **真正的前置条件（v2 完全缺失）**：**Nginx 必须转发该头**。§10 的 Nginx 配置须包含：
  ```nginx
  location /api/ {
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header Host              $host;
      proxy_set_header X-Real-IP         $remote_addr;
      proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;   # ← 关键
      proxy_set_header X-Forwarded-Proto $scheme;
  }
  ```
  并确认 `FORWARDED_ALLOW_IPS` 未被设为 `*`。
  **若现网 Nginx 未转发该头，`request.client.host` 确会退化为代理地址 → 一人连错 5 次锁死全员**——风险真实，但**根因在 Nginx，不在 uvicorn flag**。
- **影响**：FR-1.3、NFR-1。

#### ADR-017 管理员初始口令与轮换 ⭐（v2 缺失，仅有悬空引用）
- **选项**：A 保持 `admin123` 硬编码（现状）｜**B 首次启动从环境变量注入 + 无则生成随机口令打印到日志**｜C 保留默认口令但强制首登修改
- **推荐**：**B + C 组合**
- **理由**：`main.py:50` 硬编码 `admin123`；更关键的是 `:47` 的 `if not admin_user` 使其**对已存在的库永不生效**（本机库中 admin 已存在）→ v2 提出的"首启随机口令"按字面实现**什么也不会发生**。
- **做法**：
  1. `ADMIN_INIT_PASSWORD` 环境变量优先；未设置则生成随机口令并打印一次到日志；
  2. `users` 增加 `must_change_password BOOLEAN DEFAULT 0`；首登强制修改；
  3. 对**既有库**：提供 CLI `scripts/reset_admin_password.py`，并**作废 `00-topic.md` 记录的公开凭据 `admin/admin123`**。
- **影响**：NFR-1(d)。**P0 级，v2 仅有悬空引用 `ADR-017''`，无实际设计。**

#### ADR-019 备份与恢复
- **选项**：A 无备份｜**B 定时 `sqlite3 .backup` + uploads 打包**｜C 云数据库自动备份
- **推荐**：**B**
- **理由**：NFR-8 要求；ADR-004 把会话迁入 DB 后，DB 损坏后果更严重。
- **做法**：`sqlite3 interview.db ".backup"`（WAL 安全）每日；保留 7 份；含 `uploads/` 与加密 `.env`；每季度恢复演练。
- **影响**：NFR-8。

#### ADR-009 数据库迁移治理 ⭐（v2.1 修正 `stamp head` 错误）
- **选项**：A 保持 `create_all` + 手写 ALTER｜**B 引入 Alembic**
- **推荐**：**B**
- **⚠️ v2 的 `alembic stamp head` 是错的**：`stamp head` 表示"已应用全部迁移"，于是 **4 张新表 + `interview_records.client_token` + 删除 `link_url` 都不会执行** → 上线即崩。
- **正确流程（v2.1 补 runbook 细节）**：
  - **全新空库**：`alembic upgrade head`（全部迁移依次执行）——由 `scripts/init_db.py` 调用。
  - **既有库**：先 **备份** `interview.db` → `alembic stamp <基线修订>` → 再 `alembic upgrade head` 以应用新表与新列。
    **⚠️ 基线的 schema 必须镜像"`create_all()` + 手写 ALTER 补列之后"的真实状态**（`database.py:73-80` 已补入 `type`/`link_url`/`target_type`/`target_id` 四列），不能只写"现有 3 表结构"，否则基线与线上库不一致。
  - **`init_db.py` 须自行判定走哪条路**（`alembic_version` 表是否存在 + 3 张业务表是否存在），并在日志中明示所选路径；**空库/既有库的分岔判定与执行者必须写进该脚本，不能靠人工记忆**。
  - **迁移后校验**：执行 `alembic check`（确认无待生成的 schema 差异）。
  - **移除** `create_all()`（`database.py:65`）与手写 ALTER（`:68-82`）——与 Alembic 并存会造成迁移历史与 schema 不一致，且 import 期 `create_all` 干扰 autogenerate。
  - **SQLite 的 ALTER 限制**：`ADD COLUMN ... UNIQUE` 不可行（`client_token` 即属此类，已实测报 `Cannot add a UNIQUE column`），须用 `batch_alter_table` 重建表；`DROP COLUMN` 需 ≥3.35（本机 3.35.5 实测满足）。
  - `alembic` 须加入 `backend/requirements.txt`（当前 42 行无此依赖，已实测）。
- **影响**：NFR-8、ADR-004、ADR-023。

#### ADR-020 契约级参数固化
- **选项**：A 散落代码中｜**B 集中为配置常量 + 回归测试**
- **推荐**：**B**
- **参数**：通知去重 5 分钟；未读轮询 30 秒；验证码 TTL 300 秒；限流窗口 10 分钟 / 5 次；面试时长 15 分钟；会话 TTL 2 小时；续期绝对上限 8 小时。
- **影响**：FR-7.2、FR-7.5、FR-1.3。

#### ADR-021 测试框架与范围
- **选项**：A 不引入｜**B pytest + TestClient + Vitest + RTL**｜C 仅后端
- **推荐**：**B**
- **理由**：NFR-6 为 P0；C 无法回归"前端拆分是否正确"。
- **范围**：后端覆盖全部 26 条 P0 相关接口（鉴权、越权、会话推进与并发、幂等、降级契约）；测试库用**独立文件 SQLite**（非内存，以覆盖 WAL/事务行为）。
- **前置**：删除 `.gitignore:45` 的 `**/test_*.py`。
- **影响**：NFR-6。

### 5.5 其他

#### ADR-008 通知渠道
- **选项**：A 仅站内｜B 站内 + 邮件｜C 站内 + 邮件 + 企微/短信
- **推荐**：**A**
- **理由**：B/C 需第三方服务（规则 6 点名的关键决策）。
- **影响**：FR-1.7 不可做（依赖邮件）。

#### ADR-026 数据清理触发方式（配合 ADR-022）
- **选项**：A APScheduler 进程内定时｜**B systemd timer 调用一次性脚本**｜C 懒清理 only
- **推荐**：**B**
- **理由**：多 worker 下 APScheduler 会**每 worker 一份**→ 重复清理；systemd timer 是单实例，且无需新增 Python 依赖（APScheduler 未在 `requirements.txt` 中，已实测 42 行无此依赖）。
- **做法**：`scripts/cleanup.py` + `cleanup.timer`（如每 10 分钟）。
- **影响**：ADR-022。
- **编号说明**：本条原为 `ADR-024'`（带撇号、与 ADR-024 重号、§11 无行），v2.1 终审后**改为 ADR-026** 并补入 §11。

---

## 6. 数据模型设计

### 6.1 既有 3 表（非破坏性保留）

- `interview_records` **增加** `client_token VARCHAR UNIQUE`（ADR-023；SQLite 须 `batch_alter_table`）。
- **删除**死列 `notifications.link_url`（需 SQLite ≥3.35，本机 3.35.5 实测满足）。
- `users` **增加** `must_change_password BOOLEAN NOT NULL DEFAULT 0`（ADR-017）。
- relationship 增加 `cascade="all, delete-orphan"`（当前 `database.py:45-47,60-62` 未声明）。

### 6.2 新增表（DDL 已实测可执行）

```sql
-- 面试会话（ADR-004 + ADR-022 + ADR-023）
CREATE TABLE interview_sessions (
  session_id      VARCHAR PRIMARY KEY,          -- UUID
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role            VARCHAR NOT NULL,
  questions       TEXT    NOT NULL,             -- JSON
  question_status TEXT    NOT NULL,             -- JSON
  user_answers    TEXT    NOT NULL,             -- JSON
  current_index   INTEGER NOT NULL DEFAULT 0,   -- 语义：下一个待答索引
  last_seq        INTEGER NOT NULL DEFAULT 0,   -- seq 幂等：上次已处理的客户端序号（ADR-023）
  last_reply      TEXT,                         -- v2.1 补：上次响应正文，供 seq 幂等重放（ADR-004 第 0 步）
  version         INTEGER NOT NULL DEFAULT 0,   -- 乐观锁
  status          VARCHAR NOT NULL DEFAULT 'active',  -- active/finished/abandoned
  created_at      DATETIME NOT NULL,
  updated_at      DATETIME NOT NULL,
  expires_at      DATETIME NOT NULL
);
CREATE INDEX idx_sessions_user ON interview_sessions(user_id);
CREATE UNIQUE INDEX idx_sessions_active ON interview_sessions(user_id) WHERE status = 'active';

-- 验证码
CREATE TABLE captcha_store (
  captcha_id VARCHAR PRIMARY KEY,
  code       VARCHAR NOT NULL,
  expires_at DATETIME NOT NULL,
  used       BOOLEAN NOT NULL DEFAULT 0
);
CREATE INDEX idx_captcha_expires ON captcha_store(expires_at);

-- 失败限流
CREATE TABLE auth_attempts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ip           VARCHAR NOT NULL,
  attempted_at DATETIME NOT NULL
);
CREATE INDEX idx_attempts_ip_time ON auth_attempts(ip, attempted_at);

-- 令牌黑名单（ADR-003 选 B 时启用）
CREATE TABLE token_blacklist (
  jti        VARCHAR PRIMARY KEY,
  expires_at DATETIME NOT NULL
);
CREATE INDEX idx_blacklist_expires ON token_blacklist(expires_at);
```

**已实测验证**（本机 SQLite 3.35.5）：部分唯一索引语法与语义成立（同一用户第二个 `active` 会话被拒、`finished` 可并存）；`foreign_keys=ON` 下 `ON DELETE CASCADE` 生效（删用户后会话归零）。

**设计说明**
- **`session_id` 作 PK**：现状以 `user_id` 为键是**静默覆盖**；改为 UUID 主键 + 部分唯一索引，把冲突变成**显式 409**，并按 ADR-022 提供"继续/放弃"。
- **`finished` 布尔列已删除**：v2 同时存在 `finished` 与 `status`，语义重复；统一由 `status` 表达。
- **`foreign_keys=ON` 必须开启**：SQLite 默认关闭，故现有 `ForeignKey` **当前根本不生效**；开启后须检查既有脏数据。

### 6.3 数据生命周期（ADR-022）

| 表 | TTL | 清理方式 |
|---|---|---|
| `captcha_store` | 300 秒 | 惰性 + `cleanup.timer` |
| `auth_attempts` | 10 分钟 | `cleanup.timer` |
| `interview_sessions` | 2 小时 | 惰性置 `abandoned` + `cleanup.timer` |
| `token_blacklist` | **8 小时**（≥ ADR-016 的续期绝对上限，否则被吊销令牌会复活） | `cleanup.timer` |

触发：**systemd timer（单实例）**，非 APScheduler（避免每 worker 重复清理）。

---

## 7. 安全架构

| 面 | 现状 | 目标 |
|---|---|---|
| 传输 | 仅 HTTP | HTTPS + Certbot（ADR-012） |
| 认证 | JWT 30min 固定、无 `jti`、无吊销 | 滑动续期 + 8h 绝对上限（ADR-016）；可选黑名单（ADR-003） |
| 授权 | 两接口缺失；**禁用不生效** | 白名单外全鉴权（FR-2.4）；`is_active` 校验（FR-2.6） |
| 密码 | 注册仅前端；改密 6 vs 8 | 后端强制 + 单一来源（ADR-018） |
| 密钥 | **`SECRET_KEY` 硬编码回退** | 缺失即启动失败（NFR-1c） |
| 初始口令 | `admin123` 硬编码，**对既有库不生效** | env 注入 / 随机 + 强制改密 + CLI（ADR-017） |
| 上传 | 简历有大小校验；**头像完全没有** | 全量校验 + 配额 + 孤儿清理（ADR-013） |
| 限流 | 仅登录/注册，内存态 | DB 滑动窗口 + 覆盖上传/chat（ADR-014） |
| 客户端 IP | 依赖 uvicorn 默认；**Nginx 是否转发未知** | Nginx 显式转发 XFF + 可信代理白名单（ADR-015） |
| 数据入库 | **7 个真实头像已被跟踪且未被 ignore 覆盖** | 修 `.gitignore` + `rm --cached` + 历史取舍（ADR-013） |

---

## 8. 目标目录结构与迁移策略

```
backend/
├─ main.py                 # 应用装配（薄）
├─ core/  config.py · security.py · log_setup.py · errors.py
├─ db/    base.py（engine+PRAGMA） · models.py（ORM）
├─ models/schemas.py       # Pydantic（清理死参数）
├─ routers/                # 薄 HTTP 层
├─ services/ interview_service.py · ai_service.py
│            notification_service.py · resume_service.py · cleanup_service.py
├─ migrations/             # Alembic（ADR-009）
├─ scripts/  init_db.py · cleanup.py · reset_admin_password.py
└─ tests/                  # pytest（ADR-021）
```

> **⚠️ 迁移策略**：目录重组会破坏**全部**顶层扁平导入。G-1 要求"可分批上线"，故**不得一次性大爆炸重构**，分 4 步：
> **步 1** 新增 `core/`/`services/`/`db/`，**保留旧模块作为 shim**（`config.py` 内 `from core.config import *`），全量测试通过；
> **步 2** 逐个迁移路由导入；**步 3** 删除 shim；**步 4** 调整 Alembic 与 `scripts/`。
> `database.py` 拆为 `db/base.py`（engine/PRAGMA）+ `db/models.py`（ORM），建表与迁移职责交给 Alembic。
> **同时**：删除 `.gitignore:45` 的 `**/test_*.py`。

---

## 9. 错误处理与日志

| 层 | 做法 |
|---|---|
| AI 服务 | `timeout=30s` + 1 次重试；失败返回 **503 + `Retry-After`**；**绝不用全 0 分冒充评分** |
| 事务 | 审核（状态+通知）同事务；报告+会话状态同事务；**AI 调用一律在事务外**；失败整体回滚 |
| 业务异常 | `HTTPException` + 明确状态码与用户可读消息（无会话 → 409） |
| 未捕获异常 | 记录堆栈到日志，对外通用消息（NFR-2） |
| 前端 | 401 → 统一登出跳登录；业务错误 → 统一提示组件；顶层 `ErrorBoundary` 兜白屏 |

---

## 10. 部署拓扑（ADR-006）

```nginx
server {
    listen 443 ssl;
    server_name <your-domain>;
    root /var/www/html;                       # frontend/dist

    client_max_body_size 8m;                  # ← v2.1 补（R-2）：Nginx 默认 1m，简历上限 5MB，缺此行 >1MB 上传被 413 拦下

    location / { try_files $uri $uri/ /index.html; }
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;  # ADR-015 关键
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;          # ← v2.1 补：ADR-007 的 30s + 1 次重试逼近默认 60s，易 504
    }
    location /uploads/ { proxy_pass http://127.0.0.1:8000; }
}
server { listen 80; return 301 https://$host$request_uri; }
```

```
用户浏览器 ──HTTPS:443──▶ Nginx ──┬─ 静态 dist（同源）
                                  ├─ /api/*     ─▶ 127.0.0.1:8000
                                  └─ /uploads/* ─▶ 127.0.0.1:8000
                                                      │ systemd 托管
                                                uvicorn --workers 2
                                                      │
                                        SQLite(WAL, FK=ON) + uploads/
                                                      │
                                                 DeepSeek API
清理：systemd timer → scripts/cleanup.py
备份：每日 sqlite3 .backup + uploads 打包（7 份）→ 异地
日志：journald + Nginx access/error
```

---

## 11. 决策汇总（请逐条裁决）

| ADR | 决策点 | 推荐 |
|---|---|---|
| 001 | 技术栈 | A 保持 |
| 002 | 数据库与 SQLite 契约 | A 保持 + WAL/busy_timeout 15s/foreign_keys |
| 003 | 鉴权 | A 止血 + B 黑名单(P1) |
| **004** | **会话/验证码/限流载体 + 并发控制** | **B 落 SQLite + AI 事务外 + 乐观锁** |
| 005 | 前端工程化 + 测试框架 | B 渐进重构 + 32 处调用点收敛 |
| **006** | **部署** | **B systemd + workers 2**（proxy-headers 非必需） |
| 007 | AI 调用与降级契约 | B 超时 + 503 |
| 008 | 通知渠道 | A 仅站内 |
| 009 | 迁移治理 | B Alembic（`stamp <基线>` → `upgrade head`） |
| 010 | 简历问题生成 | B 默认启用 AI |
| 011 | CORS/同源 | B 配置化 + 暴露续期头 |
| 012 | HTTPS | B Certbot |
| 013 | 上传安全 | B 全量校验 + 修 `.gitignore` |
| 014 | 限流载体与算法 | B DB 滑动窗口 |
| **015** | **反代客户端 IP** | **B Nginx 转发 XFF**（非 uvicorn flag） |
| **016** | **JWT 有效期与续期** | **B 滑动续期 + 8h 绝对上限** |
| **017** | **管理员初始口令与轮换** | **B+C env 注入 + 强制改密 + CLI** |
| 018 | 密码策略单一来源 | B 后端强制 |
| 019 | 备份与恢复 | B 定时备份 |
| 020 | 契约参数固化 | B 常量集中 |
| 021 | 测试框架 | B pytest + Vitest |
| 022 | 会话 TTL/恢复/放弃 | B 惰性 + timer + 409 可解除 |
| 023 | 幂等与重试语义 | B 关键写操作幂等键 |
| 024 | API 版本化 | B 契约测试替代 |
| 025 | 错误边界 | B 顶层 ErrorBoundary |
| **026** | **数据清理触发方式** | **B systemd timer 单实例** |

**下一步**：你确认（或修改）以上 25 条决策后，我进入 **阶段 5 任务拆解**，产出 `docs/03-tasks.md`。
