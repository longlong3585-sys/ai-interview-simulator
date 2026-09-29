# T-46 / T-47 人工验收：InterviewRoom + ReportView / ProfilePanel / NotificationCenter / QuestionBank 拆分

> 对应任务：`docs/03-tasks.md` 的 **T-46**（拆分 `InterviewRoom`）与 **T-47**
> （拆分 `ReportView` / `ProfilePanel` / `NotificationCenter` / `QuestionBank`）
> 上游：`docs/30-manual-verification.md`（T-44 路由化 + T-45 AdminPanel 拆分）
> 交付物（前端）：见下「一、拆了什么」的文件清单
> 交付物（契约/验收）：`frontend/tests/component-split-contract.test.mjs`、
> `backend/scripts/verify_t46_t47_manual.py`

## 1. 这次到底改了什么

T-45 结束时 `App.tsx` 还有 **2189 行**，一屏一屏地内联着面谈主流程、报告、个人中心、
消息中心、题库和登录注册。本轮把它们全部搬走，`App.tsx` 只剩 **227 行**装配。

| 拆分 | 文件 | 行数 | 顺带解决/保留的问题 |
|---|---|---|---|
| 超时闭环状态机 | `interview/useInterviewTimeout.ts` | 282 | 锁定态的**唯一**入口从 App 里搬出来；判别力自检（把修复前写法喂进去必须判负） |
| 语音识别/合成 | `interview/useSpeech.ts` | 93 | 顺手修掉一个真 bug：识别回调闭包引用**首次渲染**的 `sendMessage`，那个 `input` 是空串，第一行就 `return` —— 语音输入只会填进输入框、永远不会自动发送。现在走 `onTranscriptRef` |
| 问答两条写路径 | `interview/useInterviewChat.ts` | 239 | 保留 T-42 不变量：超时 409 要**撤回**乐观插入的用户气泡；`finished` 走 `endInterviewRef` |
| 组合根（状态+开始/结束/登出） | `interview/useInterviewSession.ts` | 383 | 拆出根因：共享状态放组合根、行为放子 hook，避免 hook 互相 import 对方的状态 |
| 面试室视图 | `interview/InterviewRoom.tsx` | 325 | 视图层 0 处请求；`data-testid="interview-locked"` 等 T-42 锚点原样保留 |
| 简历上传纯函数 | `interview/resumeUpload.ts` | 54 | 校验/上传与状态迁移分离（否则必然出现循环依赖） |
| 题库取数 | `interview/questionBank.ts` + `interview/QuestionBankModal.tsx` | 23 + 96 | 原先"打开题库 + 拉数据"在导航栏与落地页**各抄一份**，现在只剩一份 |
| 报告页 | `report/ReportView.tsx` | 130 | T-43 超时标注与其条件渲染原样保留 |
| 通知中心数据 | `notifications/useNotificationCenter.ts` | 259 | `unreadCount` 要给顶部红点、`openHistory()` 要给面试流程用，所以状态不能塞进 UI 组件 |
| 通知中心视图 | `notifications/NotificationCenter.tsx` | 145 | `h.report` 为 null 的容错（T-10）、`record-${id}` 锚点原样保留 |
| 个人中心 | `profile/ProfilePanel.tsx` | 333 | 资料/头像/改密状态**无其他消费者**，整块下沉；头像裁剪从内联 `onClick` 提成函数 |
| 登录/注册 | `auth/AuthModal.tsx` | 299 | 14 个表单 state 跟着弹窗搬走；`signIn()` 仍是 `AuthContext` 的唯一写路径 |

`App.tsx`：**2189 → 227 行**。全树最大文件 `interview/useInterviewSession.ts` 383 行。

### 拆分保真度：JSX 是"逐行搬运"，不是"重写"

拆分的最大风险是**顺手改了行为**。为此做了一次一次性核对：用
`git show HEAD:frontend/src/App.tsx` 取出被搬走的那段 JSX，与目标文件
**逐行比对（忽略缩进与空行）**，看原行是否还在、新增了多少行：

| 区块 | 原 JSX 行数 | 命中原行 | 新增行 | 相似度 |
|---|---|---|---|---|
| `InterviewRoom`（简历卡 + 控制面板 + 进度条 + 聊天区 + 输入区/锁定面板） | 293 | **292** | 25 | 0.957 |
| `ReportView`（报告卡 + 超时标注 + 导出 TXT） | 124 | 102 | 23 | 0.819 |
| `NotificationCenter`（通知页签 + 历史页签） | 122 | **120** | 19 | 0.920 |
| `QuestionBankModal` | 55 | **50** | 40 | 0.690 |

"新增行"都是组件外壳（import / `export function` / `const { … } = session` 解构 /
Props 类型），不是新的界面元素；`ReportView` 的 22 行"未命中"来自把内联的
`onClick={() => { …20 行… }}` 提成 `const exportTxt = () => { … }`（同一份代码换了位置）。
`ProfilePanel` / `AuthModal` 的比例低是**预期**的：它们的 state 与处理函数原本散在 App 里，
现在一起并入了组件文件。核对脚本是一次性的（依赖 `git show HEAD`），未入库。

### T-46 / T-47 的验收标准逐条对照（`docs/03-tasks.md` §阶段 4）

| 标准 | 落点 |
|---|---|
| 单文件 ≤400 行 | 契约测试「全树单文件都在 ≤400 行预算内」（含判别力自检）+ 验收脚本 C 段①逐文件核对（当前最大 383 行） |
| `App.tsx` 降为路由装配 | 契约测试「App.tsx 已降为路由/页面装配」（14 项"实现痕迹"必须为 0 + 6 个视图必须被装配）+ 验收脚本 C 段②（同样带判别力自检） |

## 2. 傻瓜验证（一条命令，不用起服务、不用写 curl、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t46_t47_manual.py
```

可选参数：`--skip-tsc`（跳过类型检查）、`--with-build`（额外尝试 `vite build`）。
退出码：0 = 通过；1 = 未通过；2 = 环境问题。

### 它由四段**互不依赖**的取证拼成

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 预检：`node` 用哪个可执行文件、13 个目标文件在不在、`node_modules` 有没有 | 缺什么说什么 |
| A | **前端契约测试**（`node --test --test-reporter=tap`）：本轮新增的拆分契约 + 既有全部契约 | `tests=62 pass=62 fail=0`，且**点名用例必须真的出现过** |
| B | **TypeScript 全量类型检查**（`tsc -b`） | exit 0 |
| C | **本脚本自己重扫源码**（**不 import 测试里的任何辅助函数**）：行数预算 / `App.tsx` 是否只是装配 / 单一所有者 / 接口归属 / 无反向依赖 / 旧不变量随代码搬家 / eslint 结构性规则，并带判别力自检 | 全 `[PASS]` |
| D | 构建探测（`vite build`，**默认跳过**） | 见 §4 沙箱边界 |

### 最近一次实跑结果

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t46_t47_manual.py
0. 预检        [PASS] node（v24.9.0）/ 13 个文件齐 / node_modules 在 / 6 个契约测试文件
A. 契约测试    [PASS] tests=62 pass=62 fail=0；点名用例 6/6 出现
B. 类型检查    [PASS] tsc -b 退出码 0
C. 独立复核    [PASS] ①~⑨ 全绿 + 3 组判别力自检
D. 构建探测    [SKIP] 默认不跑（沙箱 EPERM，见 §4）
汇总：[PASS] A / [PASS] B / [PASS] C / [SKIP] D
✅ T-46 / T-47 人工验收通过
退出码 0
```

关键证据行（真实输出，非示意）：

```
解析结果：tests=62 pass=62 fail=0
[PASS] ① 全树单文件都在 ≤400 行预算内（最大 interview/useInterviewSession.ts，383 行）
[PASS] ① 判别力自检：超预算的合成文件被判为超预算（说明上面的 PASS 不是恒真）
[PASS] ② App.tsx 227 行（T-46 前 2189 行 → 现在只剩装配）
[PASS] ② App.tsx 里没有残留任何一屏的实现（14 项实现痕迹全为 0）
[PASS] ② 判别力自检：拆分前的写法被判为「仍内联实现且没装配子视图」
[PASS] ④ 锁定态/倒计时/endInterview ref/sendMessage/skipQuestion 各自只有一个所有者
[PASS] ⑤ 10 个后端接口各自只有一个调用方（拆分没有产生重复请求）
[PASS] ⑥ 依赖方向单向：只有 main.tsx import App.tsx
[PASS] ⑦ 37 条 T-42 / T-43 / T-44 不变量在拆分后全部仍在
[PASS] ⑧ 面试室/报告页/消息中心的请求全部下沉到 hook（视图层 0 处 fetch）
[PASS] ⑨ 结构性 Hook 规则全为 0（react-hooks/refs、react-hooks/immutability、react-hooks/purity）
```

### 只跑前端那一半（不需要后端）

```powershell
cd frontend
npm run test:node                        # 62 项契约测试（零依赖、不联网、毫秒级）
node node_modules/typescript/bin/tsc -b  # 类型检查，实测 exit 0
```

T-44 / T-45 的验收脚本**仍然通过**（本轮没有破坏它的任何断言）：

```powershell
cd backend; .\venv\Scripts\python.exe scripts\verify_t44_t45_manual.py   # 退出码 0
```

## 3. 契约测试怎么断言"拆分达标"

`frontend/tests/component-split-contract.test.mjs`（新增，10 项）的核心不是黑盒行为，
而是**代码形状** —— 与 `route-guard` / `api-parity` / `dead-params` / `skip-words-parity` 同口径：

1. **全树行数预算**：遍历 `src/**/*.ts(x)`，任何文件 >400 行即失败；
2. **`App.tsx` 是否只是装配层**：14 条"实现痕迹"正则（`const sendMessage =`、
   `data-testid="interview-locked"` …）必须全部为 0，且 6 个视图组件必须被装配；
3. **单一所有者**：`interviewLocked` / `timeLeft` / `endInterviewRef` / `sendMessage` /
   `skipQuestion` 只允许在一个文件里被定义（防止拆出"两份真源"）；
4. **接口归属**：`/api/chat`、`/api/generate_report`、`/api/change_password` … 各自只允许
   出现在一个文件里（防止拆分顺手复制出重复请求）；
5. **无反向依赖**：除 `main.tsx` 外，任何模块 import `App.tsx` 即失败。

其中 2 条**判别力自检**：把拆分前的形状（`const sendMessage = …` + 内联
`data-testid="interview-locked"`；一个 401 行的合成文件）喂给同一个判定函数，
必须判为失败 —— 否则"永远通过"的断言等于没有断言。

另外，T-42 / T-43 的既有契约（`tests/interview-timeout.test.mjs`、`tests/route-guard-contract.test.mjs`、
`tests/skip-words-parity.test.mjs`）**口径从"扫 App.tsx"改成了"扫整棵 src 树"**：
拆分让"只盯一个文件"的护栏静默失效（本轮一次报出 9 条假警报，全是"代码搬走了、护栏没搬"）。
报告页那条标注断言改为**定点**扫 `src/report/ReportView.tsx`，因为用整树 `indexOf`
比较的是"哪个文件恰好排在前面"，属于假证据。

## 4. 已知边界（刻意如此，非遗漏）

* **不驱动真实浏览器**。"点开始面试真的渲染出面试室""超时后输入区真的从 DOM 里消失"
  是**源码契约**证明的（装配点、`data-testid`、条件渲染），不是截图。
  肉眼验收：`cd frontend; npm run dev` → 登录 → 上传简历 → 开始面试 →
  （把 `INTERVIEW_DURATION_SECONDS` 调小）等到归零 → 应看到红色锁定面板且**没有输入框** →
  点"生成报告（按超时口径评分）" → 报告页应出现 ⏰ 标注。
* **`vite build` 在受限沙箱里以 `spawn EPERM` 失败**：它要 spawn 一个 stdio 走管道的
  子进程来打包 `vite.config.ts`，而沙箱禁止命名管道。与 `docs/29` §3、`docs/30` §4
  记录的是同一个**沙箱边界**，不是本次改动的问题。
* **`npm run lint` 本来就是红的**，本轮之后全库 **43 errors / 0 warnings**
  （HEAD ≈ 41，其中 `App.tsx` 31）。差异集中在 `@typescript-eslint/no-explicit-any`
  （随代码搬家的状态标注），而**结构性 Hook 规则 `react-hooks/refs` /
  `immutability` / `purity` 全为 0**：`immutability` 2→0、`exhaustive-deps` 2→0
  （拆分时顺手删掉了 6 条多余的 disable 指令，eslint 报的 "Unused directive" 一并清零）。
  验收脚本把这三条结构性规则设为**门槛**，其余只报数字，不假装 lint 是绿的。
* **一个行为修正是有意为之**（`useSpeech.ts`）：语音识别自动提交以前是坏的（见 §1 表格）。
  这是拆分时发现的真实缺陷，修复方式是"跨渲染回调走 ref"，与 T-42 修 `endInterview` 的
  手法完全一致；因此没有单开任务号，但在此显式登记，避免被当成"拆分顺手改了行为"。
* **T-48 / T-49 的地盘没有碰**：
  * `historyListRef`（`useNotificationCenter.ts` 里声明、从未绑定 DOM）原样保留；
  * `src/QuestionBank.tsx`（从未被任何地方 import 的旧组件）一行未改；
  * `_passwordError`（`AuthModal.tsx` 里的死状态）原样保留；
  * 题库弹窗里 `q.tags.map(...)` **仍然没有可选链** —— 缺字段会崩，归 T-49（FR-9.2）。
  `component-split-contract.test.mjs` 里对这几处都写了注释，防止后人在 T-48 之前"顺手清掉"。
* **`alert` 仍然在用**（发送失败、头像上传、通知跳转、岗位切换…）。T-37 / T-38
  要用 Toast + ConfirmDialog 替换它们，本轮只是**搬家**，不做替换：否则一次改动同时
  换了行为和位置，出问题无法二分。
* **`GET /api/interview/config` 的加载时机**：从"App 的 token 副作用"变成
  `useInterviewSession` 自己的 `token` 副作用，触发条件等价（有 token 就拉），
  但**不再受 `userRole` 变化影响** —— 管理员会走 `/admin` 重定向，下拉配置对其无意义。
