# T-40 人工验收：`AuthContext` 集中并持久化 token + userId（Bug 2 前端，刷新不丢身份）

> 对应任务：`docs/03-tasks.md` 的 **T-40**（Bug 2 前端，依赖 T-36）
> 上游：`docs/34-manual-verification.md`（T-36 api 层收敛，本任务兑现它留的 `onTokenRefreshed` 口子）
> 下游关联：**T-41**（挂载时拉取会话并重建视图）、**T-50**（JWT 滑动续期，服务端下发续期头）
> 交付物（前端）：`src/auth/authStorage.ts`（新增）、`src/auth/AuthContext.tsx`（重写）、
> `src/auth/AuthBridge.tsx`、`src/services/authResponse.ts`、`src/services/api.ts`
> 交付物（契约/验收）：`frontend/tests/auth-persistence.test.mjs`（新增 14 项）、
> `frontend/tests/{route-guard-contract,api-convergence}.test.mjs`（适配）、
> `backend/scripts/verify_t40_manual.py`（新增）、
> `backend/scripts/{verify_t36,verify_t44_t45}_manual.py`（键清单口径同步）

## 1. 这次到底改了什么

T-44 把认证状态收敛进了 `AuthContext`，但留下两个洞；**Bug 2 的"刷新就报废"正是从这两个洞漏出去的**：

| # | 洞 | 触发 | 后果 |
|---|---|---|---|
| ① | **`userId` 不落盘** | `const [userId, setUserId] = useState<number \| null>(null)` | 登录响应那一刻才有值，**一刷新就没了** —— 前端答不出"我是谁" |
| ② | **续期令牌只写了 `localStorage`** | T-36 的 `handleRefreshedToken` 只落盘 | 当前页面里 `AuthContext.token` 仍是旧值，要等下次刷新才对得上 |

修复后：

| 关注点 | 唯一落点 |
|---|---|
| 持久化键清单（token / userId / role / username） | `authStorage.ts` 的 `AUTH_PERSISTED_KEYS` |
| 唯一写路径（含登出清空） | `AuthContext` 的 `persistAuthSnapshot()`（仅 `hydrated` 之后） |
| 启动还原（"刷新后 userId 仍存在"） | `AuthProvider` 的 `useReducer` **惰性初值** → `restoreAuthState()` |
| 续期令牌回灌内存态 | `AuthBridge` → `onTokenRefreshed(applyRefreshedToken)` |
| 跨标签页身份切换 | `AuthBridge` 监听 `storage` 事件 → `restoreFromStorage()` |

四个刻意的设计决定：

* **还原走首帧惰性初值，不走 effect。** 用 effect 还原会先渲染一帧"未登录"、
  再闪一下"已登录"，而且持久化 effect 有可能抢在前面把存储里的会话**抹掉**。
  现在 `AuthProvider` 第一帧就是还原后的状态，"刷新后 `userId` 仍存在"不依赖时序。
* **写盘前必须过 `hydrated` 守卫。** 这是同一类陷阱的通用形态：
  `useEffect(落盘)` 一旦在初始化那一帧跑，就会用空快照覆盖刚读出来的会话。
  契约测试专门断言"守卫必须在唯一写路径之前"。
* **`localStorage` 的容错包装只有一处，键清单只有一份。**
  存储不可用（隐私模式/沙箱）时降级为"仅内存"，但绝不抛 —— 认证降级可以，白屏不可以。
* **`role` 与 `userId` 解析都按白名单/正整数收窄。** 存储是用户可改的
  （DevTools 一行就能写），一个 `role='root'` 或 `userId='12abc'` 若直接进内存，
  前端守卫会做出无法解释的判断。宁可退化成"无身份/无角色"。

## 2. 傻瓜验证（一条命令，不用起服务、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t40_manual.py
```

可选参数：`--skip-tsc`、`--verbose`。退出码：0 = 通过；1 = 未通过；2 = 环境问题。

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 预检：`node`、5 个目标文件、`node_modules` | 缺什么说什么 |
| A | **契约测试** `node --test`（TAP） | `tests=115 pass=115 fail=0`，5 个点名用例必须真出现过 |
| B | `tsc -b` | exit 0 |
| C | **独立复核**（不 import 测试里的辅助函数）：① 源码重扫 / ② **逐个点名跑关键用例** / ③ **Python 侧独立复述持久化语义**（键清单从源码解析） / ④ 判别力自检与反例 | 全 `[PASS]` |

### 最近一次实跑结果（真实输出；中文在部分终端会显示为乱码，不影响判定）

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t40_manual.py
0. 预检        [PASS] node（v24.9.0）/ 5 个文件齐 / node_modules 在
A. 契约测试    [PASS] tests=115 pass=115 fail=0；点名用例 5/5 出现
B. 类型检查    [PASS] tsc -b 退出码 0
C. 独立复核    [PASS] ①~④ 全绿（含 5 条关键用例逐个点名跑通 + 9 条语义复述）
汇总：[PASS] A / [PASS] B / [PASS] C
✅ T-40 人工验收通过
退出码 0
```

关键证据行（真实输出，非示意）：

```
[PASS] ① 持久化清单（从源码解析）正好是这 4 个键：token、userId、role、username
[PASS] ① 写盘前有 hydrated 守卫（首帧空快照不会把存储里的会话抹掉）
[PASS] ① 首帧就用 restoreAuthState 惰性还原（不依赖 effect 时序，不会闪一下未登录）
[PASS] ① signIn / signOut / applyRefreshedToken 入口齐全，登出立刻清盘，T-36 接线未回退
[PASS] ① AuthContext 不再直接 setItem/removeItem（持久化口径只有一处）
     [PASS] 单独跑通（该用例唯一命中）：刷新后 userId 仍存在：写入 → 重新还原，整份会话身份原样回来
     [PASS] 单独跑通（该用例唯一命中）：登出清理干净：四个键全部消失，且不误伤同源的其他数据
     [PASS] 单独跑通（该用例唯一命中）：持久化只有一条写路径，且**只在 hydrated 之后**写（…）
     [PASS] 单独跑通（该用例唯一命中）：T-36 的口子已兑现：AuthBridge 订阅 onTokenRefreshed…
     [PASS] 单独跑通（该用例唯一命中）：userId 解析器拒绝一切非正整数（…）
[PASS] ③ 写入 → 丢掉内存重新还原：userId 仍然是 42
[PASS] ③ 判别力自检：userId 缺失时必须还原成"无身份"（否则上一条恒真）
[PASS] ③ 登出后 4 个键全部消失
[PASS] ③ 登出没有误删同源的非认证数据（不是粗暴 clear()）
[PASS] ③ user_id 解析只认正整数（空串/字母/负数/小数/NaN 全拒绝）
```

### 只跑前端那一半（不需要后端）

```powershell
cd frontend
npm run test:node                        # 115 项契约测试（零依赖、不联网、毫秒级）
node node_modules/typescript/bin/tsc -b  # 类型检查，实测 exit 0
```

既有验收脚本**全部仍然通过**（其中两个的口径已随本次结构变化同步）：

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t34_manual.py        # 退出码 0
.\venv\Scripts\python.exe scripts\verify_t36_manual.py        # 退出码 0
.\venv\Scripts\python.exe scripts\verify_t44_t45_manual.py    # 退出码 0
.\venv\Scripts\python.exe scripts\verify_t46_t49_manual.py    # 退出码 0
```

## 3. 契约测试怎么断言"刷新后 userId 仍存在 / 登出清理干净"

`frontend/tests/auth-persistence.test.mjs`（14 项）分三层，全部用**假 `localStorage` 真跑**
（`node:test` 能直接 import `.ts`，跑的是产线同一份代码，不是测试里另写的一份）：

1. **纯逻辑闭环**：`persistAuthSnapshot()` 写入 → 换掉内存、只留存储 → `restoreAuthState()`
   断言 `userId === 42`；`clearAuthStorage()` 后四个键全消失且 `ui-theme` 之类的
   同源数据**没有被误删**（证明不是粗暴 `clear()`）。
   判别力自检：把 `userId` 写成 `null` 时必须还原成 `null`，否则上面那条断言恒真。
2. **脏数据防御**：`userId` 拒绝 `''`/空白/`'abc'`/`'12abc'`/`'0'`/`'-1'`/`'3.5'`/`'NaN'`/`'1e3'`/`'0x10'`；
   `role` 只放行 `admin`/`user`；存储抛错时全部降级返回 `false`/`null` 而**不抛**。
3. **接线**：唯一写路径必须是 `persistAuthSnapshot` 且**前面有 `hydrated` 守卫**；
   首帧必须走惰性 `restoreAuthState`；`SIGN_IN`/`SIGN_OUT`/`REFRESH_TOKEN` 三个 reducer 分支齐全；
   `AuthModal` 必须把 `data.user_id` 交给 `signIn`（否则落盘的永远是 `null`）；
   `AuthBridge` 必须 `onTokenRefreshed(applyRefreshedToken)` 且 401 接线未回退。

`route-guard-contract.test.mjs` 里那条"AuthContext 把三件套纳入持久化写路径"的断言
也一起升级了：原来的 `writeStored('token')` 已经不存在（收敛成一次写整份快照），
现在断言"调用唯一写路径 + `authStorage` 的清单里有这 4 个键（含新增的 `userId`）"。

## 4. 已知边界（刻意如此，非遗漏）

* **还原是乐观的**：`restoreAuthState()` 不校验令牌是否过期（校验要发请求），
  所以它回答"上次登录留下的是谁"，**不回答"这个令牌还有效吗"**。
  真正失效的令牌会在下一次请求被 T-36 的统一 401 登出收拾掉；
  "挂载时拉取会话并重建视图"是 **T-41**。
  也就是说：**刷新后 `userId` 立刻可见**（本任务的验收标准），
  但它只保证"身份不丢"，不保证"身份已被服务端确认"。
* **`useInterviewSession` 挂载校验令牌时仍自己读 `localStorage.getItem('token')`**：
  那段逻辑依赖"有没有令牌"来决定要不要发校验请求，属于 **T-41** 的改造范围
  （拉取会话后重建视图）；本任务不动它，避免与 T-41 的会话接口打架。
* **不驱动真实浏览器**。"刷新后 `userId` 仍存在"由两段证据合成：
  ① 纯逻辑闭环（真跑写入/还原）；② 接线断言（还原路径就是 `AuthProvider` 首帧调用的同一个函数）。
  肉眼验收（不用改代码，可选）：
  1. `cd frontend; npm run dev`（先起后端）→ 登录 → 看 DevTools → Application →
     Local Storage：应出现 `token` / `userId` / `role` / `username` 四个键；
  2. **按 F5 刷新** → 界面仍显示已登录、用户名还在（而不是被踢回未登录）；
  3. 点"退出" → 再看 Local Storage：四个键**全部消失**，同源的其他键（例如界面偏好）保留。
* **跨标签页同步是"以最后一个写入者为准"**：另一个标签页登录/登出会派发 `storage` 事件，
  本页跟着切换身份（`AuthBridge` → `restoreFromStorage`）。这是刻意的最小实现：
  没有做"本页正在操作时不接受外部变更"的仲裁 —— 同一个人在两个标签页用不同账号属于越界用法，
  不在本任务口径内。
* **`npm run lint` 仍然是红的**：实测 `39 errors / 0 warnings`（T-36 之后是 40；
  本次把 `AuthContext` 的历史常量导出清掉，反而少了 1 条 `only-export-components`）。
  错误类型与文件分布没有新增来源：全是既有的 `no-explicit-any`、`set-state-in-effect`、
  `only-export-components`。本任务不假装 lint 是绿的。
* **`verify_t40_manual.py` 的 C-③ 是"另一套语言的语义复述"，不是"再跑一遍 JS"**：
  `.ts` 模块 Node 不能直接 import（要类型剥离），本脚本第一版试过"机械转换成 `.mjs` 再 import"，
  结果那套 TS 语法剥离规则把探针副本里的运行逻辑**改写了**
  （对象字面量的值、泛型实参被啃掉），"探针通过"不再等于"被测代码通过" —— 已删除。
  现在：**真正的执行交给 A 段与 C-②（`node:test` 有类型剥离，跑的是原文件）**，
  C-③ 只从源码解析键清单并独立复述语义。这条边界是刻意保留的，不是遗漏。
