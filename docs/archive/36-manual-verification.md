# T-50 人工验收：JWT 滑动续期 + 8 小时绝对上限 + `auth_time`（ADR-016，P0）

> 对应任务：`docs/03-tasks.md` 的 **T-50**（P0，依赖 T-36）
> 上游：`docs/34-manual-verification.md`（T-36 前端前置：读头落库的落点）、
> `docs/35-manual-verification.md`（T-40 持久化）
> 下游关联：**T-51**（`jti` 声明 + `token_blacklist` 真登出，本任务把 `jti` 沿用同一条续期链的前提备好）
> 交付物（后端）：`utils/token_renewal.py`（新增）、`utils/token_renewal_middleware.py`（新增）、
> `auth.py`、`routers/auth_router.py`、`main.py`、`tests/test_token_renewal.py`（新增）、`tests/support.py`
> 交付物（前端）：`src/services/authResponse.ts`、`src/services/api.ts`、
> `tests/api-convergence.test.mjs`
> 交付物（验收）：`backend/scripts/verify_t50_manual.py`（新增）

## 1. 这次到底改了什么

T-36 把前端改造成了"能读 `X-Refreshed-Token` 并落库"，但**后端从来没有发过这个头** ——
令牌 30 分钟固定到期，一次面试（15 分钟）+ 上传简历 + 生成报告很容易越过它，
而 T-36 之后"401 即统一登出"，于是长面试会被**静默踢出并丢进度**。这就是 ADR-016 记的 P0。

修复后（ADR-016 的五条规则逐条落地）：

| # | 规则 | 落点 |
|---|---|---|
| 1 | 回写头名固定 `X-Refreshed-Token` | `utils/token_renewal.REFRESHED_TOKEN_HEADER` |
| 2 | 剩余有效期 **< 50%** 才续期 | `should_renew()`（阈值 `RENEWAL_THRESHOLD_RATIO = 0.5`） |
| 3 | **8 小时绝对上限**（`auth_time` 起算） | `exceeds_absolute_lifetime()`；超限 → `401 + X-Token-Expired: absolute` |
| 4 | 续期**沿用同一 `jti`** | `renewed_claims()`（T-51 的黑名单据此吊销整条链） |
| 5 | 续期只换 `exp`/`iat`，身份逐字复制 | `IDENTITY_CLAIMS = (sub, user_id, role, jti, auth_time)` |

四个刻意的设计决定：

* **规则 = 纯函数，中间件只做装配。** `utils/token_renewal.py` 零依赖（不 import config/DB/fastapi），
  输入是"已解码的 payload + 当前时刻"。这样"8 小时"这种规则能被**固定时钟**精确验证 ——
  靠手点页面是验不出来的（真实等待 8 小时显然不可行）。
* **续期挂在 ASGI 层，不逐个端点。** 漏掉任何一个端点都会表现为"某些接口永远不续期"，
  且只在长时间会话中偶发。`TokenRenewalMiddleware` 对**所有**受保护请求生效。
* **只续期"仍然有效"的令牌。** 已过期的令牌**不会**因为"还在 8 小时内"被续期 ——
  那等于让过期令牌无条件复活、`exp` 形同虚设。代价是"挂机超过 30 分钟要重新登录"，
  与 ADR-016 的动机（一次面试+上传+报告不被踢出）不冲突。
* **绝对上限是 401，但前端不登出。** 这是 R-9 的 UX 要求"提示重新登录且保留进度"：
  服务端会话与面试进度都还在，静默登出会把"可恢复的进度"变成"必须先登录"。
  因此响应多带一个 `X-Token-Expired: absolute` 标记，前端据此**只提示、不登出**
  （见 `services/api.ts` 的绝对上限分支）。

顺带修掉一个**文档与实现不一致的隐患**：`create_access_token` 的默认寿命此前写死
`timedelta(minutes=15)`，而 `config.ACCESS_TOKEN_EXPIRE_MINUTES = 30` ——
"令牌寿命"因此无法从一个地方解释。现在默认值读配置（单一来源）。

## 2. 傻瓜验证（一条命令，不用起服务、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t50_manual.py
```

可选参数：`--skip-full-tests`（跳过后端全量 562 项，约 80 秒）、`--skip-frontend`。
退出码：0 = 通过；1 = 未通过；2 = 环境问题。

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 预检：python / 6 个后端文件 / 3 个前端文件 / node | 缺什么说什么 |
| A | ① `tests.test_token_renewal` 30 项；② **全量** 562 项 | 两段都 `OK` |
| B | 6 个改动文件 `py_compile` | 全通过 |
| C | **独立复核**：重跑关键语义 + 前端源码接线（不用测试里的断言） | 17 条语义 + 5 条接线全 PASS |
| D | 前端 `node --test` + `tsc -b` | 116 项全绿 + exit 0 |

### 最近一次实跑结果（真实输出；中文在部分终端会显示为乱码，不影响判定）

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t50_manual.py
0. 预检        [PASS] 6 个后端文件 + 3 个前端文件 + node（v24.9.0）
A. 后端测试    [PASS] tests.test_token_renewal: Ran 30 tests OK
               [PASS] 全量：Ran 562 tests OK（续期中间件没打坏既有链路）
B. 语法检查    [PASS] 6 个改动文件全部通过 py_compile
C. 独立复核    [PASS] ① 17 条语义 ② 5 条前端接线
D. 前端契约    [PASS] tests=116 pass=116 fail=0；tsc -b exit 0
汇总：[PASS] A / [PASS] A2 / [PASS] B / [PASS] C / [PASS] D
✅ T-50 人工验收通过
退出码 0
```

关键证据行（真实输出，非示意）：

```
[PASS] ① 登录签发的令牌带 jti 与 auth_time（绝对上限的起算点）
[PASS] ① 恰好 50% 剩余**不**续期
[PASS] ① 过半衰点一秒就续期（阈值不是恒假）
[PASS] ① 恰好 8 小时算超限（`>=`）
[PASS] ① 差一秒不算超限（边界不是恒真）
[PASS] ① 续期沿用同一 jti（T-51 吊销整条链的前提）
[PASS] ① 续期不重置 auth_time（否则绝对上限被绕过）
[PASS] ① 续期把 exp 往后延一个完整寿命
[PASS] ① 超 8h 的拒绝原因码是 absolute_limit
[PASS] ① 已过期令牌的原因码是 expired（不续期）
[PASS] ① 续期中间件真的注册在 app 上
[PASS] ① CORS 暴露 X-Refreshed-Token 与 X-Token-Expired
[PASS] ① 端到端：低剩余寿命请求 200 且响应头带新令牌
[PASS] ① 端到端：超 8h 请求 401 且带 X-Token-Expired: absolute
[PASS] ① 端到端：刚签发的令牌不续期（没有写放大）
[PASS] ② 前端认识 X-Refreshed-Token 响应头
[PASS] ② 前端认识 X-Token-Expired 响应头与 absolute 取值
[PASS] ② 绝对上限分支排在统一登出之前（保留会话可恢复）
[PASS] ② 普通 401 仍然统一登出（T-36 不变量未回退）
```

### 只跑后端那一半（不需要前端）

```powershell
cd backend
.\venv\Scripts\python.exe -m unittest tests.test_token_renewal -v   # 30 项
.\venv\Scripts\python.exe -m unittest discover -s tests -t . -q     # 562 项
```

既有验收脚本**仍然通过**（其中 T-42 是真正起服务、发 HTTP 的全栈脚本，
本次已实跑确认续期中间件没有打坏它）：

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t42_manual.py   # 退出码 0（27.2 秒，端到端）
```

## 3. 测试怎么断言这三条验收标准

`backend/tests/test_token_renewal.py`（30 项）分两层，缺一层都会留下"看起来实现了"的假象：

1. **纯逻辑**（固定时钟，不碰网络/数据库）：半衰点边界（恰好 50% 不续期、过半秒续期）、
   8 小时边界（恰好算超限、差一秒不算）、缺 `auth_time` **fail-closed**、
   `auth_time` 为 `True`/`[]`/`{}` 等脏值一律视为超限、老令牌（无 `jti`/`auth_time`）不续期、
   续期后 `jti`/`auth_time` 不变而 `exp` 往后延、两次签发的 `jti` 必须不同（否则吊销会误伤）。
2. **接线**（`TestClient(main.app)`，真发请求）：
   `GET /api/interview/config` 带"剩 9/30"的令牌 → 200 **且**响应头有 `X-Refreshed-Token`；
   该令牌能继续用、且**不会**立刻又被续一次（写放大护栏）；
   带"寿命 10h / 已活 9h / 仍有效"的令牌 → **401 + `X-Token-Expired: absolute`**，
   且写接口（`/api/user/profile`）同样被挡；
   过期令牌 → 普通 401（**不带** absolute 标记）；公开端点（验证码/题库）完全不受影响。

前端契约（`tests/api-convergence.test.mjs`）新增一条：**绝对上限分支必须排在统一登出之前**，
并断言该分支抛出可识别的 `SessionAbsoluteExpired` + 带上 `tokenExpired` 标记。

## 4. 已知边界（刻意如此，非遗漏）

* **不驱动真实浏览器**。"读头落库"由 T-36 的契约测试（假 `storage` 真跑）证明，
  "绝对上限不登出"由 `services/api.ts` 的分支顺序 + `ApiError.isAbsoluteExpiry` 证明。
  肉眼验收（不用改代码，可选，需要起后端）：
  1. `cd backend; .\venv\Scripts\python.exe -m uvicorn main:app --port 8000`；
  2. 登录后打开 DevTools → Network → 任一 `Authorization` 请求：
     响应头里**不该**立刻出现 `X-Refreshed-Token`（剩余 > 50%）；
  3. 等 16 分钟（30 分钟寿命过半）后再点一次：应当出现 `X-Refreshed-Token`，
     且 Application → Local Storage 里的 `token` **跟着变了**（前端落库生效）。
* **绝对上限不要求重新登录验证 8 小时**：脚本用"寿命 10 小时、已活 9 小时"的令牌构造，
  这是唯一可行的离線方式（等价于把时钟拨过去）。
* **被吊销令牌 30 分钟后复活的老问题**（`docs/02-arch-review.md` R-4）**本任务不解决**：
  那需要 `token_blacklist`，属 **T-51**（🔒 待启动）。本任务只把前提备好 ——
  续期沿用同一 `jti`，因此 T-51 的黑名单能一次性吊销整条续期链，
  且黑名单 TTL 必须 ≥ 8 小时（否则吊销形同虚设）。
* **分域部署下要动一处**：`TokenRenewalMiddleware` 注册在 CORS **外层**
  （Starlette 里"后注册者在外层"），因此它自己返回的 401 **不带 CORS 响应头**。
  同源托管（本项目已选，见 `docs/33` §5）无影响；将来若真分域，需要把它移到 CORS 内层，
  或显式补 CORS 头（登记在 T-35 的备选里）。
* **`npm run lint` 仍然是红的**：实测 `39 errors / 0 warnings`（与 T-40 之后相同，
  本次没有新增；两个改动的前端文件单独 lint 是干净的）。
