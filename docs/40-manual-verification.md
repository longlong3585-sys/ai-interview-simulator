# T-53 人工验收：Nginx 同源托管 + HTTPS + 前端 dist 上传方案（+ 升级 Runbook）

> 对应任务：`docs/03-tasks.md` 的 **T-53**（依赖 T-34）
> 上游：`docs/archive/34-manual-verification.md`（T-34 把 API 基地址改成相对路径，
> 因此**生产必须同源托管** —— 那正是本任务要落地的部署形态）
> 交付物（配置）：`deploy/nginx/ai-interview.conf`、`ai-interview-ssl.conf`、
> `acme-challenge.conf`、`security-headers.conf`、`html-no-cache.conf`、
> `assets-cache.conf`、`README.md`
> 交付物（测试）：`backend/tests/test_nginx_config.py`（新增 38 项）
> 交付物（手册）：`docs/39-aliyun-upgrade-runbook.md`（本次核心交付物）
> 交付物（验收）：`backend/scripts/verify_t53_manual.py`（离线一键）

## 1. 这次到底做了什么

T-34 把前端 API 基地址改成了**相对路径**（`API_BASE_URL = ''`），于是
"页面与接口必须同源"从偏好变成了硬约束。T-53 把这条约束落到 Nginx 上：

| 要求 | 落点 |
|---|---|
| 托管 `dist/` | `root` 指向 `frontend/dist`；入口 HTML **禁缓存**、带 hash 的 `assets/` **永久缓存** |
| 同源反代 `/api` | `proxy_pass http://ai_interview_backend;`（**不带结尾 `/`**，否则 `/api` 前缀被剥掉 → 全线 404） |
| 同源反代 `/uploads` | 用 `^~` 前缀 + 反代（不 alias）；见下面第 3 条 |
| `/admin` history 回退 | `location = /admin` + `location ^~ /admin/` → `try_files /index.html`（**返回 200**，不是 301） |
| `client_max_body_size 8m` | 保证 >5MB 简历能**到达应用**，由应用返回 **400**（不是 Nginx 的 413） |
| XFF 转发 | `$proxy_add_x_forwarded_for` + `X-Forwarded-Proto` + `X-Forwarded-Host` |
| HTTPS / certbot | 独立 `ai-interview-ssl.conf`（443 业务 + 80 仅 ACME + 301 跳转），TLS 1.2/1.3、OCSP stapling、HSTS 初始 30 天 |

另外补了两件 ADR 没写、但不做就会出事的事：

* **安全响应头**（CSP / nosniff / X-Frame-Options / Referrer-Policy）——
  并且做成 include 片段，因为 nginx 的 `add_header` **不叠加**；
* **入口 HTML 禁缓存**与 `assets/` 长缓存的区分 —— 混在一起会造成
  "发了新版但用户还在跑旧版"。

## 2. 傻瓜验证（一条命令，离线、不起服务、不改文件）

```powershell
cd backend
.\venv\Scripts\python.exe scripts\verify_t53_manual.py
```

可选参数：`--skip-full-tests`（跳过约 100 秒的全量测试）。
退出码：0 = 通过；1 = 未通过；2 = 环境问题。

| 段 | 证明 | 期望 |
|---|---|---|
| A | 11 个交付物存在；两份站点配置只有 `YOUR_DOMAIN`/`APP_DIR` 两个占位符，**没有写死 IP** | 全 PASS |
| B | ① 新增 nginx 测试 38 项；② **全量** 709 项 | 都 `OK` |
| C | **独立复核**：用 nginx 的 location 优先级规则算四个关键 URL 落到哪一条 + 关键指令断言 | 全 PASS |
| D | **Runbook 审计**：六项指定内容 + 顺序安全（备份在 `git reset` 之前）+ 禁止服务器构建 + 预期输出数量 | 全 PASS |

### 最近一次实跑结果（真实输出）

```
$ cd backend; .\venv\Scripts\python.exe scripts\verify_t53_manual.py

A 段 · 交付物与占位符卫生                     [PASS]
B 段 · 自动化测试                            [PASS]  新增 38 项 OK；全量 709 项 OK
C 段 · 独立复核（选路 + 关键指令）             [PASS]
  /admin -> `location = /admin` + try_files（返回 200，不是 301/404）
  /uploads/avatars/user_1_deadbeef.jpg -> `location ^~ /uploads/`（没被静态文件正则抢走）
  /api/interview/config -> /api 反代且不剥前缀；XFF 用 $proxy_add_x_forwarded_for
  client_max_body_size=8m ⇒ >5MB 的简历能到达应用（由应用返回 400，不是 413）
  每个写了 add_header 的 location 都 include 了安全头
  最终形态里 http:// 会 301 跳 https://
  续期请求落在 `^~ /.well-known/acme-challenge/`，不会被 301 吃掉
D 段 · Runbook 审计                          [PASS]
  六项指定内容 ✓；备份在 git reset 之前；迁移后紧邻回滚小节；
  38 处「预期输出」、14 处「不通过怎么办」；无 TODO 残留

汇总：A PASS / B PASS / C PASS / D PASS
✅ T-53 人工验收通过
退出码 0
```

## 3. 三个"静默失效"级的坑（都已在本地钉住）

### 3.1 `/uploads/xxx.jpg` 会被静态文件正则抢走（**本仓库真实存在过的缺陷**）

nginx 的选路规则里，**正则优先于普通前缀**。所以 `/uploads/avatars/x.jpg`
这条 URL：

1. 命中 `location /uploads/`（普通前缀）—— 但这是**候选**，不是结论；
2. 同时命中 `location ~* \.(jpg|png|...)$`（正则）；
3. **正则胜出** ⇒ nginx 去 `root`（= `dist` 目录）找这个 jpg ⇒ **404**，
   且报错只说"文件不存在"，看起来像后端没存上。

修法是 `location ^~ /uploads/`（`^~` 前缀优先级高于正则）。
这条是写完配置后用"选路规则复核"跑出来的，`tests/test_nginx_config.py::
test_uploads_subpath_matches_the_uploads_rule` 现在钉着它。

### 3.2 `add_header` 不叠加，只替换

在 `location /` 里写一句 `Cache-Control: no-store`，会让 server 级的
CSP / nosniff / X-Frame-Options / HSTS **全部消失** —— 页面照常打开，
只有安全扫描或真被 iframe 嵌套时才暴露。

因此安全头被做成 `security-headers.conf` 片段，在每个 location 里
**显式 include**；`test_every_location_with_add_header_also_includes_security_headers`
盯着它。

### 3.3 `http2` 参数在新版 nginx 上会启动失败

`listen 443 ssl http2;` 在 nginx **≥ 1.25.1** 被弃用（改成独立的 `http2 on;`）。
配置里**刻意不写 `http2` 参数** —— 任何版本都能启动，代价只是没有 HTTP/2。
`deploy/nginx/README.md` 与 Runbook §7.3 都写了按 `nginx -v` 选择的办法。

## 4. 必须留到阿里云上做的事（如实声明）

本机的 Windows 环境**没有 nginx、没有域名、没有公网**，因此以下四项
只能在服务器上完成 —— Runbook 里每一步都给了命令与预期输出：

1. **`nginx -t` 语法校验**：本脚本证明的是**语义**正确（该有的指令都在、
   该赢的 location 会赢），不能证明语法能被 nginx 解析。
   所以 Runbook 里所有改配置的步骤都强制"**先 `-t` 再 `reload`**"。
2. **certbot 真签发**：需要真实域名 + 80 端口公网可达。
   Runbook §7 给了完整流程，含 staging 试签、失败原因对照表、
   以及"**80 端口必须保留 ACME 目录**"（否则 90 天后证书静默过期）。
3. **浏览器 §9.2 的 7 项人工验收**：白屏、混合内容、`/admin` 刷新、
   验证码显示这些只有真浏览器能证。
4. **上传 >5MB 返回 400（不是 413）**：命令行能验到"请求到达了应用"
   （401/403），要拿到那条 400 的 `detail` 需要登录后再传。

## 5. Runbook 的形态（为什么它不是"一堆命令"）

`docs/39-aliyun-upgrade-runbook.md` 按用户要求写成**可逐行复制执行**的手册：

* 11 个章节，**38 处「预期输出」**、**14 处「不通过怎么办」**；
* 每章开头重新声明变量（`APP` / `DOMAIN` / `STAMP` / `APP_USER`），
  因为运维会不断新开终端；
* 唯一的不可逆动作（数据库迁移）**前后各有一个校验小节**，
  失败时的回滚步骤紧跟在后面（§4.5），不需要翻文档；
* 备份（§2.2）**在** `git reset --hard`（§2.5）**之前**，且 §2.3 强制验证
  备份可用才允许继续 —— 这条顺序由 `verify_t53_manual.py` 的 D-④ 自动审计；
* **明令禁止在服务器上 `npm run build`**（2G 内存会被 OOM Killer 打爆），
  只给"本地 Windows 构建 → rsync/scp 上传 dist"的路线，
  并用 D-③ 自动检查这条禁令没有被后续编辑破坏；
* 回滚锚点用 `STAMP` 贯穿全文 + `git_head_before.txt` 记录升级前的 commit，
  §10.5 给出整体回滚的完整序列。
