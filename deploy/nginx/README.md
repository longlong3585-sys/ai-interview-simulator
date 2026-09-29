# Nginx 同源托管 · 部署说明（T-53）

> **开发环境是 Windows**，本目录的文件只在 Linux 服务器上部署时使用。
> 逐步的线上操作手册在 **`docs/39-aliyun-upgrade-runbook.md`**；本文件讲
> "每个文件是什么、为什么这么写"。

## 为什么必须同源托管

T-34（Bug 4，P0）把前端 API 基地址改成了**相对路径**（`API_BASE_URL = ''`）。
相对路径只有在"页面与接口同源"时才成立 —— 于是生产环境的托管方式被钉死为：
**Nginx 托管 `frontend/dist` + 同源反代 `/api` 与 `/uploads`**。
不用 CDN、不用独立 API 域名（`docs/03-tasks.md` 的部署拓扑决定）。

直接后果：`/admin` 用 `BrowserRouter`，刷新时浏览器会**真的请求 `/admin`**
这个路径，所以必须有 SPA history 回退，否则刷新即 404。

## 文件清单

| 文件 | 装到哪里 | 作用 |
|---|---|---|
| `ai-interview.conf` | `/etc/nginx/sites-available/ai-interview` | **第一步**：80 端口跑通（前端 + `/api` + `/uploads` + ACME） |
| `ai-interview-ssl.conf` | `/etc/nginx/sites-available/ai-interview-ssl` | **第二步**：443 业务站点 + 80 端口 301 跳转（最终形态） |
| `acme-challenge.conf` | `/etc/nginx/snippets/acme-challenge.conf` | Let's Encrypt 校验目录（**80 端口必须保留**） |
| `security-headers.conf` | `/etc/nginx/snippets/security-headers.conf` | CSP / nosniff / X-Frame-Options / Referrer-Policy |
| `html-no-cache.conf` | `/etc/nginx/snippets/html-no-cache.conf` | 入口 HTML 禁缓存 |
| `assets-cache.conf` | `/etc/nginx/snippets/assets-cache.conf` | 带 hash 的产物长缓存 |

**`ai-interview.conf` 与 `ai-interview-ssl.conf` 互斥**（同一时刻只启用一个）。
两份都有 `upstream ai_interview_backend`，同时启用会因为
`duplicate upstream` 让 nginx 起不来 —— 所以 Runbook 里切 HTTPS 的那一步
是"删旧链接 + 建新链接"，不是"再加一个"。

> ⚠️ 两份配置里的**代理头与缓存策略是逐字重复的**（互斥启用，无法共享）。
> 改任何一条都要**同时改两个文件**，否则"回滚到 HTTP"或"切到 HTTPS"后行为不一致。

## 关键配置逐条解释

### `client_max_body_size 8m` —— 为了返回 **400 而不是 413**

T-53 验收标准原文：**"上传 >5MB 简历返回 400（不是 413）"**。

应用侧判据在 `routers/interview.py`：`MAX_FILE_SIZE = 5MB` → `HTTPException(400)`。
而 Nginx 默认 `client_max_body_size` 只有 **1m**，会在请求到达应用**之前**
就掐断并返回 **413** —— 用户看到的是网关错误，不是"文件不能超过 5MB"。

设 **8m**：既让 5MB 的边界值进得到应用（由应用回 400），
又仍然挡住真正的超大请求（8MB 以上直接 413）。8 而不是 6 是为了给
multipart 边框与文件名留余量。

### `/uploads/` 用 `^~` 且**反代**而不是 alias

两条独立的理由：

1. **必须 `^~`**：nginx 的选路规则里**正则优先于普通前缀**。头像 URL
   `/uploads/avatars/user_1_ab.jpg` 会命中下面那条
   `location ~* \.(jpg|png|...)$` 静态文件正则 —— nginx 于是去 `root`
   （= dist 目录）里找这个 jpg，找不到就 404。这条是
   `tests/test_nginx_config.py` 抓出来的真实缺陷。
2. **反代而不是 `alias`**：后端自己挂载了
   `app.mount("/uploads", StaticFiles(directory="uploads"))`（`main.py:51`），
   而它的目录是**相对路径** `uploads/avatars` —— 到底是哪个目录取决于
   uvicorn 的 cwd。用 alias 就等于引入"第二个真相"（Nginx 读哪个目录），
   一旦与应用的 cwd 不一致，就会出现"能上传、看不到"。
   代价只是静态文件走后端进程（头像 ≤2MB、QPS 极低，可接受）。

### 每个写了 `add_header` 的 location 都要 `include` 安全头

nginx 有一条反直觉规则：**子级 location 只要写了任何一条 `add_header`，
父级/server 级的 `add_header` 就全部失效**（是替换，不是叠加）。

所以"在 server 块里写一遍 CSP 就全站生效"是错的：
`location /` 里那句 `Cache-Control: no-store` 会让 CSP、`nosniff`、
`X-Frame-Options` **静默消失** —— 页面照常打开，只有安全扫描或真被
iframe 嵌套时才暴露。因此本目录把安全头做成片段，在每个 location 里
**显式 include**；`tests/test_nginx_config.py` 有一条用例专门盯着
"写了 `add_header` 却没 include 安全头"的 location。

### `/admin` 回退必须是 `try_files`（200），**不能是 3xx**

```nginx
location = /admin   { try_files /index.html =404; }   # ✅ 内部重写，返回 200
location ^~ /admin/ { try_files /index.html =404; }   # ✅ 覆盖将来的子路由
```

写成 `return 301 /` 会让浏览器地址栏跳到 `/`，`/admin` **永远进不去**
（而且没有任何报错）。`^~` 是为了让 `/admin/xxx` 这种子路由不被正则抢走 ——
将来往 `/admin` 下加子路由时不必再动 Nginx。

### `X-Forwarded-For` 用 `$proxy_add_x_forwarded_for`

后端靠 XFF 的**最右**一段取真实客户端 IP（T-21，`TRUSTED_PROXY_COUNT` 默认 1）。
`$proxy_add_x_forwarded_for` 会把客户端伪造的值留在左边、把真实 IP 追加到最右，
后端只信最右 ⇒ 伪造无效。

**不要**改成 `$remote_addr`（丢掉中间代理链），也**不要**用
`$http_x_forwarded_for`（那就是可伪造的原始头 —— 等于把限流键交给攻击者，
`docs/02-architecture.md` ADR-016 明确点名了这个风险）。

### TLS 与 HTTP/2 的版本差异

Ubuntu 22.04 自带 nginx **1.18.0**，支持 `listen 443 ssl http2` 合并写法。
nginx **≥ 1.25.1** 弃用了这个参数，要在 server 块里单独写 `http2 on;`。

本仓库的 `ai-interview-ssl.conf` **刻意不写 `http2` 参数**，这样任何版本都能
直接启动；代价只是没有 HTTP/2。想打开的话按下面判断：

```bash
nginx -v 2>&1                    # 看版本号
# 1.18 之类（< 1.25.1）：把 `listen 443 ssl;` 改成 `listen 443 ssl http2;`
# 1.25.1 及以上：保留 `listen 443 ssl;`，在 server 块里加一行 `http2 on;`
```

### HSTS 初始只给 30 天

浏览器一旦记住 HSTS，证书出问题期间站点会**彻底打不开**（连"继续访问"
都没有）。所以初始 `max-age=2592000`（30 天）、**不开 `preload`**；
观察一轮 certbot 自动续期正常后再逐步加到 `31536000`。

## 本地怎么验证（Windows，无 nginx）

```powershell
cd backend
.\venv\Scripts\python.exe -m unittest tests.test_nginx_config -v
```

这 38 项覆盖：`client_max_body_size`、`/admin` 回退写法、`proxy_pass` 是否
改写前缀、XFF 头、`add_header` 继承陷阱、占位符卫生、HSTS 长度、
**以及用 nginx 的选路规则复核"某个 URL 到底落到哪个 location"**
（`_select_location()` 按官方优先级实现）。

它**不能**替代 `nginx -t`：语法错误、指令拼写、include 路径是否存在，
只有真 nginx 才知道。所以 Runbook 里每一步都强制先 `nginx -t` 再 `reload`。
