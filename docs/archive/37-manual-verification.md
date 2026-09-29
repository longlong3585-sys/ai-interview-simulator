# T-51 人工验收：`jti` 声明 + `token_blacklist` 真登出（ADR-003 选 B）

> 对应任务：`docs/03-tasks.md` 的 **T-51**（依赖 T-22、T-50；本任务解冻执行）
> 上游：`docs/36-manual-verification.md`（T-50 把续期链拉到 8 小时，**正是因为这一点**，
> "前端删掉令牌 = 登出"不再成立）、`docs/23-manual-verification.md`（T-22 的清理任务；
> 本任务把 `token_blacklist` 纳入它的第四个动作）
> 下游关联：**T-50 的 R-4**（黑名单 TTL 必须 ≥ 8h，否则被吊销令牌会复活）
> 交付物（后端）：`utils/token_revocation.py`（新增）、
> `services/stores/sqlite_token_blacklist_store.py`（新增）、
> `services/stores/base.py`（新增 `TokenBlacklistStore` 协议）、
> `services/stores/factory.py`（登记第 4 个存储 + 装配自检）、
> `services/stores/__init__.py`、`auth.py`、`routers/auth_router.py`、
> `utils/token_renewal_middleware.py`、`main.py`、`scripts/cleanup.py`、
> `tests/test_token_revocation.py`（新增 34 项）、`tests/test_cleanup.py`（适配）
> 交付物（前端）：`src/auth/AuthContext.tsx`、`tests/auth-persistence.test.mjs`
> 交付物（验收）：`backend/scripts/verify_t51_manual.py`（新增）

## 1. 这次到底改了什么

修复前"退出登录"只有**前端动作**：`localStorage` 里的令牌被删掉，令牌本身仍然有效。
共享电脑、浏览器同步、日志泄漏都可能把它抄走 —— 而 T-50 之后这条链最长能活 **8 小时**。

修复后（ADR-003 选 B）：

| 环节 | 落点 |
|---|---|
| 签发时给出链标识 | `auth.create_access_token` 并入 `jti`（T-50 已做，续期沿用同一 `jti`） |
| 登出时吊销 | `POST /api/logout` → `utils/token_revocation.revoke_token()` → `token_blacklist` |
| 每个受保护请求都查 | `auth.get_current_user`（**鉴权最底层**，`require_user`/`get_current_admin_user` 全依赖它）+ 续期中间件 |
| 留多久 | `blacklist_expiry()` = **`now + 8h`**（与 T-50 的绝对上限取同一个数值） |
| 清理 | `scripts/cleanup.py` 第 4 个动作：只删 `expires_at <= now` |

四个刻意的设计决定：

* **TTL 取"恰好 8 小时"而不是"令牌自己的 exp"**。这是 R-4 的直接落点：
  任何合法令牌都活不过 `auth_time + 8h`，所以记录活到 `now + 8h` **一定**覆盖它的
  剩余寿命（满足"≥8h"）；反过来留更久没有意义（没有令牌能活那么长，只会堆垃圾行）。
  初稿的 30 分钟会让"登出 → 等半小时 → 复用旧令牌"成立。
* **登出端点是幂等的，而且不要求令牌仍然有效**。它不过 `Depends(get_current_user)`：
  拿着**已过期**的令牌点退出也必须成功（页面挂了很久再点），否则前端只会把它当网络错误 ——
  用户以为登出了，服务端那枚令牌却还在别处有效。签名字面正确就吊销。
  同理 `/api/logout` 进了中间件的"跳过黑名单与续期"名单：拿着**已被吊销**的令牌再点一次
  也得 200（否则"再确认一次已登出"会变成一个报错）。
* **吊销按 `jti`，续期沿用同一 `jti`** ⇒ 一次登出吊销**整条续期链**。
  这条有专门的端到端用例：用旧令牌登出，再用**续期后**的令牌访问 → 必须 401。
* **查询过滤 `expires_at > now` 而不是"这行在不在"**。否则清理任务一旦延后，
  用户就会被自己的旧令牌永久挡在 401 后面 —— 而且看不出原因。
  写入与读取的判据严格互补（`> now` / `<= now`），与 T-20/T-21 的互补纪律一致。

## 2. 傻瓜验证（一条命令，不用起服务、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t51_manual.py
```

可选参数：`--skip-full-tests`、`--skip-frontend`。退出码：0 = 通过；1 = 未通过；2 = 环境问题。

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 预检：python / 9 个后端文件 / 2 个前端文件 / node | 缺什么说什么 |
| A | ① `tests.test_token_revocation` 34 项；② **全量** 597 项 | 两段都 `OK` |
| B | 9 个改动文件 `py_compile` | 全通过 |
| C | **独立复核**：重跑语义 + `TestClient` 端到端 + 前端接线 | 22 条语义 + 3 条接线全 PASS |
| D | 前端 `node --test` + `tsc -b` | 117 项全绿 + exit 0 |

### 最近一次实跑结果（真实输出；中文在部分终端会显示为乱码，不影响判定）

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t51_manual.py
0. 预检        [PASS] 9 个后端文件 + 2 个前端文件 + node（v24.9.0）
A. 后端测试    [PASS] tests.test_token_revocation: Ran 34 tests OK
               [PASS] 全量：Ran 597 tests OK
B. 语法检查    [PASS] 9 个改动文件全部通过 py_compile
C. 独立复核    [PASS] ① 22 条语义 ② 端到端 ③ 前端接线
D. 前端契约    [PASS] tests=117 pass=117 fail=0；tsc -b exit 0
汇总：[PASS] A / [PASS] A2 / [PASS] B / [PASS] C / [PASS] D
✅ T-51 人工验收通过
退出码 0
```

关键证据行（真实输出，非示意）：

```
[PASS] ① 黑名单 TTL = 8 小时，且**等于** T-50 的绝对上限（R-4 的护栏）
[PASS] ① 令牌只剩 5 分钟 → 仍按 8 小时留痕（下限兜住，R-4 的修正点）
[PASS] ① 令牌已过期 → 仍留痕（否则登出在时钟偏差下变成空操作）
[PASS] ① 令牌声称能活 20 小时 → 不跟着延长（不产生垃圾行）
[PASS] ① 缺 jti / 非字符串 jti 一律视为不可吊销
[PASS] ③ 重复吊销同一个 jti 是**幂等**的（只留一行）
[PASS] ④ 已过期的吊销记录不算「已吊销」（与「已被清理」行为一致）
[PASS] ⑤ 清理**只**删已过期记录，仍生效的必须留着（删了就复活）
[PASS] ⑥ 端到端：**登出后旧 token 两个端点都 401**（验收标准）
[PASS] ⑥ 端到端：**续期后的令牌也被吊销**（jti 沿用 ⇒ 整条链一次性吊销）
[PASS] ⑥ 端到端：另一个会话（不同 jti）不受影响
[PASS] ⑥ 端到端：拿着**已过期**的令牌登出仍然成功
[PASS] ⑥ 端到端：老令牌（无 jti）登出返回 no_jti，且鉴权照旧
[PASS] ⑦ 顺序正确：**先**调登出（请求才带得上令牌）**再**清本地
```

### 只跑后端那一半

```powershell
cd backend
.\venv\Scripts\python.exe -m unittest tests.test_token_revocation -v   # 34 项
.\venv\Scripts\python.exe -m unittest discover -s tests -t . -q         # 597 项
```

既有验收脚本**仍然通过**：`verify_t42_manual.py`（真起服务、走 HTTP 的全栈脚本）
与 `verify_t50_manual.py` 均已复跑为 exit 0 —— 登出/黑名单没有破坏续期链路。

## 3. 测试怎么断言这两条验收标准

`backend/tests/test_token_revocation.py`（34 项）分三层：

1. **纯逻辑**（固定时钟）：TTL **等于** `ABSOLUTE_MAX_LIFETIME_SECONDS`（R-4 护栏）；
   令牌快到期 / 已过期 / 缺 `exp` / 声称能活 20 小时，四种情形都按 8h 留痕；
   产出的是**朴素 UTC ISO 串**且与 `stores/base` 的工具逐字一致
   （格式漂移会让 `expires_at > now` 的字典序比较**静默**失效）；缺 `jti` / 非字符串 `jti` 不可吊销。
2. **存储**：写入幂等（`INSERT OR REPLACE`，两个标签页点退出或前端重试都不会 500）；
   `expires_at <= now` 视为已失效；清理**只**删已过期；
   协议 `isinstance` 自检 + 工厂能装配（漏了 registry 一行会运行时 AttributeError）。
3. **接线**：登出前 2 个端点 200 → `POST /api/logout` 返回 `revoked: true` → **登出后同样两个端点 401**；
   401 带 `X-Token-Revoked`；**已吊销的令牌不会被续期**（否则吊销被自己的续期逻辑绕过）；
   续期后的令牌同样失效；不同 `jti` 的会话不受影响；管理员同样被吊销；
   过期令牌 / 无令牌 / 坏令牌 / 无 `jti` 老令牌的登出都**成功**且原因可辨。

前端契约（`tests/auth-persistence.test.mjs`）新增一条：`signOut` 必须**先**调
`apiPost('/api/logout')` **再**清本地，且是 fire-and-forget（带 `.catch`）。
顺序是功能正确性而非风格 —— 先清本地的话请求就不带令牌，服务端无从吊销。

`scripts/cleanup.py` 新增第 4 个动作，并在 `tests/test_cleanup.py` 里加了一条
"清理绝不删除仍生效的吊销记录"的用例（删了就等于让被吊销令牌复活）。

## 4. 已知边界（刻意如此，非遗漏）

* **不驱动真实浏览器**。"登出时先调服务端"由契约测试的源码顺序断言 + 判别力自检证明。
  肉眼验收（不用改代码，可选，需要起后端）：
  1. 登录后 `F12 → Application → Local Storage` 复制 `token` 的值；
  2. 点"退出" → 用刚才复制的令牌手工发一次请求（例如在 Console 里
     `await fetch('/api/interview/config', {headers: {Authorization: 'Bearer 粘贴'}})`）
     → 必须 **401**（修复前会是 200）；
  3. 关掉页面重新登录 → 新令牌可用（吊销是按 `jti` 的，不会误伤新会话）。
* **老令牌（无 `jti`）无法被吊销**：这是 T-50 之前签发的令牌，登出接口会明确返回
  `reason: "no_jti"`（而不是假装成功）。它们最长 30 分钟后自然失效；
  前端拿到这个原因可以提示"请重新登录以完成登出"。这是**如实声明**，不是遗漏。
* **不做"登出全部设备"**：当前接口吊销的是**本次会话的 `jti`**（一次登录 = 一条链）。
  按用户级吊销（`sub` 维度）需要额外的用户态版本号，属独立需求，本任务不做。
* **下线即吊销依赖一次成功的请求**：`signOut` 是 fire-and-forget，
  如果用户在断网瞬间点退出，服务端那枚令牌会自然过期（≤30 分钟）。
  这是"登出必须立刻生效、不能把用户留在登录态"与"下线即吊销"之间的取舍，
  已写进 `AuthContext.signOut` 的注释。
* **多 worker 一致性**：本项目 SQLite 单库，黑名单落在共享库里，多 worker 天然一致。
  若将来换成进程内存储（或 Redis），这条要重新验 —— 逃生舱阈值见 ADR-004R。
* **`npm run lint` 仍然是红的**：实测 39 errors / 0 warnings（与 T-50 之后相同，
  本次没有新增）。
