# T-48 / T-49 人工验收：死代码清理 + 题库缺字段不崩

> 对应任务：`docs/03-tasks.md` 的 **T-49**（`q.tags?.map` 可选链，题库缺字段不崩，FR-9.2）
> 与 **T-48**（死代码清理：`historyListRef`、`QuestionBank.tsx` 处置、`_passwordError`）
> 执行顺序：先 T-49（一行救命代码），再 T-48（涉及删文件）。
> 上游：`docs/31-manual-verification.md`（T-46 / T-47 拆分）
> 交付物（前端）：`src/interview/questionBankEntry.ts`（新增）、
> `src/interview/QuestionBankModal.tsx`、`src/notifications/useNotificationCenter.ts`、
> `src/notifications/NotificationCenter.tsx`、`src/auth/AuthModal.tsx`；
> **删除** `src/QuestionBank.tsx`
> 交付物（契约/验收）：`frontend/tests/question-bank-robustness.test.mjs`、
> `frontend/tests/dead-code-contract.test.mjs`、
> `backend/scripts/verify_t46_t49_manual.py`（统一入口，覆盖 T-46 ~ T-49）

## 1. 这次到底改了什么

### T-49：题库缺字段不再白屏（FR-9.2）

修复前 `QuestionBankModal` 里写的是 `q.tags.map(...)`。题库条目来自后端 JSON，
`tags` 字段在历史数据 / 后端改字段 / 降级返回时可能整个缺失 ——
**一条这样的题目就让整个题库弹窗白屏**（React 渲染期 TypeError）。

修复方式不是就地加一个 `?.`，而是把"读取"收敛成一个**可独立测试的纯函数**：

```ts
// src/interview/questionBankEntry.ts
export function questionTags(entry: { tags?: unknown } | null | undefined): string[] {
  if (!entry) return [];
  return Array.isArray(entry.tags) ? entry.tags : [];
}
```

两个刻意的设计：

1. **只加 `?.` 不够**：`q.tags?.map` 挡不住 `tags: null`（`?.` 只跳过 `null`/`undefined`
   的**属性访问**，这里 `tags` 为 `null` 时 `q.tags?.map` 恰好也不抛，但 `tags: 'str'`
   或 `tags: {}` 仍会抛 `map is not a function`）。`Array.isArray` 把三种坏输入一起挡住。
2. **刻意不 import `../config`**：`config.ts` 读 `import.meta.env`，在 Node 里是 `undefined`
   一 import 就抛。把这个文件做成零依赖纯逻辑，`node:test` 就能**直接 import 并断言行为**
   —— "缺字段不崩"是行为测试证明的，不是靠 grep 猜的（见 §3）。

顺带发现：被删掉的那个死组件 `src/QuestionBank.tsx` 里写的**恰恰是** `q.tags?.map(...)`
（它比线上那份更安全）。这也说明"死代码"不只是碍眼 —— 它会让人误以为某个洞已经补上了。

### T-48：三处死代码

| 死代码 | 它到底是什么 | 处置 |
|---|---|---|
| `historyListRef`（`notifications/useNotificationCenter.ts` + `NotificationCenter.tsx`） | `useRef` 声明 + 绑到 `<ul ref={…}>`，但**从未被读过** —— 点通知后的高亮滚动走的是 `document.getElementById('record-…')` + `scrollIntoView`。只写不读的 ref | 删掉声明、API 字段与 `<ul ref>` |
| `_passwordError`（`auth/AuthModal.tsx`） | `const [_passwordError, setPasswordError] = useState('')`，写入 3 处、**没有一处读取**（真正的错误文案走 `authError`） | 删掉状态与 3 处写入 |
| `src/QuestionBank.tsx` | 一个**从未被任何地方 import** 的旧题库组件（原 `App.tsx` 用的是内联弹窗；T-47 之后用的是 `QuestionBankModal`） | `git rm` 整个文件 |

清理后：全库 `historyListRef` / `_passwordError` / `setPasswordError` **grep 为 0**，
`src/QuestionBank.tsx` 不存在，且**没有删错**（活的三个题库文件仍在，见 §2 C 段 ⑪）。
被删文件仍可从历史取回：`git show HEAD~1:frontend/src/QuestionBank.tsx`。

行数变化（脚本口径 `split("\n")`）：`QuestionBankModal` 96 → 97（多了 import 与注释）、
`useNotificationCenter` 259 → 258、`NotificationCenter` 145 → 146、`AuthModal` 299 → 297；
全树仍是 **30 个 `.ts/.tsx`**（新增 `questionBankEntry.ts` 与删除 `QuestionBank.tsx` 相抵），
最大文件仍为 `interview/useInterviewSession.ts` 383 行。

## 2. 傻瓜验证（一条命令，不用起服务、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t46_t49_manual.py
```

它同时覆盖 **T-46 / T-47 / T-48 / T-49**（旧的 `verify_t46_t47_manual.py` 已并入这一个入口）：

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 预检：`node`、14 个交付物、`node_modules`、8 个契约测试文件 | 缺什么说什么 |
| A | **契约测试** `node --test`（TAP） | `tests=76 pass=76 fail=0`，13 个点名用例必须真出现过 |
| B | `tsc -b` | exit 0 |
| C | **本脚本自己重扫源码**（不复用测试辅助函数）：①~⑨ 拆分不变量 + **⑩ T-49 防崩** + **⑪ T-48 死代码**，每节都带判别力自检，⑩ 还独立跑一次 `node` 做**行为取证** | 全 `[PASS]` |
| D | `vite build` 探测（默认跳过） | 见 §4 |

### 最近一次实跑结果（真实输出）

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t46_t49_manual.py
0. 预检        [PASS] node（v24.9.0）/ 14 个文件齐 / 8 个契约测试文件
A. 契约测试    [PASS] tests=76 pass=76 fail=0；点名用例 13/13 出现
B. 类型检查    [PASS] tsc -b 退出码 0
C. 独立复核    [PASS] ①~⑪ 全绿 + 5 组判别力自检 + 1 次独立行为取证
D. 构建探测    [SKIP] 默认不跑（沙箱 EPERM，见 §4）
汇总：[PASS] A / [PASS] B / [PASS] C / [SKIP] D
✅ T-46 ~ T-49 人工验收通过
退出码 0
```

关键证据行（真实输出，非示意）：

```
解析结果：tests=76 pass=76 fail=0
[PASS] ① 全树单文件都在 ≤400 行预算内（最大 interview/useInterviewSession.ts，383 行）
[PASS] ② App.tsx 227 行（T-46 前 2189 行 → 现在只剩装配）
[PASS] ⑦ 39 条 T-42 / T-43 / T-44 不变量在拆分后全部仍在
[PASS] ⑩ T-49：题库弹窗的标签渲染走 questionTags(q)
[PASS] ⑩ T-49：全库无裸 `q.tags.map(`（去注释后）
[PASS] ⑩ 行为取证（独立跑 node）：8 种缺字段输入全部返回 []，而裸 q.tags.map 确实抛 TypeError
[PASS] ⑪ T-48：historyListRef（只写不读的 ref） —— 全库为 0
[PASS] ⑪ T-48：_passwordError（只写不读的状态） —— 全库为 0
[PASS] ⑪ 判别力自检：修复前的三处死代码写法全部被判为命中
[PASS] ⑪ T-48：死组件 src/QuestionBank.tsx 已删除，活的 QuestionBankModal.tsx 仍在
[PASS] ⑪ T-48：题库三个活文件（取数 / 防崩读取 / 弹窗）都还在
```

### 只跑前端那一半（不需要后端）

```powershell
cd frontend
npm run test:node                        # 76 项契约测试（零依赖、不联网、毫秒级）
node node_modules/typescript/bin/tsc -b  # 类型检查，实测 exit 0
```

T-44 / T-45 的验收脚本**仍然通过**：`cd backend; .\venv\Scripts\python.exe scripts\verify_t44_t45_manual.py`（退出码 0）。

## 3. 契约测试怎么断言这两件事

### T-49：`tests/question-bank-robustness.test.mjs`（6 项）

这一条**不是**源码 grep，而是**真实行为断言** —— `questionTags` 是被 `import` 进来直接调用的：

| 用例 | 期望 |
|---|---|
| 完全没有 `tags` 字段 / `tags: undefined` / `tags: null` / `tags: '字符串'` / `tags: {}` / 条目本身为 `null` / `undefined` / `{}` | 一律返回 `[]`，**不抛异常** |
| `tags: ['react','hooks']` | 原样返回（不做去重/复制，避免"修 bug 修出新行为"） |
| 判别力 | 修复前的写法 `q.tags.map(t => t)` 喂进 `{}` 与 `{tags: null}` **必须抛 TypeError** |

另加两条源码结构断言：渲染层必须写 `questionTags(q).map(`、不得出现裸 `q.tags.map(`、
`questionBank[selectedBankCategory]?.map(` 的可选链不得丢失；以及"`questionBankEntry.ts`
不许 import `config`"（否则上面那些行为测试在 Node 里根本跑不起来）。

### T-48：`tests/dead-code-contract.test.mjs`（8 项）

把验收标准里那句"全库 grep 为 0"固化成断言，每条都配**判别力自检**：

1. `historyListRef` 全库为 0 —— 判别力：拿修复前的两行（声明 + `<ul ref={…}>`）喂进同一个 grep，必须命中 2 处；
2. `_passwordError` / `setPasswordError` 全库为 0 —— 判别力：同法必须命中 1 + 2 处；
3. `src/QuestionBank.tsx` 不存在、且无人 `import ... from './QuestionBank'`
   —— 判别力：同一个 `existsSync` 对**活的** `QuestionBankModal.tsx` 必须为真（否则"文件没了"可能是被判错的路径掩盖的）；同一个 import 正则对活的 `QuestionBankModal` 必须**不**命中；
4. **清理不许删错东西**：题库三个活文件仍在、`/api/question_bank` 归属唯一、`App.tsx` 仍装配题库弹窗、全树仍在 ≤400 行预算内。

扫描口径是**整棵 `src` 树**（T-46/T-47 之后"前端"不再等于一个文件），并**去掉注释后**判定
—— 因为注释里正引用着被删掉的旧写法（那是文档，不是代码）。

## 4. 已知边界（刻意如此，非遗漏）

* **不驱动真实浏览器**。"缺 tags 的题目渲染出来是一个空标签行、不会白屏"是
  **行为测试 + 源码契约**证明的（纯函数断言 + 渲染层必须走 `questionTags`），不是截图。
  肉眼验收：`cd frontend; npm run dev` → 打开题库 → 正常显示；
  想复现修复前，把 `QuestionBankModal.tsx` 里改回 `q.tags.map(`，删掉某条题目的 `tags` 字段
  （或让后端少返回该字段）即可看到白屏。
* **`npm run lint` 本来就是红的**：本轮之后 **42 errors / 0 warnings**（上一轮 43）。
  减少的那一条正是 `_passwordError` 的 `no-unused-vars`。三条结构性 Hook 规则
  （`react-hooks/refs` / `immutability` / `purity`）在验收脚本里是**门槛**，其余只报数字。
* **`vite build` 在受限沙箱里以 `spawn EPERM` 失败**（要 spawn 走管道的子进程，
  沙箱禁止命名管道）：与 `docs/29` §3、`docs/30` §4、`docs/31` §4 是同一个沙箱边界，
  验收脚本把它标成 `[ENV]` / `[SKIP]`，不是本次改动的问题。
* **删除不是"拆分"的替代品**：T-48 只删了**零引用**的东西（`QuestionBank.tsx`）
  与**只写不读**的东西（`historyListRef`、`_passwordError`），并在契约测试里加了
  "活文件仍在、接口归属唯一"的反向断言，防止"为了达标而删"。
* **`git rm` 之后文件名不会再出现在 grep 里**，但历史仍可追：
  `git show HEAD~1:frontend/src/QuestionBank.tsx`（它在 T-47 时还带着
  `q.tags?.map` 的"看似已修"写法，是这次 T-49 的直接线索）。
