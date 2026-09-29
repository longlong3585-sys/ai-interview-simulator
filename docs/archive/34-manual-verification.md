# T-36 人工验收：api 层收敛（401 统一登出 + `X-Refreshed-Token` 集中处理 + 裸 `fetch` 归零）

> 对应任务：`docs/03-tasks.md` 的 **T-36**（ADR-016 前置，依赖 T-03 / T-34）
> 上游：`docs/33-manual-verification.md`（T-34 相对基地址 + Vite 代理）
> 下游关联：**T-40**（`AuthContext` 收敛 token + userId）、**T-50**（JWT 滑动续期，服务端下发续期头）
> 交付物（前端）：`src/services/authResponse.ts`（新增）、`src/services/api.ts`（重写）、
> `src/auth/AuthBridge.tsx`（新增）、`src/main.tsx`、
> `src/auth/AuthModal.tsx`、`src/interview/questionBank.ts`、`src/profile/ProfilePanel.tsx`、
> `src/notifications/useNotificationCenter.ts`、`src/interview/useInterviewSession.ts`、
> `src/admin/tabs/{StatsDashboard,InterviewsTable,UsersTable}.tsx`
> 交付物（契约/验收）：`frontend/tests/api-convergence.test.mjs`（新增 16 项）、
> `frontend/tests/{api-parity,component-split-contract,dead-code-contract}.test.mjs`（适配新写法）、
> `backend/scripts/verify_t36_manual.py`（新增）

## 1. 这次到底改了什么

收敛前全库有 **18 处裸 `fetch`**，散在 6 个文件里；每个调用点各自拼
`${API_BASE_URL}`、各自塞 `Authorization: Bearer ${token}`、各自判断 `res.ok`：

| 文件 | 裸 fetch | 收敛前的问题 |
|---|---|---|
| `notifications/useNotificationCenter.ts` | 9 | 令牌从 `props` 闭包进来，续期后不生效；401 只 `console.error` |
| `profile/ProfilePanel.tsx` | 3 | 改密用 `res.ok` 判定，401 与"密码错误"混为一谈 |
| `auth/AuthModal.tsx` | 2 | 公开端点也走裸 fetch，响应头 `X-Captcha-Id` 自己读 |
| `interview/useInterviewSession.ts` | 2 | 挂载校验令牌时自己判 401 → 自己登出 |
| `interview/questionBank.ts` | 1 | 公开端点 |
| `services/api.ts` | 2 | 已经是统一层，但只做"注令牌 + 401 抛错" |

后果是**同一类缺陷要修 N 遍**：401 处处不同（有的登出、有的只打日志、有的
把 401 当空数据）；`X-Refreshed-Token`（T-50 的续期头）根本没有落点，无人读、无人存。

修复后 **除 `src/services/api.ts` 外，全库裸 `fetch` = 0 处**，且四件事各只有**一个实现点**：

| 关注点 | 唯一落点 |
|---|---|
| URL 解析（相对基地址 / 绝对地址放行） | `api.ts` 的 `resolveApiUrl()` |
| `Authorization` 注入（公开路径除外） | `api.ts` 的 `prepareRequest()` |
| **401 → 统一登出** | `api.ts` 的 `request()` 401 分支 → `notifyUnauthorized()` |
| **`X-Refreshed-Token` → 刷新令牌** | `api.ts` 的 `handleRefreshedToken()` ← `authResponse.ts` 的 `readRefreshedToken()` |

两个刻意的设计决定：

* **401 登出用"注册回调"而不是 import `AuthContext`**。
  `AuthContext` → `main.tsx` → `App` → hook → `api.ts` 会构成运行时环，
  所以 `api.ts` 只暴露 `registerUnauthorizedHandler()`，由新增的
  `<AuthBridge />`（挂在 `<AuthProvider>` 内、渲染 `null`）把 `signOut` 接进去。
  契约测试直接断言"`main.tsx` 必须渲染 `<AuthBridge />`"——**忘接线是一个响亮的失败**，
  而不是悄悄退化成"401 后界面仍装作已登录"。
* **续期逻辑写成可注入依赖的形态**（`handleRefreshedToken(response, storage, listeners)`）。
  这样契约测试能在 Node 里给一个假 `storage` **真跑一遍续期链路**，
  而不是只靠 grep 断言"看起来读过头了"。顺序也被断言：**先落存储、再通知订阅者**
  （反过来会出现"订阅者拿到新令牌、存储却还是旧的"）。

## 2. 傻瓜验证（一条命令，不用起服务、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t36_manual.py
```

可选参数：`--skip-tsc`。退出码：0 = 通过；1 = 未通过；2 = 环境问题。

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 预检：`node`、5 个目标文件、`node_modules` | 缺什么说什么 |
| A | **契约测试** `node --test`（TAP） | `tests=101 pass=101 fail=0`，5 个点名用例必须真出现过 |
| B | `tsc -b` | exit 0 |
| C | **本脚本自己重扫源码**（另一套语言复述同一条规则）：① 裸 fetch 计数 / ② 401 链路 / ③ 续期头链路 / ④ 迁移清单 | 全 `[PASS]`，含 4 组判别力自检 |

### 最近一次实跑结果（真实输出，中文在部分终端会显示为乱码，不影响判定）

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t36_manual.py
0. 预检        [PASS] node（v24.9.0）/ 5 个文件齐 / node_modules 在
A. 契约测试    [PASS] tests=101 pass=101 fail=0；点名用例 5/5 出现
B. 类型检查    [PASS] tsc -b 退出码 0
C. 独立复核    [PASS] ①~④ 全绿 + 4 组判别力自检
汇总：[PASS] A / [PASS] B / [PASS] C
✅ T-36 人工验收通过
退出码 0
```

关键证据行（真实输出，非示意）：

```
[PASS] ① src 下 33 个 .ts/.tsx 文件里，除统一层外**裸 fetch = 0 处**
[PASS] ① 统一层自己只有 2 处 fetch（request / publicFetch 两个出口，没有第三个漏网出口）
[PASS] ① 判别力自检：收敛前的写法被判为违规，而 `refetch(` / `.fetch(` / 注释里的 fetch 不被误判
[PASS] ① 全库 grep `${API_BASE_URL}` 模板拼接 = 0 处（验收标准原文的口径，含 admin 页签）
[PASS] ② 401 分支：先 notifyUnauthorized() 再抛 401（顺序正确，登出路径唯一）
[PASS] ② main.tsx 渲染了 <AuthBridge />（401 登出有接线入口）
[PASS] ② 统一层不 import AuthContext（用注册口注入，避免运行时环）
[PASS] ③ 续期链路顺序正确：读头 → 落 localStorage → 通知订阅者
[PASS] ③ 两个出口（request / publicFetch）都经过续期处理，没有「漏一类请求不续期」
[PASS] ④ 5 个参与文件共 17 处调用点全部改走统一层（第 18 处是 services/api.ts 自身）
     [PASS] src/notifications/useNotificationCenter.ts  收敛前 9 处 → 现在 0 处，import 统一层=是
```

### 只跑前端那一半（不需要后端）

```powershell
cd frontend
npm run test:node                        # 101 项契约测试（零依赖、不联网、毫秒级）
node node_modules/typescript/bin/tsc -b  # 类型检查，实测 exit 0
```

自己想再确认一次"裸 fetch 归零"，两条等价命令（PowerShell，**不依赖 `rg`**）：

```powershell
cd frontend
Get-ChildItem -Recurse src -Include *.ts,*.tsx |
  Select-String -Pattern '(^|[^.\w])fetch\s*\(' | Select-Object Path,LineNumber
  # 期望：只命中 src\services\api.ts —— 真实调用在 request() 与 publicFetch() 里，
  #       另一处命中是**注释**里在讲解这条规则（验收脚本会去注释后再数，口径以脚本为准）

Get-ChildItem -Recurse src -Include *.ts,*.tsx |
  Select-String -Pattern '\$\{API_BASE_URL\}'
  # 期望：只命中 src\services\api.ts（第 4 行注释、第 113 行 resolveApiUrl、第 169 行注释）
  #       —— 统一层是唯一允许拼基地址的地方；其它文件 0 命中
```

既有验收脚本**全部仍然通过**：

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t34_manual.py       # 退出码 0（T-34 未回退）
.\venv\Scripts\python.exe scripts\verify_t44_t45_manual.py   # 退出码 0
.\venv\Scripts\python.exe scripts\verify_t46_t49_manual.py   # 退出码 0
```

## 3. 契约测试怎么断言这三条不变量

`frontend/tests/api-convergence.test.mjs`（16 项）分三层，每条都带判别力自检：

1. **不变量 ③ 裸 fetch 归零**（源码口径）：
   扫 `src/**` 去注释后统计 `fetch(`，排除 `services/api.ts`，必须为空集；
   统一层自己必须**恰好 2 处**（多一处说明有第三个绕过 `request()` 的出口）。
   判别力自检：把收敛前的两行写法塞进同一个判定器必须被判为违规，
   而 `refetch(` / `obj.fetch(` / 注释里的 `fetch(` 不能被误判。
2. **不变量 ① 401 统一登出**：`isUnauthorizedError()` 不依赖跨模块 `instanceof`
   （打包后多份类定义会让 `instanceof` 失效）；401 错误的 `message` 仍是 `'Unauthorized'`
   （`useInterviewChat` / `resumeUpload` 里既有的字符串判定不能被改坏）。
   接线层断言 `main.tsx` 渲染 `<AuthBridge />` 且 `AuthBridge` 调用
   `registerUnauthorizedHandler(signOut)`；`api.ts` **不得** import `AuthContext`；
   `request()` 的 401 分支里 `notifyUnauthorized()` 必须**先于** `throw`，
   且全文件只允许出现一次该调用（登出入口唯一）。
3. **不变量 ② 续期头集中处理**：`REFRESHED_TOKEN_HEADER === 'X-Refreshed-Token'`、
   存储键 `'token'` 与 `AuthContext.AUTH_STORAGE_KEYS` 同源（否则续期写到别处，
   下次请求读的还是旧令牌）；`readRefreshedToken()` 对 `null` / 空白 / 缺失一律返回 `null`
   （绝不把 `null` 写进 localStorage 把用户登出）；`applyRefreshedToken()` 在存储抛错时
   返回 `false` 而不抛；再**真跑一遍**链路（假 storage + 假响应）断言副作用。

`api-parity.test.mjs` 的路径收集器也一起升级了：T-34 之前它认的是
`${API_BASE_URL}/api/x`，收敛后路径变成普通字面量 `'/api/x'`，
若不改就会**静默变成空集**——而空集会让"每个路径后端都存在"这条恒真（假通过）。
现在两种写法都收，并额外断言"新写法确实被收到"。
`component-split-contract.test.mjs` / `dead-code-contract.test.mjs` 的"归属判定"
从 `src.includes('/api/x')` 改成"认调用形态"，否则统一层里那张**公开端点路由表**
会被误判成"第二个调用方"。

## 4. 已知边界（刻意如此，非遗漏）

* **后端目前还没有下发 `X-Refreshed-Token`**：`grep -r X-Refreshed-Token backend/` = 0 处，
  那属于 **T-50**（ADR-016 的服务端侧）。本任务交付的是**前端前置**：
  集中读头 + 落 `localStorage` + 通知订阅者，并用契约测试真跑一遍该链路。
  也就是说"续期端到端生效"要等 T-50 配合，本任务不假装已经打通。
* **`token` 的内存态（`AuthContext.token`）不在本任务范围内**：统一层每请求**现读
  `localStorage`**，所以令牌刷新后下一个请求一定用新令牌；但当前页面里
  `AuthContext.token` 这个 React 状态要等 T-40 才会跟着同步。
  影响面被限定在 T-50 打开之后的窗口期（本任务不做 `auth_time` 绝对上限，
  后端也还没发续期头），因此这里**不越界**，只把口子（`onTokenRefreshed`）留好。
* **不驱动真实浏览器**。"401 后真的登出"是**源码链路**证明的：
  唯一出口 `request()` 先登出再抛错，回调由 `main.tsx` → `<AuthBridge />` 接线。
  肉眼验收（不用改代码，可选）：
  1. `cd frontend; npm run dev`（先起后端）→ 登录后**手动改掉** `localStorage.token`
     为任意乱串 → 点任意需要登录的按钮（例如刷新一次页面触发 `/api/user/profile` 校验）
     → 应**自动回到未登录状态**（而不是停在界面上反复报错）；
  2. 打开 DevTools → Network → 任一 `/api/*` 请求的 Request Headers 里应带
     `Authorization: Bearer …`，而 `/api/captcha`、`/api/login`、`/api/question_bank`
     **不带**该头。
* **`npm run lint` 仍然是红的**：实测 `40 errors / 0 warnings`（T-34 文档记的是 42，
  但那轮统计里混进了 2 条"未使用的 `eslint-disable` 指令"，本轮顺手清掉了；
  错误类型与文件分布完全没变，**没有新增结构性违规**：全是既有的
  `no-explicit-any`、`set-state-in-effect`、`only-export-components`）。
  本任务只核对"没有新增违规"，不假装 lint 是绿的。
* **`apiPatch` 不再无条件带 `Content-Type`**：以前无论有无 body 都写
  `application/json`，现在只在真有 body 时写。对 `PATCH /api/notifications/read_all`
  这类空 body 请求，少一个头不影响 FastAPI 路由（它们不解析请求体）。
* **`e2e-timeout-live.mjs` 不受影响**：它自己接 `--base-url`，与前端基地址解耦。
