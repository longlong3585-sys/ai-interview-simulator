# T-42 / T-43 人工验收：前端超时强制闭环 + 超时报告标注

> 对应任务：`docs/03-tasks.md` 的 **T-42**（FR-4.12 / Bug 3A·3B）与 **T-43**（FR-4.5）
> 上游：`docs/28-manual-verification.md`（T-28 后端兜底 —— 本文只负责**前端**这一半）
> 交付物：`frontend/src/interview/timeout.ts`、`frontend/src/components/Toast.tsx`、
> `frontend/src/components/toastSeq.ts`、`frontend/tests/interview-timeout.test.mjs`、
> `frontend/tests/e2e-timeout-live.mjs`、`backend/scripts/verify_t42_manual.py`

## 1. 这次到底改了什么

T-28 把"超时时刻"搬到了服务端，但当时留下一条明确的边界（docs/28 §4）：
**"销毁输入区 / 遮罩 / Toast 文案"仍属 T-42/T-43**。修复前三处硬伤：

| # | 问题 | 修复前 | 现在 |
|---|---|---|---|
| ① | 时长来源 | 前端硬编码 `setTimeLeft(15 * 60)`；服务端把时长调小后两边各说各话 | 时长/死线全部来自服务端：`/api/interview/config` 的 `duration_seconds`，会话的 `deadline_at` / `interview_remaining_seconds` |
| ② | 倒计时方式 | `prev - 1` 逐秒自减 —— 标签页挂起、系统休眠、后台节流后比真实时间**慢** | 每个 tick 用 `Date.now()` **按死线重算**；回到前台（`visibilitychange`）再向服务端校正一次 |
| ③ | 归零后的界面 | 只调 `endInterview()`，**没有任何锁定状态** —— 输入区还在，还能继续答（服务端其实会 409） | `interviewLocked` 后**输入区整块从 DOM 移除**、写入口全部关闭、弹**不可自动消失**的 Toast；只留"生成报告（超时口径）/ 重新开始" |

另外三处收口：

* **Bug 3A（闭包）**：`setTimeout(() => endInterview(), 1500)` 捕获的是**本次渲染之前**的
  `messages` —— 最后一轮问答进不了报告入参（也不会出现在保存的历史里），
  `messages.length === 0` 的判断还可能用旧值命中（超时后弹"还没有任何对话"）。
  现在统一走 `endInterviewRef.current()`（最新回调）。
* **零作答的超时**：前端不再用"还没有任何对话"把用户挡在报告之外 ——
  服务端 T-27 会给一份带「未及作答，无法评分」的报告。
* **过期响应**：新增**世代号**（`interviewGenerationRef`）丢弃"回来得太晚"的会话同步响应。
  否则报告页上会冒出一条迟到的"本场面试已超时"Toast。

### ⚠️ 本次踩到并修掉的一个**只在 UTC+8 暴露**的时区陷阱

后端 `deadline_at` 是 `datetime.utcnow()` 派生的**无时区**串（`2026-09-29T17:36:23`）。
JS 的 `new Date('2026-09-29T17:36:23')` 会按**浏览器本地时区**解释它 ——
在东八区，这条死线被当成"8 小时以后"：**倒计时多出 8 小时，UI 永远不会锁**，
而且和 T-28 服务端的判定彻底脱节。

更阴的是：**跑在 UTC 的 CI 上这个 bug 完全看不见**（两种解释恰好相同）。
因此修法不是"记得加 Z"，而是把它做成**可断言的不变量**：

* `parseServerDeadline()`：无时区标记一律补 `Z` 按 UTC 解释（带 ±hh:mm / Z 的原样尊重）；
* 契约测试用**固定时钟**断言"死线前 60 秒的串 → 剩余正好 60 秒"，
  并在非 UTC 机器上额外断言"解析结果 ≠ 本地解释"；
* 端到端脚本用**真实服务端数据**交叉校验：`deadline_at` 与
  `interview_remaining_seconds` 必须互相印证（实测偏差 974 毫秒；若按本地解释会差 8 小时）。

### 判别力：409 不止一种，不能一律锁

`no_active_session` / `version_conflict` **同样是 409**。把它们也当成超时去锁 UI，
用户会被卡在一场其实还能继续的面试外面。因此前端只认
`code === 'interview_timeout' || ended_reason === 'timeout'`（`isTimeoutResponse()`），
并把三种 409 的反例写进了契约测试。

## 2. 傻瓜验证（一条命令，不用起服务、不用写 curl、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t42_manual.py
```

脚本自己做完所有事，**不需要你手工配合任何一步**：

1. 起一个**本机假 AI**（回环，永不联网），把收到的 prompt 原样记下来当证据；
2. 用 `OPENAI_BASE_URL` 指向它，另起一个 uvicorn，把面试时长压成 `--duration` 秒（默认 **12**），
   并把 `DATABASE_URL` **显式指向 `--db` 指定的库**（保证"脚本查的库"就是"服务端写的库"）；
3. 自己往库里造一个**临时用户**并**自己签令牌**（`create_access_token`）——
   所以不用登录、不用从浏览器里复制 Token；
4. 跑**前端契约测试**（`node --test`，毫秒级）；
5. 跑**端到端**（`frontend/tests/e2e-timeout-live.mjs`，实时转发它的每一行）——
   它**用的是前端自己那份逻辑**（`src/interview/timeout.ts`）做判定，
   而不是在脚本里重写一份"我以为前端会这么做"的实现；
6. 自己再**直查数据库**复核（与 Node 侧互相独立取证）；
7. 删掉临时用户及其会话/报告/通知，复查四张表无残留。

可选参数：`--duration 8`（越小越快，必须 > 3）、`--db <路径>`。
退出码：0 = 通过；1 = 未通过；2 = 环境问题。

### 它逐条证明什么

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 环境预检（node 用哪个可执行文件、三个文件在不在、库有没有表） | 缺什么说什么，退出码 2 |
| A | **前端契约测试**（23 项）：倒计时纯逻辑 / 时区陷阱 / 409 判别力 / 锁定接线 / "输入区在非锁定分支" / 前后端字面量对齐 | `pass=23 fail=0` |
| B | 起服务（假 AI + uvicorn，真实库，时长压成 N 秒） | 就绪 |
| C | 临时用户 + **自签令牌**被服务端接受 | HTTP 200 |
| D | **端到端**：`duration_seconds` 来自服务端 → 死线/倒计时 → 到点前 200（不误伤）→ 走到 00:00 → 到点后 **409 `interview_timeout`** → 第二次是 `no_active_session` → `last_ended.status=abandoned` → 报告体 `ended_reason=timeout` → 超时后能立刻重开 | 25 项 `[PASS]`，0 项 `[FAIL]` |
| E | **Python 直查库**：会话行 `status=abandoned` + `ended_reason='timeout'`；报告已落库且带 `ended_reason`；`answered_count` 与题目状态一致（假 AI 自报 7/99 被覆盖）；prompt 里写明 `ended_reason = timeout`；到点后写路径（chat / skip_question）被拒 | 逐条 `[PASS]` |
| F | 清理：users / interview_sessions / interview_records / notifications 复查 | 四张表均 0 行 |

### 最近一次实跑结果

```
$ .\venv\Scripts\python.exe scripts\verify_t42_manual.py --duration 12
...
汇总（逐段）
  [PASS] 预检（node / 三个文件 / 参数 / 库）
  [PASS] 前端契约测试（倒计时·锁定·销毁输入区）      <- pass=23 fail=0
  [PASS] 起服务（假 AI + uvicorn，真实库）
  [PASS] 临时用户 + 自签令牌
  [PASS] 端到端（e2e-timeout-live.mjs）              <- 节点自己打了 25 个 [PASS] / 0 个 [FAIL]
  [PASS] 服务端事实复核（Python 直查库）
  [PASS] 清理（确认无残留）

  耗时 25.1 秒
✅ T-42 / T-43 人工验收通过：…
退出码 0
```

关键证据行（真实输出，非示意）：

```
服务端字段: deadline_at=2026-09-29T17:36:23.890460 interview_remaining_seconds=11 duration_seconds=12
前端算出的倒计时: 00:11（剩余 11 秒）
[PASS] 倒计时不超过服务端设定的时长（无时区偏移） —— 剩余 11 秒，上限 14 秒
[PASS] deadline_at 与 interview_remaining_seconds 互相印证（按 UTC 解释） —— 偏差 974 毫秒
[PASS] 倒计时确实走到了 00:00（前端据此锁定 UI） —— 首次归零时刻与死线相差 137 毫秒
[PASS] 到点后的 /api/chat 返回 409 —— HTTP 409
[PASS] 前端会判定为"超时"并锁定 UI —— {"code":"interview_timeout","ended_reason":"timeout"}
[PASS] 锁定 Toast/面板文案直接来自服务端 detail —— 本场面试已超时自动结束，请开始一场新的面试
[PASS] 紧接着的第二次请求是 no_active_session（不是超时），前端不会重复锁 —— HTTP 409 code=no_active_session
服务端死线 = created_at(2026-09-29T17:36:11.890460) + 12 秒 = 2026-09-29T17:36:23.890460（前端倒计时照它走）
落库报告体的关键字段：ended_reason='timeout'  answered_count=1  total_questions=3
[PASS] 报告体的 answered_count(1) 与库里的题目状态一致 —— 计数是服务端事实，不是模型自报（假 AI 编的是 7 / 99）
[PASS] 真正喂给模型的 prompt 里写着 ended_reason = timeout —— 评分口径由服务端给定
[PASS] 临时用户及其 3 场会话（含落库报告）已全部删除，四张表复查均为 0
```

## 3. 只跑前端那一半（不需要后端）

```powershell
cd frontend
npm run test:node          # 含本任务的 23 项契约测试（零依赖、不联网、毫秒级）
npm run build              # tsc -b && vite build —— 实测 exit 0（22 modules，272.64 kB）
```

> ⚠️ 在 DSH 的受限沙箱里 `vite build` 会以 `Error: spawn EPERM` 失败：
> 它要 spawn 一个 stdio 走管道的子进程来打包配置文件，而沙箱禁止命名管道。
> 这是**沙箱边界**，不是构建本身有问题（同一条命令在放宽权限后 exit 0）。
> `tsc -b` 不受影响。

## 4. 已知边界（刻意如此，非遗漏）

* **脚本不驱动真实浏览器**。"锁定后输入区被销毁"是**源码结构契约**证明的
  （断言 `<textarea>` 落在 `interviewLocked ? … : (…)` 的非锁定分支里，
  并有反例自检），不是截图。要在浏览器里肉眼看，请用 `--duration 20` 起服务并手工点一遍：
  输入区应当**消失**、出现红色"本场面试已超时自动结束"面板与右上角 Toast。
* **T-43 的那句文案在报告 JSON 里不存在**：`TIMEOUT_REPORT_NOTE`
  （"因超时自动结束，仅基于已答部分评分"）是**前端常量**，
  由 `isTimeoutReport(report)` 这个开关控制渲染。因此验收分两层证明：
  端到端证明"报告体真的带 `ended_reason='timeout'`"（开关会被点亮），
  契约测试证明"标注确实挂在报告分支里、且非超时报告不显示"。
* **T-42 的依赖 T-40（`AuthContext`）尚未落地**：本次改动没有引入新的令牌读取路径
  （仍走 `services/api.ts` 的 `authFetch`），因此不构成阻塞；T-40/T-41 仍待做。
  代价是 `App.tsx` 继续变大（2349 → 2694 行），拆分留给 T-45~T-47。
* **`npm run lint` 本来就是红的**（HEAD 上 App.tsx 已有 46 条）。本次改动**新增 0 条
  `purity`/`set-state-in-effect` 违规**，但把另外 2 条**既有**代码形状问题顶到了台面上
  （`loadHistory`/`sendMessage` 的"先用后声明"、`logout` 未进依赖数组）——
  它们属于 T-44~T-47 的重构范围。
* **`--duration` 下限是 4 秒**：倒计时与"等时间走过去"各有 1~2 秒抖动，
  再小就会变成脚本自己在跟时钟赛跑，结论不再可信。
