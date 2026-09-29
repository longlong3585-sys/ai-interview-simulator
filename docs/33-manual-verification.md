# T-34 人工验收：API 基地址相对化 + Vite 代理 + `VITE_API_BASE_URL` 注入（Bug 4，P0）

> 对应任务：`docs/03-tasks.md` 的 **T-34**（**P0**，Bug 4）
> 上游：`docs/32-manual-verification.md`（T-48 / T-49）
> 下游关联：**T-35**（CORS 允许来源配置化）、**T-36**（api 层收敛）、
> **T-53 / T-54**（Nginx 反代 + 后端同源托管前端产物）
> 交付物（前端）：`src/config.ts`、`src/utils/apiBaseUrl.ts`（新增）、
> `vite.config.ts`、`.env.example`（新增）、`.gitignore`
> 交付物（契约/验收）：`frontend/tests/api-base-url.test.mjs`（新增）、
> `frontend/tests/api-parity.test.mjs`（解禁 config.ts）、
> `backend/scripts/verify_t34_manual.py`

## 1. 这次到底改了什么

修复前 `src/config.ts` 只有一行：

```ts
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';
```

这一行同时埋了三个必然的线上故障 —— 也就是说，**没有这一步，上云就是死局**：

| # | 故障 | 触发条件 | 后果 |
|---|---|---|---|
| ① | **混合内容（Mixed Content）** | 页面走 HTTPS | 浏览器直接拦掉所有 `http://` 请求，界面看起来正常但**每个按钮都点不动** |
| ② | **跨域（CORS）** | 前端换成真实域名 | 前端 origin 与那个写死的本机地址不同源，请求被浏览器挡下（后端即使配了 CORS 也救不了 ①） |
| ③ | **host 被内联进产物** | 任何一次构建 | 地址固化在 JS 里，换环境（测试/生产）必须重新构建前端 |

修复后：**基地址默认是空串 = 相对路径**，所有请求打到当前页面的 origin
（`/api/...`、`/uploads/...`），三个故障同时消失；需要跨源时才用构建期变量覆盖。

| 文件 | 作用 |
|---|---|
| `src/utils/apiBaseUrl.ts`（新增） | `normalizeApiBaseUrl()`：空值/空白 → `''`；末尾斜杠归一（否则拼出 `//api/...`）。**零依赖纯函数**，所以行为能被 Node 直接单测 |
| `src/config.ts` | `API_BASE_URL = normalizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL)` |
| `vite.config.ts` | `server.proxy` + `preview.proxy`：开发与预览期把 `/api`、`/uploads` 转发到后端（目标默认本机 8000，可用 `DEV_PROXY_TARGET` 覆盖，**读 `.env*` 也读 shell**） |
| `.env.example`（新增） | 环境变量模板：`VITE_API_BASE_URL` 留空 = 相对路径；并显式警告"HTTPS 页面不能填 `http://` 地址" |

两个刻意的细节：

* **连注释里都不写旧地址的字面量**。第一版把 `'http://127.0.0.1:8000'` 写在
  `config.ts` 的注释里做说明，离线打包模拟立刻抓到 —— 因为 **tsc 产物会保留注释**，
  `build.minify=false` 时 vite 产物也会。验收标准是"产物里 grep 不到"，不能靠压缩器兜底。
  注释改成"本机回环 + 8000 端口"这样的描述。
* **`preview.proxy` 与 `server.proxy` 一起配**。验收标准写的是"**预览**/HTTPS 环境下所有按钮可点"；
  只配 `server` 的话 `npm run preview`（4173）会把 `/api` 当成静态路径自己吞掉。

## 2. 傻瓜验证（一条命令，不用起服务、**不用手动复制 Token**、不花钱、不联网）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t34_manual.py
```

可选参数：`--skip-tsc`、`--skip-bundle`（跳过离线打包模拟）、`--with-build`（额外尝试 `vite build` 并扫 `dist/`）。
退出码：0 = 通过；1 = 未通过；2 = 环境问题。

| 段 | 证明 | 期望 |
|---|---|---|
| 0 | 预检：`node`、6 个目标文件、`node_modules` | 缺什么说什么 |
| A | **契约测试** `node --test`（TAP）：本任务 9 项 + 既有全部契约 | `tests=85 pass=85 fail=0`，5 个点名用例必须真出现过 |
| B | `tsc -b`（`src` + `vite.config.ts` 两个 project） | exit 0 |
| C | **本脚本自己重扫源码 + 自己编译一遍**：① 相对基地址 / ② Vite 代理（结构 + **运行时求值**）/ ③ 离线打包模拟 / ④ 代理目标不进产物 / ⑤ `.env` 加载语义 / ⑥ 结构性 Hook 规则 | 全 `[PASS]`，含 5 组判别力自检 |
| D | `vite build` 探测（默认跳过；真跑成功时**直接对 `dist/` 再 grep 一遍**） | 见 §4 |

### 最近一次实跑结果（真实输出）

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t34_manual.py
0. 预检        [PASS] node（v24.9.0）/ 6 个文件齐 / node_modules 在
A. 契约测试    [PASS] tests=85 pass=85 fail=0；点名用例 5/5 出现
B. 类型检查    [PASS] tsc -b 退出码 0
C. 独立复核    [PASS] ①~⑥ 全绿 + 5 组判别力自检
D. 构建探测    [SKIP] 默认不跑（沙箱 EPERM；离线版见 C-③）
汇总：[PASS] A / [PASS] B / [PASS] C / [SKIP] D
✅ T-34 人工验收通过
退出码 0
```

关键证据行（真实输出，非示意）：

```
[PASS] ① src 下 31 个 .ts/.tsx 的**代码**里，写死的 host 为 0
[PASS] ① 无 http:// 绝对地址字面量（页面走 HTTPS 时不存在混合内容）
[PASS] ① 判别力自检：修复前的 config.ts 被判为「写死 host」
[PASS] ② 运行时求值：server.proxy['/api'].target = http://127.0.0.1:8000
[PASS] ② 运行时求值：preview.proxy['/api'] 也配了（验收标准里的「预览环境可点」成立）
[PASS] ② 运行时求值：DEV_PROXY_TARGET=http://10.11.12.13:9000 时目标跟着变
[PASS] ③ 编译产物（31 个 .js + index.html）里 grep 不到 127.0.0.1 / localhost
[PASS] ③ 按 Vite 规则把 import.meta.env 替换成 .env 的值后，仍然 grep 不到 host
[PASS] ③ 判别力自检：替换规则确实生效（设了 VITE_API_BASE_URL 就会被内联 → 所以 0 命中是真结论）
[PASS] ⑤ .env.example 不在 Vite 的加载清单里 —— 它只是模板，示例地址不会进产物
```

### 只跑前端那一半（不需要后端）

```powershell
cd frontend
npm run test:node                        # 85 项契约测试（零依赖、不联网、毫秒级）
node node_modules/typescript/bin/tsc -b  # 类型检查，实测 exit 0
```

既有验收脚本**全部仍然通过**：

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t44_t45_manual.py   # 退出码 0
.\venv\Scripts\python.exe scripts\verify_t46_t49_manual.py   # 退出码 0
```

## 3. 契约测试怎么断言这件事

`frontend/tests/api-base-url.test.mjs`（9 项）分三层，缺一层都会留下"看起来改好了"的假象：

1. **纯逻辑**（真实行为，不是 grep）：`normalizeApiBaseUrl()` 对
   `undefined / null / '' / '   ' / '\n'` 一律返回 `''`；`…:8000/` 与 `…///` 被归一；
   并断言拼接不变式 `${base}/api/chat` **不会出现 `//api`**。
2. **接线**：`config.ts` 走 `VITE_API_BASE_URL` + 归一化、代码里无 host；
   `vite.config.ts` 有 `server.proxy` / `preview.proxy` / `/api` / `/uploads` /
   `changeOrigin` / `DEV_PROXY_TARGET` / `loadEnv`；`.env.example` 存在且示范"留空 = 相对路径"、
   警告混合内容。
3. **产物口径**（验收标准的可离线版本）：把所有会进浏览器产物的文件
   （`src/**`、`index.html`）去注释后 grep host = 0；
   并检查所有**会被 Vite 加载**的 `.env*`（`.env` / `.env.local` / `.env.[mode]`）里
   `VITE_*` 变量不带 host —— `.env.example` **不在**加载清单里，所以它可以示范地址。

两条**判别力自检**：把修复前的 `config.ts` 塞回产物文件集必须被判为违规；
"空的 vite 配置"必须被判为缺 `server.proxy`。

另外 `api-parity.test.mjs` 里那句"待 T-34 修复后再把 config.ts 纳入强制"**已经兑现**：
现在它扫描时不再豁免 `config.ts`。同时它改成**去注释**后再判定 —— 因为解释 Bug 4 的注释
会引用那条旧地址；不动这个口径的话，护栏会变成假警报（本轮就撞到一次：
注释里的 `${API_BASE_URL}/api/xxx` 被当成了一个真实接口路径）。

## 4. 已知边界（刻意如此，非遗漏）

* **不驱动真实浏览器**。"HTTPS 环境下按钮可点"是**源码 + 产物**证明的：
  基地址是相对路径 ⇒ 请求与页面同源 ⇒ 既无混合内容也无跨域；`preview.proxy` ⇒ 预览也有后端。
  肉眼验收（两条，都不用改代码）：
  1. 常规：`cd frontend; npm run dev`（**先起后端**）→ 登录、上传简历、问答、看报告 —— 全部可用；
  2. HTTPS：`npm run build; npm run preview`，再用任意 HTTPS 反代指向
     `http://localhost:4173`（nginx / caddy / `ngrok http 4173` 都行）→ 打开 `https://…`，
     按钮依然可点（页面内所有请求都是相对路径）。
* **`vite build` 在受限沙箱里以 `spawn EPERM` 失败**（要 spawn 走管道的子进程，
  沙箱禁止命名管道）：与 `docs/29`~`docs/32` 记录的是同一个沙箱边界。
  这正是 C-③ 存在的理由 —— 用 `tsc` 真编译出 JS，再按 Vite 的规则把
  `import.meta.env.X` 替换成 `.env` 里的值（模拟 define），然后 grep 产物；
  并且**反向验证**替换确实生效（设了 `VITE_API_BASE_URL` 就必须 grep 得到），
  避免"0 命中"只是因为替换没发生。`--with-build` 在不受限环境里会直接扫真实 `dist/`。
* **相对路径 = 生产必须同源托管**。这是本任务的**有意后果**：前端产物与后端必须挂在同一个
  origin（Nginx 反代 `/api` 与 `/uploads`，或由后端托管 `dist`）。
  相应地，`/admin` 的 history 回退也要一起做 —— 已登记给 **T-53 / T-54**
  （`docs/30` §4 早已记过这条）。如果将来真的需要前后端分域（CDN + 独立 API 域名），
  就设 `VITE_API_BASE_URL=https://api.example.com`，同时由 **T-35** 把 CORS 允许来源配置化。
* **`npm run lint` 仍然是红的**：42 errors / 0 warnings（与 T-48/T-49 之后相同，本次没有新增）；
  结构性 Hook 规则（`react-hooks/refs` / `immutability` / `purity`）在验收脚本里是**门槛**。
* **`e2e-timeout-live.mjs` 不受影响**：它自己接 `--base-url`，与前端基地址解耦。
