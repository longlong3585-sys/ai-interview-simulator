# 00 · 题目与现状盘点（逆向版）

> **阶段 0 产物** — 澄清与假设
> **文档性质**：逆向工程。本文不描述"项目应该是什么"，只描述"我接手时，代码里实际有什么"。
> **盘点对象**：`D:\AI 驱动的智能面试准备与模拟系统`
> **代码版本**：`main` 分支 @ `13c0321`（"Remove docx and other changes"）
> **盘点日期**：2026-09-28
> **盘点方法**：通读全部后端源码与前端主文件 + 实际启动前后端服务 + 真实 HTTP 探测接口行为（见 §12）

---

## 0. 一句话定位

一个 **AI 模拟技术面试的 Web 全栈应用**：用户上传简历 → 系统生成针对性面试题 → AI 扮演面试官多轮追问 → 结束后 AI 给出多维度评分报告 → 记录入库 → **管理员人工审核并反馈"通过/未通过"+ 评语** → 用户收到站内通知。

它最容易被误读的地方：**这不是一个自助式的练习工具，而是一条带人工审核环节的"模拟招聘流水线"**。管理员是这个系统的核心角色，不是附属功能。

---

## 1. 实测技术栈

| 层 | 实际使用 | 证据 |
|---|---|---|
| 后端框架 | FastAPI 0.124.4 + Starlette 0.44.0 + uvicorn 0.33.0 | `backend/requirements.txt` |
| ORM | SQLAlchemy 2.0.50（声明式 Base） | `backend/database.py` |
| 数据库 | SQLite（`backend/interview.db`，约 100KB） | `.env` → `DATABASE_URL=sqlite:///./interview.db` |
| 数据库驱动 | `psycopg2-binary` 已安装但**当前未使用**；`.env.example` 注释里预留 PostgreSQL | `requirements.txt:25` |
| 建表方式 | `Base.metadata.create_all()` + **手写 SQLite `ALTER TABLE` 补列**；**无 Alembic** | `database.py:65-82` |
| AI | `openai` SDK 2.2.0 指向 DeepSeek（`https://api.deepseek.com/v1`），模型固定 `deepseek-chat` | `utils/ai_helpers.py:8`、`routers/interview.py` |
| 鉴权 | JWT（`python-jose` 3.4.0，HS256，有效期 30 分钟）+ OAuth2PasswordBearer | `auth.py:19,109-117`、`config.py:13` |
| 密码 | bcrypt 5.0.0；兼容历史 SHA-256 哈希并在登录时自动升级 | `auth.py:82-97` |
| 验证码 | `captcha` 0.7.1 + Pillow，4 位数字，内存字典存储 | `auth.py:32-60` |
| 简历解析 | `PyPDF2`（PDF）+ `python-docx`（DOCX） | `routers/interview.py:7-8,289-311` |
| 前端 | React 19.2.6 + TypeScript ~6.0.2 + Vite 8.0.12（rolldown 内核）+ TailwindCSS 3.4.19 | `frontend/package.json` |
| 前端路由 | **无路由库在实际使用**。`react-router-dom` 7.16.0 写在 `package.json` 里，但 `src/` 下**零引用**（全量 grep 无匹配）→ 死依赖 | `grep react-router src/` = 0 命中 |
| 前端状态 | 纯 `useState`/`useEffect`/`useRef`，**无 Redux/Zustand/Context**；`App.tsx` 单文件 2322 行 | `App.tsx` |
| 前后端联调 | **无 Vite proxy**。前端直连 `http://127.0.0.1:8000`（可用 `VITE_API_BASE_URL` 覆盖） | `src/config.ts:1`、`vite.config.ts` 仅含 react 插件 |
| 其他前端库 | **无 axios**（用原生 fetch 封装）、**无 UI 组件库**、**无图表库**（统计柱状图是手写 div 高度） | `src/services/api.ts`、`App.tsx:206` |

**部署（依据仓库自带文档，非当前代码可验证）**：阿里云 ECS + Nginx 反代 + `nohup uvicorn` 直跑，无 Docker、无 systemd。仓库内**不存在** `docker-compose.yml`（`.gitignore` 预留了 `docker-compose.override.yml`，属未落地规划）。

---

## 2. 角色与权限模型

只有两个角色，硬编码字符串：`"user"` / `"admin"`（`database.py:25`）。

后端三个鉴权依赖（`auth.py:128-152`）：

| 依赖 | 行为 |
|---|---|
| `get_current_user` | 解 JWT，从 `sub`（用户名）查库；失败 401 |
| `get_current_admin_user` | 非 admin → **403** |
| `require_user` | **admin → 403**（注释：管理员不能进行面试） |

**注意一个反直觉设计**：管理员被**显式禁止**参加面试，也被禁止访问 `/api/history`、`/api/notifications` 等接口。管理员的面试经验数据在库里也不存在。前端同样配合：admin 登录后 `history`/`notifications`/`unreadCount` 被清空（`App.tsx:1270-1273`），且 admin 登录会**自动打开管理后台**（`App.tsx:1282-1286`）。

**管理员账号**：`main.py:43-58` 在每次启动时检查并创建 `admin` / `admin123`（`is_active=True`，邮箱 `admin@system.local`）。当前库中已存在。

---

## 3. 功能清单

### 3.1 游客（未登录）

| 功能 | 说明 | 接口 |
|---|---|---|
| 浏览题库 | 顶栏"题库"按钮打开弹窗，按岗位分类查看题目、难度、标签 | `GET /api/question_bank`（**公开**） |
| 获取验证码 | 4 位数字图形验证码，可"换一张" | `GET /api/captcha`（**公开**） |
| 注册 / 登录 | 顶栏"登录 / 注册"弹窗，两种模式切换 | `POST /api/register`、`POST /api/login`（**公开**） |
| 查看首页 | 未登录时面试区展示占位提示，输入框禁用（"请先登录"） | — |

未登录时**不能**上传简历、开始面试（前端 `sendMessage`/`handleResumeFile` 均要求 `token`）。

### 3.2 普通用户（role = `user`）

**A. 简历与面试主流程**

| 功能 | 关键细节 | 接口 |
|---|---|---|
| 上传简历 | 支持 PDF/DOCX，≤5MB，**支持拖拽上传**；返回 500 字预览 + 全文本 + 预生成问题 | `POST /api/resume/upload`（**无鉴权！见 §10.1**） |
| 开始面试 | 提交 role / 简历全文 / 问题列表，后端建**内存会话**并返回开场白 | `POST /api/start_interview` |
| 多轮答题 | 逐轮对话；后端按会话内问题列表推进，并对上一答给出一句反馈 | `POST /api/chat` |
| 跳过题目 | 显式"跳过此题"按钮；**另**在输入框打"不会/换个/下一题"等 22 个关键词也会被识别为跳过 | `POST /api/skip_question` + `/api/chat` 内关键词判定 |
| 倒计时 | 前端 15 分钟倒计时，归零自动结束（**但超时路径可能导致报告生成失败，见 §10.4**） | 纯前端 `setInterval` |
| 进度显示 | 显示"第 N/总 题"，题目状态 pending/answered/skipped | 前端 state |
| 生成报告 | 结束后 AI 评分，返回三维分 + 总分 + 建议 + 总结 | `POST /api/generate_report` |
| 自动落库 | 报告生成成功后自动保存一条 `pending` 记录 | `POST /api/save_interview` |
| 语音输入 | `webkitSpeechRecognition`，`lang=zh-CN`；**转写会填入输入框，但"自动发送"实际失效（见 §10.4）** | 浏览器 API |
| 语音播报 | `SpeechSynthesis`，优先挑中文女声，可开关（默认关） | 浏览器 API |
| 导出报告 | 报告页「导出 TXT」按钮，前端拼字符串 + `Blob` 直接下载 | **纯前端，无后端参与** |

**B. 消息中心**（顶栏"消息"，含未读红点角标，每 30 秒轮询未读数）

| 功能 | 接口 |
|---|---|
| 通知列表（5 类图标：✅通过 / ❌未通过 / ⏳待审核 / 💬评语 / 🔔系统） | `GET /api/notifications` |
| 未读数 | `GET /api/notifications/unread_count` |
| 点击通知 → 标记已读 + **跳转并高亮对应面试记录** | `PATCH /api/notifications/{id}/read` + `GET /api/history_item/{id}` |
| 全部已读 | `PATCH /api/notifications/read_all` |
| 删除单条 / 清空全部 | `DELETE /api/notifications/{id}`、`DELETE /api/notifications/clear_all` |
| 面试历史列表 | `GET /api/history` |
| 历史记录内联展开看报告明细 + **管理员评语** | 同上（数据已含） |

**C. 个人中心**

| 功能 | 关键细节 | 接口 |
|---|---|---|
| 查看/编辑资料 | 昵称、简介、性别（male/female/other）、生日；**邮箱只读** | `GET`/`PATCH /api/user/profile` |
| 头像上传 | 选图后弹裁剪层：**缩放滑块（0.5–2x）+ canvas 裁成 200×200 JPEG**（quality 0.85）再上传；前端限 ≤2MB | `POST /api/user/avatar` |
| 个人统计 | 面试总次数、平均分 | `GET /api/user/stats` |
| 修改密码 | 需原密码；成功后**强制登出**要求重新登录 | `POST /api/change_password` |
| 登出 | **纯前端**：清 localStorage，无后端接口，JWT 无法吊销 | — |
| 登录态校验 | 挂载时拿 localStorage 的 token 探一次 `/api/user/profile`，401 则登出 | `GET /api/user/profile` |

### 3.3 管理员（role = `admin`）

独立管理后台组件 `AdminPanelContent`（`App.tsx:5`），三个标签页：

**A. 📊 仪表盘**（`GET /api/admin/stats`）
- 总用户数（**仅统计 `is_active=True`**）、面试总次数、平均总分、通过率
- 近 7 天每日面试量柱状图（**手写 div 高度实现，无图表库**）

**B. 用户管理**（`GET /api/admin/users`）

| 功能 | 限制 | 接口 |
|---|---|---|
| 用户列表 | 展示 id/用户名/角色/邮箱/状态/注册时间 | `GET /api/admin/users` |
| 重置密码 | 新密码 ≥8 位 | `POST /api/admin/users/{id}/reset_password` |
| 启用 / 禁用 | **不能禁用管理员** | `PATCH /api/admin/users/{id}/toggle_active` |
| 删除用户 | **不能删除管理员**；级联删除该用户的面试记录与通知 | `DELETE /api/admin/users/{id}` |

> 注意：`GET /api/admin/users` 返回了 `email` 字段，但**前端未做角色/邮箱/状态筛选**——尽管仓库自带 v1.0 报告声称有筛选功能（见 §9）。

**C. 面试记录管理**

| 功能 | 说明 | 接口 |
|---|---|---|
| 记录列表 | 含用户名、岗位、状态、评语、报告、时间 | `GET /api/admin/interviews` |
| 修改状态 | 下拉切换 `pending`/`approved`/`rejected` | `PATCH /api/admin/interviews/{id}` |
| 填写管理员评语 | 内联文本框 | 同上 |
| 查看详情弹窗 | 三维分 + 总分（按分色）+ 总结 + 建议 | 同上（数据已含） |
| 删除记录 | — | `DELETE /api/admin/interviews/{id}` |

**D. 站内通知自动触发**（`admin.py:96-106`）
- 状态**实际发生变化**时 → 按新状态发 `interview_approved` / `interview_rejected` / `interview_pending`
- 评语**内容发生变化**时 → 发 `new_comment`
- **5 分钟去重窗口**：同一 (user, type, 未读) 在 5 分钟内只保留一条，命中则更新内容与时间戳而非新增（`admin.py:39-50`）

---

## 4. 核心业务流程：面试闭环

```
上传简历 (PDF/DOCX ≤5MB)
   └─ 后端解析 → 清洗文本 → 生成 7 个问题 → 返回预览+全文+问题
开始面试
   └─ 后端建内存会话 interview_sessions[user_id]：questions[] / current_index / question_status[] / user_answers[]
逐轮对话
   ├─ 答一题 → 后端取上一题评分反馈(DeepSeek, 25s 超时) → 推 current_index → 返回「反馈 + 下一题」
   ├─ 跳过   → 标记 skipped → 返回「下一位」
   └─ 直到 current_index >= len(questions) → finished=True
      （或前端 15 分钟倒计时归零强制结束）
生成报告
   └─ DeepSeek 按「严格评分标准」提示词打分 → 强制 JSON 输出 → 解析
      → 返回 {expression_score, technical_score, logic_score, overall_score,
              answered_count, total_questions, suggestion, details}
      → 会话从内存中删除
自动落库
   └─ status='pending'，messages 与 report 以 JSON 字符串存 TEXT 列
管理员审核
   └─ 改状态 / 写评语 → 触发站内通知
用户收到通知 → 点击 → 高亮对应记录 → 查看评语
```

**关键限制（写死在提示词里）**：AI 面试官"最多只能问 10 个问题"（`interview.py:100`），但简历预生成问题固定 7 个（`ai_helpers.py:46-54`）。

---

## 5. 数据模型（实测，3 张表）

**`users`**

| 字段 | 类型 | 约束 / 备注 |
|---|---|---|
| id | INTEGER | PK |
| username | VARCHAR | UNIQUE, INDEX |
| hashed_password | VARCHAR | bcrypt（兼容历史 SHA-256） |
| created_at | DATETIME | 默认 `utcnow` |
| role | VARCHAR | 默认 `"user"` |
| email | VARCHAR | **UNIQUE, NOT NULL**（注册强制） |
| is_active | BOOLEAN | 默认 True（管理员可切换） |
| nickname / avatar / bio | VARCHAR / VARCHAR / TEXT | 可空 |
| gender | VARCHAR | 可空，限 male/female/other |
| birthday | DATE | 可空 |

**`interview_records`**

| 字段 | 类型 | 备注 |
|---|---|---|
| id | INTEGER | PK |
| user_id | INTEGER | FK → users.id |
| role | VARCHAR | 面试岗位 |
| messages | TEXT | **JSON 字符串**（非 JSON 列） |
| report | TEXT | **JSON 字符串** |
| created_at | DATETIME | |
| status | VARCHAR | 默认 `pending`；pending/approved/rejected |
| admin_comment | TEXT | 可空 |

**`notifications`**

| 字段 | 类型 | 备注 |
|---|---|---|
| id | INTEGER | PK |
| user_id | INTEGER | FK → users.id |
| type | VARCHAR | 默认 `system`；另有 interview_approved / interview_rejected / interview_pending / new_comment |
| message | TEXT | 由模板格式化 `{role}` |
| target_type | VARCHAR | 目前仅 `interview_record` |
| target_id | INTEGER | → interview_records.id |
| is_read | BOOLEAN | 默认 False |
| created_at | DATETIME | |

**当前库内实际数据**：2 个用户（`admin`、`123`）、3 条面试记录（均 `pending`、均属 user 2）、0 条通知。`backend/uploads/avatars/` 下有 7 个历史头像文件。

---

## 6. 后端接口全清单（实测）

共 **33 个** 路由条目 = **32 个 API** + 1 个静态目录挂载。

**公开（无鉴权）— 7 个 API + 1 个静态挂载**
| 方法 | 路径 | 备注 |
|---|---|---|
| GET | `/` | 健康检查，返回 Hello World |
| GET | `/api/captcha` | 返回 PNG，验证码 id 走响应头 `X-Captcha-Id` |
| GET | `/api/question_bank` | 题库 JSON |
| POST | `/api/register` | form-urlencoded |
| POST | `/api/login` | form-urlencoded |
| POST | `/api/chat` | ⚠️ **无鉴权**，见 §10.1 |
| POST | `/api/resume/upload` | ⚠️ **无鉴权**，见 §10.1 |
| GET | `/uploads/*` | StaticFiles 静态目录（头像） |

**需登录（`get_current_user`）— 5 个**
`GET /api/user/profile`、`PATCH /api/user/profile`、`POST /api/user/avatar`、`GET /api/user/stats`、`POST /api/change_password`

**仅普通用户（`require_user`，admin 得 403）— 12 个**
`POST /api/start_interview`、`POST /api/skip_question`、`POST /api/generate_report`、`POST /api/save_interview`、
`GET /api/history`、`GET /api/history_item/{id}`、
`GET /api/notifications`、`GET /api/notifications/unread_count`、`PATCH /api/notifications/{id}/read`、`PATCH /api/notifications/read_all`、`DELETE /api/notifications/{id}`、`DELETE /api/notifications/clear_all`

**仅管理员（`get_current_admin_user`）— 8 个**
`GET /api/admin/users`、`GET /api/admin/interviews`、`PATCH /api/admin/interviews/{id}`、`DELETE /api/admin/interviews/{id}`、
`POST /api/admin/users/{id}/reset_password`、`PATCH /api/admin/users/{id}/toggle_active`、`DELETE /api/admin/users/{id}`、`GET /api/admin/stats`

---

## 7. AI 集成方式（3 处调用，均同步阻塞）

| 场景 | 位置 | 模型/参数 | 失败行为 |
|---|---|---|---|
| 生成简历问题 | `ai_helpers.py:19` | `deepseek-chat`, temp 0.7, timeout 30s | **静默回退**到 7 条写死的通用问题 |
| 答题反馈 + 追问 | `interview.py:54` | temp 0.7, timeout 25s | 反馈置空，**仍继续推进**到下一题 |
| 面试评分报告 | `interview.py:236` | temp 0.3，无 timeout | **返回全 0 分**并把异常文本塞进 `suggestion` |
| 无会话的角色扮演对话 | `interview.py:111` | temp 0.7，无 timeout | 返回错误提示字符串当 `reply`（余额不足会提示充值） |

**已实现但有条件的动态问题生成**：`generate_questions()` 仅在环境变量 `RESUME_QUESTION_MODE=ai` 时才真正调用 AI，否则**永远走硬编码回退列表**。当前 `.env` **未设置**该变量 → **实际运行时简历问题与简历内容无关**，是 7 条固定问题。

**JSON 解析**：`extract_json_from_response()` 手工剥 ```` ```json ```` 围栏后 `json.loads`，无 schema 校验、无重试。

---

## 8. 题库（独立数据资产）

`backend/question_bank.json`：**10 个岗位分类、共 74 道题**，每题含 `text` / `difficulty`（低/中/高）/ `tags[]`。

分类：后端开发(10)、前端开发(10)、算法工程师(9)、全栈开发(7)、移动开发(7)、测试开发(7)、运维开发(7)、数据工程(6)、机器学习工程师(6)、嵌入式开发(5)。

前后端各有一份难度配色映射，且**前端 `App.tsx` 与 `QuestionBank.tsx` 用的难度字面量不一致**（一处认"简单/中等/困难"，一处认"低/中/高"）——题库实际数据是**低/中/高**，而 `QuestionBank.tsx`（`src/QuestionBank.tsx`）**未被任何地方引用**，属孤立死文件。

---

## 9. 与仓库自带文档的差异（重要）

仓库根目录有两份 `docx`：`AI智能面试模拟系统_项目总结报告_v1.0.docx`（2026-06，作者 longlong3585-sys，含线上地址 `http://112.124.54.223`）与 `维护.docx`。**它们是旧版快照，与当前代码已明显脱节。以下以代码为准**：

| 主题 | v1.0 文档声称 | 代码实际 |
|---|---|---|
| AI 是否回答候选人反问 | **"只能提问，不能回答候选人的问题"** | **相反**：显式检测"反问"并指示 AI *必须优先直接回应*（`interview.py:85-93`） |
| 最大问题数 | 7 个 | 提示词写 **10 个**（`interview.py:100`） |
| 评分失败降级 | 返回默认 **7 分** | 返回**全 0 分** + 异常文本（`interview.py:249-258`） |
| notifications 表 | 仅 user_id/message/is_read | 多了 `type` / `target_type` / `target_id` |
| 题库功能 | **完全未提及** | 已实现（10 分类 74 题） |
| 个人中心 / 头像 / 改密 | **完全未提及** | 已实现 |
| 管理端用户筛选 | 声称"支持筛选角色、邮箱、状态" | **未实现**，仅纯列表 |
| 通知去重 | 未提及 | 有 5 分钟去重窗口 |
| `link_url` 字段 | 未提及 | `database.py:75-76` 给 notifications **迁移添加了 `link_url` 列，但模型里没有该字段** → 死列 |
| 前端 API 地址 | "已改为相对路径，无硬编码" | 实际是 **`http://127.0.0.1:8000` 绝对地址**（`src/config.ts:1`），靠 CORS 放行 |

**结论**：文档不可作为需求依据，只能作为"历史意图参考"。这也正是本阶段要做逆向盘点的原因。

---

## 10. 逆向发现的缺口与风险（待后续阶段处置）

### 10.1 高危：两个接口完全没有鉴权
- `POST /api/chat`（`interview.py:33`）签名是 `async def chat(req: ChatRequest):`，**无任何 `Depends`**。
  → 任何人可白嫖 DeepSeek 调用（直接烧 API 余额）；
  → 更严重：`user_id` 由**客户端请求体提供**，调用方可传入他人 id，**读写/推进他人的内存面试会话**。
- `POST /api/resume/upload`（`interview.py:279`）同样无 `Depends`。
  → 未登录即可上传文件触发 PDF/DOCX 解析（资源消耗攻击面）。
- 已实测确认：`POST /api/chat` 无 token 时返回 **422（请求体校验失败）而非 401**，证明其路由未挂鉴权依赖。

### 10.2 中危
- **注册密码强度仅前端校验**。后端 `register` 只判空（`auth_router.py:44-45`），直接构造请求可绕过（前端规则：8-16 位 + 至少 2 类字符 + 无 6 位连续/重复）。
- **改密前后端规则不一致**：前端允许 **≥6** 位（`App.tsx:1173`），后端要求 **≥8** 位（`user.py:100`）→ 用户输入 6-7 位时前端放行、后端报错。
- **验证码与面试会话全在进程内存**（`auth.py:21-22`、`interview.py:19`）→ 重启即丢；**多 worker / 多进程部署会失效**；`interview_sessions` 无 TTL、无上限，正常结束才删除，中断则**永久泄漏**。
- **管理员初始密码 `admin123` 硬编码在源码中**，且强度弱于系统自身规则。
- **CORS 仅放行 `localhost:5173/5174`**（`main.py:20`）→ 生产同源部署没问题，但换端口/换域名开发会直接被拦。

### 10.3 低危 / 代码质量
- `@app.exception_handler(Exception)` 把**所有**未捕获异常统一成 500「服务器内部错误」，**丢掉堆栈**，排障困难（`main.py:35-40`）。
- `GET /api/history` 内 `json.loads(r.report)` **无 try/except**（`user.py:110`）→ 若存在 `report` 为 NULL 的记录会 500。
- `ChatRequest.action` 字段前端会传 `'start'`，**后端从未读取** → 死参数（`schemas.py:12` vs `interview.py`）。
- 前后端各自维护一份**完全相同的跳过关键词列表**（22 个词），双份硬编码，易漂移。
- **死代码清单**（均已 grep 确认）：`react-router-dom` 装了不用；`src/QuestionBank.tsx` 全项目零引用；`services/api.ts` 导出的 `apiGet/apiPost/apiPatch/apiDelete/publicFetch` 零调用（**只有 `authFetch` 被使用**）；`historyListRef`（`:480`）挂到 `<ul>` 后从未被读取（滚动改用了 `getElementById`）。
- 注册弹窗《用户协议》是 `href="#"` **死链接**。
- 统计柱状图为手写 div；`App.tsx` 2322 行单文件承载全部视图。
- **项目无任何自动化测试**；`.gitignore:45` 甚至忽略 `**/test_*.py`。质量保障依赖手工 curl/Postman。

### 10.4 前端：契约错配与结构性隐患

- **⚠️ `user_id` 不持久化 → 刷新页面后面试进度彻底失灵（影响可用性最大的一条）**
  `_userId` 只存在内存 state（`App.tsx:472`），**仅登录响应时赋值一次**（`:945`），刷新后即为 `null`；但 `sendMessage` 每次都把它塞进 `/api/chat` 请求体（`:602`）。而后端 `/api/chat` 恰恰**用请求体里的 `user_id` 去取内存会话**（`interview.py:39-40`），`/api/start_interview` 却是用 `current_user.id` 建的会话（`interview.py:132,150`）。
  → **一旦刷新页面**，`/api/chat` 走入无会话的"角色扮演"分支，不再返回 `current_index`/`finished`，前端进度圆点、跳过状态、自动结束判定**全部失效**。
- **⚠️ 自动结束面试可能生成不出报告（闭包过期）**
  - 倒计时 15 分钟的 `endInterview()` 取自"点击开始面试"那次渲染（`:781-791`），当时 `messages` 为 `[]`（该按钮仅在 `messages.length===0` 时渲染）→ 超时结束会命中 `alert('还没有任何对话，无法生成报告')` 分支（`:814-817`）而**不出报告**。
  - `sendMessage` 内 `setTimeout(() => endInterview(), 1500)`（`:637`）同样捕获旧 `messages`，会**漏掉最后一问一答**。
  - 手动点「结束」按钮（`:2100`）用当前闭包，**行为正确**。
- **进度下标 0 基 / 1 基混用**：`sendMessage` 用 `cp[currentQuestionIndex]`（0 基，`:623,629`），`skipQuestion` 用 `cp[data.current_index - 1]`（1 基，`:670`）。两者必有一处错位（后端 `current_index` 语义是"下一个待答索引"）。
- **`AdminPanelContent` 条件 Hook**：`if (!token) return`（`:6`）出现在 `useState`（`:8`）**之前**，违反 Hooks 规则。当前因父组件用 `showAdminPanel && userRole === 'admin'` 双重门控（`:1668`）而不会触发，属**潜在崩溃**。
- **语音输入"自动发送"实际不会发送**：识别回调在空依赖 `useEffect`（`:535-553`）中注册一次，捕获的是**首渲染的 `sendMessage`**，其 `input` 恒为 `''`、`hasResume` 恒为 `false`，会被首行守卫直接 `return`（`:586`）。转写文字会进输入框，但不会自动发出。
- **保存记录失败对用户完全不可见**：`/api/save_interview` 失败只写 `console.error`（`:850`），用户看到完整报告，却不知历史里没有这条记录。
- **存在死分支**：前端专门处理 `history_item` 的 **403**（`:1117-1121`），但后端查询同时限定 `id` + `user_id`，越权只返回 **404** → 403 分支永不可达。
- **无分页**：用户列表、面试记录、通知、历史全部一次性渲染。
- **无统一错误边界**：无 `ErrorBoundary`，任一 `map` 抛错即白屏。`q.tags.map`（`:2153`）**无可选链**，后端某题缺 `tags` 会直接崩。
- **无无障碍支持**：模态框无 Esc 关闭、无 `aria-*`、无焦点管理；`alert`/`confirm`/`prompt` 大量阻断式交互混用。
- **管理端"近 7 天趋势"未零填充**：后端只按"有记录的日期"分组（`admin.py:195-201`），前端直接遍历（`:206-211`）→ 只显示有面试的那几天，与"7 天"预期不符。
- **state 原地修改**：管理端评语/状态下拉浅拷贝数组后直接改元素属性（`:330-335`、`:350-354`），违反 React 不可变约定（目前靠每次换新数组引用而侥幸可重渲染）。
- **`_passwordError` 是死 state**：`:494` 声明、`:2206-2210` 被赋值，但 JSX 中**从不渲染**（只渲染 `authError`，`:2289`）→ 这段密码规则文案走不到界面。
- **前端全部 32 个接口调用均能在后端找到对应路由，路径与参数名一致，无 404 风险**（已逐个核对）。
- **全项目零 `TODO`/`FIXME`/`XXX`/`HACK`/`@ts-ignore`/`eslint-disable`，也没有被注释掉的代码块**（grep 确认）——说明代码是"一次性写完"的，没有留下待办线索。

---

## 11. 进入阶段 1 前需要你确认的问题

这些会**实质影响需求规格的写法**，请逐条给方向（也可直接说"按现状"）：

1. **本项目这一轮的定位是什么？** —— 是「A. 在现有代码上继续迭代/补全」，还是「B. 把它当作参考、按标准流程重做一遍」？（决定后续所有阶段的基线）
2. **人工审核环节是否保留为核心？** 若不保留，本系统的性质会从"模拟招聘流水线"变成"自助练习工具"，`status`/`admin_comment`/通知体系都要重估。
3. **§10.1 的两个无鉴权接口是否列为最高优先级修复项？** （我建议列入，且这属于"安全"而非"功能"范畴）
4. **§9 列出的文档与代码冲突，以代码为准是否确认？** 尤其"AI 是否回答候选人反问"这一条，是产品体验的关键分歧点。
5. **简历问题生成当前实际是写死的**（`RESUME_QUESTION_MODE` 未开启）。这是"被遗漏的开关"还是"有意的成本控制"？
6. **多进程 / 容器化部署是否在本轮范围内？** 若在，内存态验证码与会话必须重构（Redis 或 DB）。
7. **§10.4 的"刷新后进度失灵"与"超时结束不出报告"是否列为必修项？** 这两条直接影响主流程能否走通，我认为应当列为高优先级。
8. **前端要不要借这轮机会拆分？** `App.tsx` 单文件 2322 行、50+ 个 useState、无路由、无状态库。若本轮要持续迭代，建议先做工程化改造；若只是小修小补，可维持现状。

---

## 12. 附：盘点方法与可复现证据

- **静态通读**：`backend/` 全部 `.py`（含 `main.py`、`auth.py`、`database.py`、`config.py`、`routers/*`、`models/schemas.py`、`utils/ai_helpers.py`）、`frontend/src/{App.tsx, QuestionBank.tsx, config.ts, main.tsx, services/api.ts}`、`tailwind.config.js`、`vite.config.ts`、`index.html`、`index.css`、`tsconfig*`、`.env.example`、`.gitignore`、`README.md`。**`App.tsx` 2322 行全文逐段通读**，并把前端 32 处接口调用与后端路由逐个对照。
- **动态验证**：实际启动后端（`uvicorn`，:8000）与前端（`vite`，:5173），确认：
  - `GET /` → 200；`GET /api/question_bank` → 200（6889 字节）；`GET /api/captcha` → 200 且带 `X-Captcha-Id`
  - `GET /api/history` 无 token → **401**（对照）
  - `POST /api/chat` 无 token → **422**（证明未挂鉴权依赖）
  - `DELETE /api/notifications/clear_all` 无 token → **401**（**反证**了"被 `{notification_id}` 抢先匹配"的猜想：FastAPI 的 `int` 路径转换器使其正确落位，此处**无 bug**）
- **数据库实测**：直接读 `interview.db` 确认表结构、列名与行数（users=2, interview_records=3, notifications=0）。
- **交叉验证**：解析两份 `.docx` 全文，与代码逐条比对得出 §9。

> **方法论声明**：§10 中所有"风险"结论均基于源码与实测；§11 中的判断（如"管理审核是核心"）属**我的推断**，非代码强制事实，需你确认。凡本文标注"未提及""看不到"者，均表示我在代码中未找到依据，不等于一定不存在。
