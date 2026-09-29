# T-26 人工验收：报告改以服务端会话为准

> 对应任务：`docs/03-tasks.md` 的 **T-26**（Bug 3A 后端）
> 交付物：`backend/tests/test_report_from_session.py`、`backend/scripts/probes/probe_t26_report.py`、`backend/scripts/verify_t26_manual.py`

## 1. 这次到底改了什么

修复前 `POST /api/generate_report` 拿的是**前端传上来的聊天记录**（`req.messages`）当评分输入。
于是有三条路径同时是坏的：

| # | 问题 | 后果 |
|---|---|---|
| 1 | 评分输入可被伪造 | 前端删改聊天记录即可影响评分，用户能"自证清白" |
| 2 | Bug 3A 的根因之一 | 超时自动结束时前端状态可能已经乱了，传上来的 messages 与真实作答对不上 → 报告与进度脱节 |
| 3 | 白嫖路径 | **没有会话**也能凭一份 messages 换出一份"报告"，白拿 AI 额度 |

现在以**会话为唯一事实来源**：`questions`（出题）+ `user_answers`（实际作答）+ `question_status`（answered/skipped/pending）。
`ReportRequest` 这个模型已从 `backend/models/schemas.py` **删除**，接口**不再声明任何请求体**。

契约变更（对前端是破坏性的，已在 `frontend/src/App.tsx` 同步）：

```diff
- fetch(API + '/api/generate_report', { method:'POST', headers:{...}, body: JSON.stringify({ messages }) })
+ fetch(API + '/api/generate_report', { method:'POST', headers:{...} })   // 不再传 body
```

## 2. 傻瓜验证（一条命令，不用起服务、不用写 curl、不花钱）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t26_manual.py
```

它自己做了三件事，所以**没有"先起服务""换端口""引号地狱"这些坑**：

1. 起一个**本机假 AI**（`127.0.0.1` 回环，永不联网），把收到的 prompt 原样记下来；
2. 用 `OPENAI_BASE_URL` 指向它，另起一个 uvicorn（真实数据库 + **临时用户**）；
3. 走完整流程，然后检查**真正喂给模型的那段 prompt**。

> 这一点是刻意的：只断言"接口返回 200"证明不了 T-26 的任何东西 ——
> 必须看到 prompt 里**有什么**（会话里的题目与回答）和**没有什么**（客户端伪造的内容）。

### 它逐条证明什么

| 段 | 证明 | 期望 |
|---|---|---|
| A | `POST /api/generate_report` 的 OpenAPI 里**没有 requestBody** | 机器可读地证明"前端再也传不进 messages"；且该接口仍需鉴权 |
| B | 没有会话时，即使请求体里塞一份"我很完美"的伪造聊天记录 | **409** `no_active_session`（修复前能凭它生成"报告"） |
| C | 造一场真实面试：答 1 题 / 跳 1 题 / 留 1 题未答 | 三题状态分别为 answered / skipped / pending |
| D | 带着**伪造 messages** 生成报告，检查假 AI 收到的 prompt | prompt **不含** `T26-FORGED`，**含**真实题目与回答；未答 = `[尚未作答]`、跳过 = `[跳过此题]`、状态三件套随之下发；题量 = "共3轮提问" |
| E | 报告落库 + 置终态 + 释放唯一锁 | 库里 `status=finished` / `ended_reason=completed` / `report` 非空；`GET session -> null`；**立刻**能开新一场面试 |
| F | 空请求体也能出报告 | 不传 body 也 200 |

结束后临时用户及其会话**全部删除**（脚本会再查一次库确认无残留）。

### 最近一次实跑结果

```
✅ T-26 人工验收通过：接口无请求体、伪造 messages 进不了评分、未答/跳过如实标注、报告落库并释放锁
（临时用户与其 2 场会话已全部删除）
```

## 3. 判别力验证（证明测试真的抓得住，而不是"恰好通过"）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\probes\probe_t26_report.py
```

把 7 种"看起来还行、其实已经退回客户端语义"的退化逐个注入，每一种都必须让
`tests/test_report_from_session` 变红：

| 探针 | 注入的缺陷 | 结果 |
|---|---|---|
| P1 | 未作答的题不再标注（等于替候选人编造回答） | CAUGHT |
| P2 | 跳过与未作答混为一谈（评分口径失真） | CAUGHT |
| P3 | 丢掉 `question_status`（状态不下发给评分 → ADR-007R 的前提没了） | CAUGHT |
| P4 | 没有会话也照样出报告（白嫖路径复活） | CAUGHT |
| P5 | 题量写死（报告与真实面试轮数脱节） | CAUGHT |
| P6 | 评分成功却不落库/不置终态（用户被锁到 TTL 结束） | CAUGHT |
| P7 | AI 失败也置终态（用户失去重试机会） | CAUGHT |

探针运行前后对 `routers/interview.py` 做 sha256 校验（本次 `3014979…4a61` 前后一致）。

> ⚠️ 首轮跑时 P3/P4/P7 报的是 **INVALID（针命中 0 次）**，即**探针自己写错了** ——
> 多行 needle 用了 `\n`，而文件当时是 CRLF。`_fit()` 现在会把 needle 对齐到目标文件
> 真实的换行符。**"探针无效"必须当成探针的 bug 来处理**，不能当成"缺陷没被抓到"。

## 4. 已知边界（留给后续任务）

* **`ended_reason` 尚未进入报告本体**（T-27）：当前只有会话行上有 `ended_reason`，
  报告的 JSON 里还没有这个字段，前端也就无法据此显示"因超时自动结束"的文案。
* **超时兜底尚未接线**（T-28）：本任务的 `[尚未作答]` 是"如实呈现事实"，
  **不是**最终评分口径；"未及作答 vs 答不上"如何折算分数属 T-27。
* **前端仍会把 409 渲染成聊天气泡**：`/api/chat` 与 `/api/skip_question` 的
  `res.ok` 检查属 T-40/T-41 的前端接线工作，本轮只保证后端契约正确。
* 前端不再传 `messages` 这件事，目前由 **A 段（OpenAPI 无 requestBody）** 客观证明；
  前端源码层面的契约断言在 `frontend/tests/` 里（`ReportRequest` 已删除的断言）。
