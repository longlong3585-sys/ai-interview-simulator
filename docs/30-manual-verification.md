# T-44 / T-45 人工验收：路由化 + 路由层守卫 + 条件 Hook + AdminPanel 拆分

> 对应任务：`docs/03-tasks.md` 的 **T-44**（启用 `react-router-dom` + 守卫移路由层，顺带修 **FR-11.3 条件 Hook**）
> 与 **T-45**（拆分 `AdminPanel` 组件）
> 上游：`docs/28-manual-verification.md`（T-28 后端超时兜底）、`docs/29-manual-verification.md`（T-42/T-43 前端超时闭环）
> 交付物（前端）：`src/auth/AuthContext.tsx`、`src/auth/RequireAuth.tsx`、`src/main.tsx`、
> `src/admin/AdminPanel.tsx`、`src/admin/AdminPage.tsx`、`src/admin/ReportDetailModal.tsx`、
> `src/admin/tabs/{StatsDashboard,UsersTable,InterviewsTable}.tsx`、`src/App.tsx`（重接线）
> 交付物（契约/验收）：`frontend/tests/route-guard-contract.test.mjs`、
> `backend/scripts/verify_t44_t45_manual.py`

## 1. 这次到底改了什么

### T-44：把"页面"从布尔量变成路由

修复前：`react-router-dom` **装在 `package.json` 里，却从没有被 import 过**。
于是"页面"只能靠 `App.tsx` 内部一堆布尔量切换（`showAdminPanel` / `showProfile` / `report` …），
带来三个具体后果：

| # | 问题 | 修复前 | 现在 |
|---|---|---|---|
| ① | 进不去任何页面 | 没有 URL 概念：管理后台、个人中心都是弹窗；**刷新即回首页** | `/admin` 是真实路由；`*` 兜底回 `/` |
| ② | 守卫只能写成渲染期的 `&&` | `{showAdminPanel && userRole === 'admin' && (…)}` —— "能不能进"和"长什么样"揉在同一个 JSX 里 | `<RequireAdmin>` / `<RequireAuth>` 只发生在**路由出口**，被放行的子树拿到的 `token` 一定非空 |
| ③ | 认证状态三处散落 | `token` / `userRole` / `username` 是三个 `useState`，初值各自 `localStorage.getItem`，写入散在登录/登出回调里 | `AuthContext` 是**唯一真源**，每次变更同步落盘 —— "刷新后仍在"由唯一写路径保证，不再靠各调用点自觉 |

同时收掉 **FR-11.3 条件 Hook**（下面单列）。

### T-45：AdminPanel 从 2527 行的 App.tsx 里搬出来

`AdminPanelContent` 原本占 `App.tsx` 的 21~456 行（436 行），和面谈主流程挤在一个文件里。
拆完第一版后 `AdminPanel.tsx` 仍有 419 行，**越过 T-45~T-47 的「单文件 ≤400 行」预算** ——
于是又切了两刀，切的过程中顺手修掉一处真实缺陷：

| 拆分 | 文件 | 行数 | 顺带修掉的问题 |
|---|---|---|---|
| 面板本体搬出 | `admin/AdminPanel.tsx` | 87 | 只剩"守卫 + 页签壳" |
| 报告详情弹窗 | `admin/ReportDetailModal.tsx` | 98 | 弹窗不再参与表格的重渲染路径 |
| 仪表盘页签 | `admin/tabs/StatsDashboard.tsx` | 74 | —— |
| 用户页签 | `admin/tabs/UsersTable.tsx` | 161 | —— |
| 面试记录页签 | `admin/tabs/InterviewsTable.tsx` | 180 | **`loading` / `successMsg` 原本被三个页签共用**：切页签时上一个页签的"加载中…/✅ 更新成功"会串到下一个页签上；现在各页签自持 |

`App.tsx`：**2716 → 2269 行**。`main.tsx` 变成纯路由装配（约 50 行）。

### FR-11.3 条件 Hook：一行修复，一条崩溃路径

```tsx
// 修复前（原 App.tsx:21-24）
function AdminPanelContent({ token }: { token: string | null }) {
  if (!token) return <div className="p-4 text-center text-gray-500">请先登录</div>;   // ← 挡在 7 个 Hook 之前
  const [activeTab, setActiveTab] = useState<'stats'>(…);
  …
```

React 的契约是"同一组件两次渲染调用的 Hook 个数必须一致"。令牌一旦**由有变无**
（登出、401 被清），这次渲染只调用 0 个 Hook，React 直接抛
**"Rendered fewer hooks than expected"**。

现在：**Hook 全部无条件调用，早退只发生在 JSX 出口**：

```tsx
export default function AdminPanel({ token, onClose }: AdminPanelProps) {
  const [activeTab, setActiveTab] = useState<AdminTab>('stats');   // 所有 Hook 先跑完
  …
  if (!token) {
    return <div className="p-4 text-center text-gray-500">请先登录</div>;   // 出口在 Hook 之后
  }
```

代价是"令牌为空"时仍然执行了一个无害的 `useState`；换来的是**渲染树可以安全替换**。

### 验收标准逐条对照（`docs/03-tasks.md` §阶段 4）

| 标准 | 落点 |
|---|---|
| T-44：`AdminPanelContent` 不再在 `useState` 前 `return` | 契约测试「AdminPanel 的 Hook 全部早于任何早退」+ 验收脚本 C 段自扫 |
| T-44：令牌由有到无不抛错 | 同上（这是"不抛错"的**唯一**成因）；另有判别力自检，把修复前的写法喂进去必须判为失败 |
| T-45~T-47：单文件 ≤400 行 | 契约测试与验收脚本逐文件核对（当前最大 180 行） |
| T-45~T-47：`App.tsx` 降为路由装配 | 部分达成：`main.tsx` 已是路由装配；**面谈主流程/报告/个人中心/通知/题库仍在 App.tsx**，留给 T-46 / T-47（2269 行） |

## 2. 傻瓜验证（一条命令，不用起服务、不用写 curl、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t44_t45_manual.py
```

可选参数：`--skip-tsc`（跳过类型检查）、`--with-build`（额外尝试 `vite build`）。
退出码：0 = 通过；1 = 未通过；2 = 环境问题。

### 它由三段**互不依赖**的取证拼成

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 预检：`node` 用哪个可执行文件、11 个目标文件在不在、`node_modules` 有没有 | 缺什么说什么 |
| A | **前端契约测试**（`node --test --test-reporter=tap`）：本任务的 15 项 + 既有全部契约 | `tests=51 pass=51 fail=0`，且**点名用例必须真的出现过** |
| B | **TypeScript 全量类型检查**（`tsc -b`） | exit 0 |
| C | **本脚本自己重扫源码**（**不 import 测试里的任何辅助函数**，自己实现括号扫描/深度判定）：逐条核对路由接线、守卫位置、认证真源、Hook 顺序、拆分与行数预算 | 全 `[PASS]`，含判别力自检 |
| D | 构建探测（`vite build`，**默认跳过**） | 见 §4 沙箱边界 |

### 最近一次实跑结果

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t44_t45_manual.py
0. 预检        [PASS] node 可执行（v24.9.0）/ 11 个文件齐 / node_modules 在
A. 契约测试    [PASS] pass=51 fail=0；点名用例 6/6 出现
B. 类型检查    [PASS] tsc -b 退出码 0
C. 独立复核    [PASS] ①②③④⑤ 全绿 + 判别力自检通过
D. 构建探测    [SKIP] 默认不跑（沙箱 EPERM，见 §4）
汇总：[PASS] A / [PASS] B / [PASS] C / [SKIP] D
✅ T-44 / T-45 人工验收通过
退出码 0
```

关键证据行（真实输出，非示意）：

```
解析结果：tests=51 pass=51 fail=0
[PASS] 点名用例确实跑过：AdminPanel 的 Hook 全部早于任何早退
[PASS] 点名用例确实跑过：判别力：把修复前的写法（return 在 useState 之前）喂进去必须判为失败
[PASS] ① main.tsx 真的 import 并挂载了 react-router-dom（BrowserRouter + Routes）
[PASS] ② /admin 路由存在，且页面被 <RequireAdmin> 包裹（守卫在路由层）
[PASS] ② 守卫同时区分「未登录」与「已登录但非管理员」
[PASS] ③ App.tsx 已不再自己读写 localStorage 的 token/role/username
      扫描结果：AdminPanel 顶层 Hook 1 个；首个组件级早退位置 87
[PASS] ④ AdminPanel 的 1 个 Hook 全部早于早退出口（FR-11.3 已修）
[PASS] ④ 判别力自检：修复前的写法被判为「Hook 在早退之后」（说明上面的 PASS 不是恒真）
[PASS] ⑤ 管理后台全部请求都走 authFetch（裸 fetch 为 0）
[PASS] ⑤ 管理员 API 调用仍是 8 处（拆分没有丢请求）
[PASS] ⑤ App.tsx 2269 行（T-45 前是 2716 行）
[PASS] ⑤ AdminPanel.tsx 87 行（≤400 预算内）
```

### 只跑前端那一半（不需要后端）

```powershell
cd frontend
npm run test:node                       # 51 项契约测试（零依赖、不联网、毫秒级）
node node_modules/typescript/bin/tsc -b  # 类型检查，实测 exit 0
```

## 3. 契约测试怎么断言"条件 Hook 已修"

`frontend/tests/route-guard-contract.test.mjs`（15 项，`node:test`，零依赖）里，
最核心的一条**不是**黑盒行为断言（本机装不上 vitest / jsdom，`node_modules` 里没有），
而是一条**源码结构断言** —— 与 `api-parity` / `dead-params` / `skip-words-parity` 同口径：

1. 用括号扫描器（跳过字符串、模板串、注释）从 `AdminPanel({ token })` 里切出**函数体**
   —— 注意必须先跳过**参数表**，否则解构参数 `{ token }` 会被当成函数体（这个坑踩过一次）；
2. 给每个字符标注**花括号深度**：组件顶层 = 0，`if (…) { … }` 里 = 1，
   `onClick={async () => { … }}` / `useEffect(() => { return () => … })` 这类回调 = 2 以上；
3. 断言：**所有 Hook（深度 0）都早于第一个组件级早退（深度 ≤1 的 `return`）**。

为了让这条断言**不是恒真**，测试里还放了一条判别力自检：把修复前的写法
（`if (!token) return …;` 在 `useState` 之前）喂给同一个函数，必须判为失败。
验收脚本 C 段用**自己实现的**同口径判定再独立跑一遍 —— 两条链互不依赖。

## 4. 已知边界（刻意如此，非遗漏）

* **不驱动真实浏览器**。"点管理按钮真的跳到 `/admin`"是**源码契约**证明的
  （按钮 `onClick` 里是 `navigate('/admin')`，管理员账号在 `/` 会被 `<Navigate to="/admin" replace />` 接管），
  不是截图。要在浏览器里肉眼看：`cd frontend; npm run dev` → 管理员登录（自动落到 `/admin`）→
  手输一个不存在的路径（应被 `*` 兜底弹回 `/`）→ 点"返回面试页"。
* **`vite build` 在受限沙箱里以 `Error: spawn EPERM` 失败**：它要 spawn 一个 stdio 走管道的
  子进程来打包 `vite.config.ts`，而沙箱禁止命名管道。与 `docs/29` §3 记录的是同一个**沙箱边界**，
  不是本次改动的问题。验收脚本遇到这个错误会标成 `[ENV]` 而非 `[FAIL]`；`tsc -b` 不受影响。
* **T-44 的依赖 T-40 仍未单独成任务**：本轮把"认证状态集中 + 持久化"这部分**随 T-44 一起落地**了
  （`AuthContext`），否则"守卫移到路由层"没有可读的认证真源、`userId` 也无处安放。
  T-40 表格行仍标 ⬜，T-41（挂载时拉取会话并重建视图）不受影响 —— `App.tsx` 里原有的
  `validateToken` 挂载校验**保留未改**。
* **T-44 没有把面谈主流程搬进路由**：`/` 仍由 `<App />` 承载 `InterviewRoom`/报告/个人中心/通知/题库。
  `App.tsx` 2269 行，**远未降到"路由装配"**——那是 T-46 / T-47 的工作量，
  本次只把管理员这一条路径真正路由化（它正好是 T-45 的拆分对象）。
* **`npm run lint` 本来就是红的**（HEAD 上 `App.tsx` 已有 46 条）。本次没有新增
  `purity` / `set-state-in-effect` 违规；新拆出的页签里对"每次渲染都新建的 fetch 函数"
  显式加了 `eslint-disable-next-line react-hooks/exhaustive-deps` 并写明理由。
* **生产部署时 `/admin` 需要服务端 history 回退**：`react-router-dom` 用的是
  `BrowserRouter`（URL 无 `#`，体验更好），刷新 `/admin` 会真的向服务器请求这个路径。
  开发（`npm run dev`）与 `vite preview` 自带 history fallback，不受影响；
  但 `backend/main.py` 目前**只挂了 `/uploads` 静态目录、没有托管前端产物**
  （没有 `dist` 挂载，也就没有 history 回退）。将来若由后端托管前端，
  需要为 `index.html` 加一条回退路由 —— 登记给 T-54（部署/上线）一并处理。
* **验收脚本的 A 段依赖测试输出里的中文用例名**：因此固定用
  `--test-reporter=tap`（机器可读、纯 ASCII 汇总行）。默认的 `spec` 报告器会打 `ⓘ`（U+2139），
  **在 GBK 控制台上直接让 Python 抛 `UnicodeEncodeError`** —— 脚本里既改用了 TAP，
  又给 stdout 加了 `errors="replace"` 兜底。
