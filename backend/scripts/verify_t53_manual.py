#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T-53 一键人工验收：Nginx 同源托管 + HTTPS + 前端 dist 上传方案 + 升级 Runbook。

一条命令，**不起服务、不联网、不改任何文件**：

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t53_manual.py

可选参数：
    --skip-full-tests   跳过 B-② 的全量后端测试（默认跑，约 100 秒）

退出码：0 = 通过；1 = 未通过；2 = 环境问题。

────────────────────────────────────────────────────────────────────────────
验收标准（`docs/03-tasks.md` §阶段 5 的 T-53）：

  * 上传 >5MB 简历返回 **400**（**不是 413**）；
  * 后端能拿到**真实客户端 IP**；
  * `http://` **301** 跳 `https://`。

四段互不依赖的取证：

  A. 交付物齐全 —— 6 个 nginx 配置 + `docs/39-aliyun-upgrade-runbook.md`
                   + 本脚本都在，且**部署件里没有写死的域名/IP/路径**
                   （只有 `YOUR_DOMAIN` / `APP_DIR` 两个占位符）。
  B. 自动化测试 —— ① 本任务的 38 项 nginx 静态测试全绿；② **全量**后端测试全绿
                   （证明新测试没有打坏既有用例）。
  C. 独立复核     —— **本脚本自己重跑一遍选路规则与配置断言**，不用测试里的断言：
                   ① 用 nginx 的 location 优先级规则算 `/admin`、`/uploads/*.jpg`、
                      `/api/*`、`/.well-known/acme-challenge/*` 各自落到哪一条 ——
                      这四问对应的正是四个"静默失效"级坑；
                   ② `client_max_body_size` 确实 ≥8m（否则 >5MB 会变成 413）；
                   ③ `/admin` 回退是 `try_files`（200）而不是 `return 301`；
                   ④ 每个含 `add_header` 的 location 都 include 了安全头
                      （nginx 的 add_header 不叠加，漏了就静默丢 CSP）；
                   ⑤ http → 301 → https 的指令存在；
                   ⑥ 续期请求不会被 301 吃掉。
  D. Runbook 审计 —— 手册是本次的核心交付物，因此**当作代码来测**：
                   ① 六项指定内容都在（强制拉取 / 依赖 / 迁移 + 回滚 / 本地构建
                      + scp/rsync / Nginx + certbot / systemd）；
                   ② **禁止**手册让运维在服务器上跑 `npm run build`
                      （2G 内存会被打爆）—— 只允许出现在"本地 Windows"小节里；
                   ③ 备份步骤在 `git reset --hard` **之前**（顺序错了会丢数据）；
                   ④ 每个关键小节都写了"预期输出"与"不通过怎么办"。

为什么 C-① 值得单独做：`/uploads/avatars/x.jpg` 与 `/admin` 这两条 URL
分别是"被正则抢走"和"被 301 吃掉"的经典受害者，而两者都**不会报错** ——
前者 404 且只说"文件不存在"，后者地址栏跳走。靠读配置很难发现，
用规则算一遍就能立刻发现。这条复核在本仓库真的抓到过一个缺陷（见
`deploy/nginx/README.md` 的 `/uploads/` 段）。

已知边界（刻意如此，非遗漏）：
  * **不跑真 nginx**：开发机是 Windows。本脚本证明"配置语义正确"，
    不证明"语法能被 nginx 解析" —— 后者只有 `nginx -t` 能回答，
    因此 Runbook 每一步都强制"先 -t 再 reload"。
  * **不签发真证书**：certbot 需要真实域名与公网可达，只能在服务器上做。
  * **不做浏览器验收**：§9.2 的 7 项人工清单必须人手点（混合内容、白屏、
    /admin 刷新这些只有浏览器能证）。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(_HERE))
BACKEND_DIR = os.path.join(REPO_DIR, "backend")
NGINX_DIR = os.path.join(REPO_DIR, "deploy", "nginx")
VENV_PYTHON = os.path.join(BACKEND_DIR, "venv", "Scripts", "python.exe")
RUNBOOK = os.path.join(REPO_DIR, "docs", "39-aliyun-upgrade-runbook.md")

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}


# ---------------------------------------------------------------------------
# 输出小工具
# ---------------------------------------------------------------------------

def _make_stdout_forgiving():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def say(msg=""):
    print(msg)


def section(title):
    say("")
    say("=" * 78)
    say(title)
    say("=" * 78)


def ok(msg):
    say("  [PASS] %s" % msg)


def bad(msg):
    FAILED.append(msg)
    say("  [FAIL] %s" % msg)


def skip(msg):
    SKIPPED.append(msg)
    say("  [SKIP] %s" % msg)


def env_problem(msg):
    ENV_PROBLEMS.append(msg)
    say("  [ENV ] %s" % msg)


def run(cmd, cwd=BACKEND_DIR, timeout=1800):
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
    except FileNotFoundError as exc:
        return None, "命令不存在：%s" % exc
    except subprocess.TimeoutExpired:
        return None, "超时（%ss）" % timeout
    except OSError as exc:
        return None, "无法启动进程：%s" % exc
    return proc.returncode, (proc.stdout.decode("utf-8", errors="replace")
                             if proc.stdout else "")


def tail(text, n=5):
    return [ln for ln in (text or "").splitlines() if ln.strip()][-n:]


def parse_unittest(out):
    m = re.search(r"^Ran (\d+) tests?", out or "", re.M)
    if not m:
        return None, False
    return int(m.group(1)), bool(re.search(r"^OK", out or "", re.M))


def python_exe():
    return VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def strip_comments(text):
    """去掉整行注释（配置里大量注释在解释这些坑，不能当成断言依据）。"""
    out = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line.split("#", 1)[0] if "#" in line else line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# nginx location 选路（与 tests/test_nginx_config.py 同一套规则，独立实现一份）
# ---------------------------------------------------------------------------

SNIPPET_MAP = {
    "/etc/nginx/snippets/acme-challenge.conf": "acme-challenge.conf",
    "/etc/nginx/snippets/security-headers.conf": "security-headers.conf",
    "/etc/nginx/snippets/html-no-cache.conf": "html-no-cache.conf",
    "/etc/nginx/snippets/assets-cache.conf": "assets-cache.conf",
}


def expand_includes(text):
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("include "):
            target = stripped[len("include "):].rstrip(";").strip()
            name = SNIPPET_MAP.get(target)
            if name:
                path = os.path.join(NGINX_DIR, name)
                if os.path.isfile(path):
                    out.append("# >>> include %s" % target)
                    out.extend(read(path).splitlines())
                    out.append("# <<< include %s" % target)
                    continue
        out.append(line)
    return "\n".join(out)


def parse_servers(text):
    servers, depth, current = [], 0, None
    for line in text.splitlines():
        bare = line.split("#", 1)[0]
        if re.search(r"\bserver\s*\{", bare) and depth == 0:
            current, depth = {"lines": []}, 1
            continue
        if current is not None:
            depth += bare.count("{") - bare.count("}")
            if depth <= 0:
                servers.append(current)
                current, depth = None, 0
                continue
            current["lines"].append(bare)
    return servers


def parse_locations(server_lines):
    locations, depth, current = [], 0, None
    for bare in server_lines:
        m = re.match(r"\s*location\s+(=|\^~|~\*|~)?\s*(\S+)\s*\{", bare)
        if m and depth == 0:
            current = {"modifier": m.group(1) or "", "pattern": m.group(2), "body": []}
            depth = 1
            continue
        if current is not None:
            depth += bare.count("{") - bare.count("}")
            if depth <= 0:
                locations.append(current)
                current, depth = None, 0
                continue
            current["body"].append(bare)
    return locations


def select_location(locations, uri):
    """nginx location 优先级：① `=` 精确 ② `^~` 最长前缀 ③ 正则（按出现序）
    ④ 普通前缀（最长）。"""
    for loc in locations:
        if loc["modifier"] == "=" and loc["pattern"] == uri:
            return loc
    caret = [l for l in locations
             if l["modifier"] == "^~" and uri.startswith(l["pattern"])]
    if caret:
        return max(caret, key=lambda l: len(l["pattern"]))
    for loc in locations:
        if loc["modifier"] in ("~", "~*"):
            flags = re.IGNORECASE if loc["modifier"] == "~*" else 0
            if re.search(loc["pattern"], uri, flags):
                return loc
    plain = [l for l in locations
             if l["modifier"] == "" and uri.startswith(l["pattern"])]
    if plain:
        return max(plain, key=lambda l: len(l["pattern"]))
    return None


# ---------------------------------------------------------------------------
# A 段：交付物
# ---------------------------------------------------------------------------

EXPECTED = {
    "HTTP 站点配置": os.path.join("deploy", "nginx", "ai-interview.conf"),
    "HTTPS 站点配置": os.path.join("deploy", "nginx", "ai-interview-ssl.conf"),
    "ACME 片段": os.path.join("deploy", "nginx", "acme-challenge.conf"),
    "安全头片段": os.path.join("deploy", "nginx", "security-headers.conf"),
    "HTML 禁缓存片段": os.path.join("deploy", "nginx", "html-no-cache.conf"),
    "静态产物缓存片段": os.path.join("deploy", "nginx", "assets-cache.conf"),
    "Nginx 部署说明": os.path.join("deploy", "nginx", "README.md"),
    "升级 Runbook": os.path.join("docs", "39-aliyun-upgrade-runbook.md"),
    "升级验收脚本": os.path.join("backend", "scripts", "verify_t53_manual.py"),
    "Nginx 测试": os.path.join("backend", "tests", "test_nginx_config.py"),
    "后端 systemd 单元（Runbook 里提供）": os.path.join("deploy", "systemd", "backup.service"),
}


def segment_artifacts():
    section("A 段 · 交付物与占位符卫生（静态，可离线）")
    failed_before = len(FAILED)

    for label, rel in EXPECTED.items():
        if os.path.isfile(os.path.join(REPO_DIR, rel)):
            ok("%s 存在：%s" % (label, rel))
        else:
            bad("%s 缺失：%s" % (label, rel))

    for name in ("ai-interview.conf", "ai-interview-ssl.conf"):
        text = strip_comments(read(os.path.join(NGINX_DIR, name)))
        for placeholder in ("YOUR_DOMAIN", "APP_DIR"):
            if placeholder not in text:
                bad("%s 缺少占位符 %s（部署时无法用 sed 统一替换）" % (name, placeholder))
        if "112.124.54.223" in text:
            bad("%s 里写死了线上 IP —— 别人拿去部署会踩坑" % name)
        else:
            ok("%s 没有写死域名/IP（只有占位符）" % name)

    SEG["A"] = len(FAILED) == failed_before


# ---------------------------------------------------------------------------
# B 段：测试
# ---------------------------------------------------------------------------

def segment_tests(args):
    section("B 段 · 自动化测试")
    py = python_exe()

    say("B-① 本任务新增的 nginx 静态测试（38 项）")
    code, out = run([py, "-m", "unittest", "-v", "tests.test_nginx_config"])
    say("  —— 尾部 5 行 ——")
    for line in tail(out, 5):
        say("     %s" % line)
    count, green = parse_unittest(out)
    if code == 0 and green:
        ok("nginx 配置测试全绿（%s 项）" % (count if count is not None else "?"))
        SEG["B1"] = True
    else:
        bad("nginx 配置测试未全绿（退出码 %s）" % code)
        SEG["B1"] = False

    if args.skip_full_tests:
        skip("B-② 全量后端测试（--skip-full-tests）")
        SEG["B2"] = None
    else:
        say("")
        say("B-② 全量后端测试（约 100 秒）")
        code, out = run([py, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                        timeout=1800)
        say("  —— 尾部 5 行 ——")
        for line in tail(out, 5):
            say("     %s" % line)
        count, green = parse_unittest(out)
        if code == 0 and green:
            ok("全量后端测试全绿（%s 项）" % (count if count is not None else "?"))
            SEG["B2"] = True
        else:
            bad("全量后端测试未全绿（退出码 %s）" % code)
            SEG["B2"] = False

    SEG["B"] = all(v is not False for v in (SEG.get("B1"), SEG.get("B2")))


# ---------------------------------------------------------------------------
# C 段：独立复核（选路 + 关键指令）
# ---------------------------------------------------------------------------

def segment_independent():
    section("C 段 · 独立复核（用 nginx 选路规则算一遍四个关键 URL）")
    failed_before = len(FAILED)

    http_text = strip_comments(expand_includes(read(os.path.join(NGINX_DIR, "ai-interview.conf"))))
    raw_http = strip_comments(read(os.path.join(NGINX_DIR, "ai-interview.conf")))
    ssl_text = strip_comments(expand_includes(read(os.path.join(NGINX_DIR, "ai-interview-ssl.conf"))))

    locations = parse_locations(parse_servers(http_text)[0]["lines"])

    # ① /admin 必须落在 = /admin，且用 try_files（200）
    loc = select_location(locations, "/admin")
    if loc and loc["pattern"] == "/admin" and loc["modifier"] == "=":
        body = "\n".join(loc["body"])
        if "try_files /index.html" in body and "return 30" not in body:
            ok("/admin -> `location = /admin` + try_files（返回 200，不是 301/404）")
        else:
            bad("/admin 落对了 location，但回退写法不对（必须 try_files，不能 3xx）")
    else:
        bad("/admin 落到了 %r，不是精确匹配的 /admin" % (loc and loc["pattern"]))

    # ② 头像 URL 必须落在 ^~ /uploads/（不能被静态文件正则抢走）
    avatar = "/uploads/avatars/user_1_deadbeef.jpg"
    loc = select_location(locations, avatar)
    if loc and loc["pattern"] == "/uploads/" and loc["modifier"] == "^~":
        ok("%s -> `location ^~ /uploads/`（没被静态文件正则抢走）" % avatar)
    else:
        bad("%s 落到了 %r（modifier=%r）—— 正则优先于普通前缀，头像会 404"
            % (avatar, loc and loc["pattern"], loc and loc["modifier"]))

    # ③ /api 前缀必须原样传给后端
    loc = select_location(locations, "/api/interview/config")
    if loc and loc["pattern"] == "/api":
        body = "\n".join(loc["body"])
        m = re.search(r"proxy_pass\s+(\S+);", body)
        if m and not m.group(1).endswith("/"):
            ok("/api/interview/config -> /api 反代且不剥前缀（%s）" % m.group(1))
        else:
            bad("proxy_pass 结尾带 `/` 会剥掉 /api 前缀 → 全线 404：%r" % (m and m.group(1)))
        if "X-Forwarded-For   $proxy_add_x_forwarded_for;" in body:
            ok("X-Forwarded-For 用 $proxy_add_x_forwarded_for（真实 IP 追加在最右）")
        else:
            bad("/api 没有正确转发 X-Forwarded-For —— 后端拿到的会是 127.0.0.1，"
                "一人连错 5 次锁死全员")
        if "X-Forwarded-Proto $scheme;" in body:
            ok("X-Forwarded-Proto 已转发（应用能判断安全上下文）")
        else:
            bad("/api 没有转发 X-Forwarded-Proto")
    else:
        bad("/api/interview/config 落到了 %r" % (loc and loc["pattern"]))

    # ④ 上传体积：必须 ≥8m
    m = re.search(r"client_max_body_size\s+(\d+)m;", raw_http)
    if m and int(m.group(1)) >= 8:
        ok("client_max_body_size=%sm ⇒ >5MB 的简历能到达应用（由应用返回 400，不是 413）"
           % m.group(1))
    elif m:
        bad("client_max_body_size=%sm 太小，>5MB 会被 Nginx 掐成 413" % m.group(1))
    else:
        bad("没有配置 client_max_body_size（默认 1m ⇒ 返回 413，验收标准不达标）")

    # ⑤ add_header 继承陷阱
    offenders = [l["pattern"] for l in parse_locations(parse_servers(raw_http)[0]["lines"])
                 if "add_header" in "\n".join(l["body"])
                 and "security-headers.conf" not in "\n".join(l["body"])]
    if not offenders:
        ok("每个写了 add_header 的 location 都 include 了安全头（CSP 不会被静默顶掉）")
    else:
        bad("这些 location 写了 add_header 却没 include 安全头，CSP/nosniff 会消失：%s"
            % offenders)

    # ⑥ http → 301 → https，且续期不被吃掉
    if "return 301 https://$host$request_uri;" in ssl_text:
        ok("最终形态里 http:// 会 301 跳 https://（验收标准第 3 条）")
    else:
        bad("HTTPS 配置里没有 `return 301 https://$host$request_uri;`")

    servers = parse_servers(ssl_text)
    if len(servers) >= 2:
        loc = select_location(parse_locations(servers[1]["lines"]),
                              "/.well-known/acme-challenge/token123")
        if loc and loc["modifier"] == "^~" and loc["pattern"] != "/":
            ok("续期请求落在 `^~ /.well-known/acme-challenge/`，不会被 301 吃掉"
               "（否则证书 90 天后静默过期）")
        else:
            bad("续期请求落到了 %r —— certbot 续期会被 301 挡住"
                % (loc and loc["pattern"]))
    else:
        bad("HTTPS 配置里缺少 80 端口那个 server 块")

    # ⑦ 占位符一致性（两文件必须同一个 server_name 占位符，否则 sed 只换一个）
    for name in ("ai-interview.conf", "ai-interview-ssl.conf"):
        if "server_name YOUR_DOMAIN;" not in read(os.path.join(NGINX_DIR, name)):
            bad("%s 的 server_name 不是统一的 YOUR_DOMAIN 占位符" % name)
    ok("两份站点配置用同一个 server_name 占位符（一次 sed 全换）")

    SEG["C"] = len(FAILED) == failed_before


# ---------------------------------------------------------------------------
# D 段：Runbook 审计（把手册当代码测）
# ---------------------------------------------------------------------------

REQUIRED_SNIPPETS = {
    "强制拉取新代码": "git fetch --all && git reset --hard origin/main",
    "备份数据库（覆盖前）": "interview.db.bak-",
    "备份上传物（覆盖前）": "uploads.tar.gz",
    "校验备份可用": "PRAGMA integrity_check",
    "安装 Python 依赖": "pip install -r requirements.txt",
    "执行迁移": "scripts/migrate.py",
    "迁移回滚（.bak 恢复）": "回滚",
    "本地构建前端": "npm run build",
    "上传 dist（rsync）": "rsync -avz --delete dist/",
    "上传 dist（scp 备选）": "scp dist.tar.gz",
    "Nginx 安装与启用": "sites-available/ai-interview",
    "Nginx 语法检查": "nginx -t",
    "certbot 签发": "certbot certonly --webroot",
    "certbot 续期演练": "certbot renew --dry-run",
    "systemd 守护进程": "/etc/systemd/system/ai-interview.service",
    "开机自启": "systemctl enable ai-interview",
    "崩溃自动重启": "Restart=always",
    "重启命令": "systemctl restart ai-interview",
    "SPA /admin 回退验收": "/admin",
    "上传 400（不是 413）": "413",
    "真实客户端 IP": "X-Forwarded-For",
}

#: 只在"本地 Windows"语境下允许出现的命令（服务器上跑会把 2G 内存打爆）
SERVER_FORBIDDEN = ["npm run build", "npm ci", "vite build"]


def _section_ranges(text):
    """把文档按 `## N.` 一级小节切段，返回 [(标题, 正文)]。"""
    parts = re.split(r"(?m)^## ", text)
    out = []
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        out.append((title.strip(), body))
    return out


def _section_start_lines(text):
    """返回 [(行号, 标题)]：`## ` 一级小节的起始行（1-based）。"""
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            out.append((i, line[3:].strip()))
    return out


def segment_runbook():
    section("D 段 · Runbook 审计（把手册当代码测）")
    failed_before = len(FAILED)

    if not os.path.isfile(RUNBOOK):
        bad("Runbook 不存在：%s" % RUNBOOK)
        SEG["D"] = False
        return
    text = read(RUNBOOK)
    sections = _section_ranges(text)
    section_starts = _section_start_lines(text)

    def section_title_at(line_no):
        """line_no（1-based）落在哪个一级小节里。"""
        current = ""
        for start, title in section_starts:
            if start <= line_no:
                current = title
            else:
                break
        return current

    # ① 指定的六项内容都在
    say("D-① 交付要求逐条对照（用户明确点名的六项）")
    required = {
        "① 强制拉取 + 先备份数据不被覆盖": [
            "git fetch --all && git reset --hard origin/main",
            "backend/interview.db",
            "backend/uploads",
        ],
        "② 安装新增 Python 依赖": ["pip install -r requirements.txt"],
        "③ 安全执行迁移 + .bak 回滚": [
            "scripts/migrate.py",
            "backup/manual-",
            "回滚数据库",
        ],
        "④ 本地构建 + scp/rsync 上传 dist": ["npm run build", "rsync", "scp"],
        "⑤ Nginx（/admin 回退 + /api + /uploads）+ certbot": [
            "try_files /index.html",
            "proxy_pass",
            "/uploads/",
            "certbot",
        ],
        "⑥ systemd（自启 + 崩溃重启 + 重启命令）": [
            "WantedBy=multi-user.target",
            "Restart=always",
            "systemctl restart ai-interview",
        ],
    }
    for label, tokens in required.items():
        missing = [t for t in tokens if t not in text]
        if missing:
            bad("%s —— 缺少：%s" % (label, missing))
        else:
            ok("%s ✓" % label)

    say("")
    say("D-② 其余关键要素")
    for label, token in REQUIRED_SNIPPETS.items():
        if token in text:
            ok("%s 在（%r）" % (label, token))
        else:
            bad("%s 缺失（找不到 %r）" % (label, token))

    # ② 禁止在服务器上构建前端
    #
    # 判定逻辑（两层，避免误报又不放过真问题）：
    #   ① **最近的小节标题**里要有"本地 / Windows"字样（§5.1 就叫
    #      "本地 Windows：构建"）—— 只看标题；
    #      若标题里没有，则
    #   ② 命令附近 5 行内要有"本地 / Windows / 本机"这类语境标记。
    #
    # 为什么不能只按标题判：§10.2「以后每次发新版」天然同时包含
    # "本地构建"与"服务器拉代码"两步，按节标题判会把本地那条误报成服务器命令。
    # 为什么不能只按邻近判：一条孤零零的 `npm run build` 若紧跟在
    # "本地"二字之后很远的地方，邻近窗口会漏。
    say("")
    say("D-③ 禁止在 2G 服务器上跑前端构建")
    LOCAL_MARKERS = ("本地", "Windows", "本机", "你的电脑")
    lines = text.splitlines()
    offenders = []
    for idx, line in enumerate(lines):
        if not any(cmd in line for cmd in SERVER_FORBIDDEN):
            continue
        line_no = idx + 1
        # ⚠️ 不能用"往上找最近一行以 # 开头的"来判小节标题：
        #    命令行注释（PowerShell / bash 里的 `# ③ 构建`）也以 # 开头，
        #    会把标题找成注释，于是永远判不出"这是本地小节"。
        #    这里改用 `## 一级小节` 的真实行号区间。
        in_local_section = any(marker in section_title_at(line_no)
                               for marker in LOCAL_MARKERS)
        window = "\n".join(lines[max(0, idx - 5): idx + 6])
        near_local = any(marker in window for marker in LOCAL_MARKERS)
        if not (in_local_section or near_local):
            offenders.append("第 %d 行（小节：%s）：%s"
                             % (line_no, section_title_at(line_no),
                                line.strip()[:50]))
    if offenders:
        bad("这些地方出现了前端构建命令，但既不在「本地/Windows」小节里、"
            "附近也没有相应语境，容易被当成服务器步骤：%s" % offenders)
    else:
        ok("前端构建命令（npm ci / npm run build / vite build）都在"
           "明确标注「本地 / Windows」的语境里，服务器小节没有")
    if "绝对不要在服务器上跑" in text:
        ok("手册里明确写了「绝对不要在服务器上跑 npm run build」及原因（2G 内存）")
    else:
        bad("手册没有显式警告：不要在服务器上跑 npm run build")

    # ③ 顺序：备份必须在 git reset --hard 之前
    say("")
    say("D-④ 动作顺序安全检查")
    i_backup = text.find("uploads.tar.gz")
    i_reset = text.find("git reset --hard origin/main")
    if i_backup == -1 or i_reset == -1:
        bad("找不到备份或 git reset 步骤，无法判断顺序")
    elif i_backup < i_reset:
        ok("备份（uploads.tar.gz）出现在 git reset --hard **之前**（%d < %d）"
           % (i_backup, i_reset))
    else:
        bad("git reset --hard 出现在备份之前 —— 顺序反了会丢数据！")

    i_verify = text.find("## 2.3")
    i_migrate = text.find("scripts/migrate.py")
    if i_verify != -1 and i_migrate != -1 and i_verify < i_migrate:
        ok("备份校验（§2.3）在迁移（§4）之前")
    else:
        bad("迁移出现在备份校验之前 —— 没有可用回滚点就动库")

    i_migrate_run = text.find("venv/bin/python scripts/migrate.py | tee")
    i_rollback = text.find("### 4.5")
    if i_migrate_run != -1 and i_rollback != -1 and i_migrate_run < i_rollback:
        ok("迁移（§4.2）之后紧邻就是回滚小节（§4.5），不需要翻文档找")
    else:
        bad("回滚步骤不在迁移之后紧邻的位置（锚点：%d / %d）"
            % (i_migrate_run, i_rollback))

    # ④ 每个关键小节都写了预期输出与排错
    say("")
    say("D-⑤ 手册形态：预期输出 + 不通过怎么办")
    expected_count = text.count("**预期输出**")
    trouble_count = text.count("**不通过怎么办**")
    if expected_count >= 15:
        ok("写了 %d 处「预期输出」（运维可以逐条对照，不用猜）" % expected_count)
    else:
        bad("只有 %d 处「预期输出」，太少（用户要求包含每一步的预期输出）"
            % expected_count)
    if trouble_count >= 10:
        ok("写了 %d 处「不通过怎么办」（出事时有下一步，而不是卡住）" % trouble_count)
    else:
        bad("只有 %d 处「不通过怎么办」" % trouble_count)

    # ⑤ 停机窗口与回滚锚点
    if "停机" in text and "STAMP" in text:
        ok("写明停机窗口估算，并用 STAMP 贯穿全文（回滚锚点可追溯）")
    else:
        bad("没有写明停机窗口或缺少 STAMP 锚点")
    if "git_head_before" in text:
        ok("回滚用 git_head_before 记录升级前的 commit（可精确退回）")
    else:
        bad("没有记录升级前的 git commit —— 代码回滚无法精确定位")

    # ⑥ 真实性：不许留 TODO/占位符糊弄
    for marker in ("TODO", "TBD", "待补充", "xxx待填"):
        if marker in text:
            bad("手册里残留占位标记 %r —— 运维会照抄" % marker)
    if not any(m in text for m in ("TODO", "TBD", "待补充")):
        ok("没有 TODO/TBD/待补充 之类的残留占位")

    SEG["D"] = len(FAILED) == failed_before


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def summary():
    section("汇总")
    for key, label in (("A", "A 交付物与占位符卫生"), ("B", "B 自动化测试"),
                       ("C", "C 独立复核（选路 + 关键指令）"),
                       ("D", "D Runbook 审计")):
        state = SEG.get(key)
        tag = "SKIP" if state is None else ("PASS" if state else "FAIL")
        say("  [%s] %s" % (tag, label))
    for key, label in (("B1", "  新增 nginx 测试"), ("B2", "  全量后端测试")):
        state = SEG.get(key)
        if state is not None:
            say("        [%s] %s" % ("PASS" if state else "FAIL", label))

    if ENV_PROBLEMS:
        say("")
        say("  环境提示（不算失败）：")
        for item in ENV_PROBLEMS:
            say("    - %s" % item)
    if SKIPPED:
        say("")
        say("  已跳过：")
        for item in SKIPPED:
            say("    - %s" % item)
    if FAILED:
        say("")
        say("  未通过项（%d）：" % len(FAILED))
        for item in FAILED:
            say("    - %s" % item)
        say("")
        say("❌ T-53 人工验收未通过")
        return 1
    say("")
    say("✅ T-53 人工验收通过（Nginx 同源托管 + HTTPS + 前端 dist 上传方案 + Runbook）")
    say("   仍需在阿里云上完成：nginx -t / certbot 签发 / 浏览器 §9.2 的 7 项人工验收")
    return 0


def main():
    _make_stdout_forgiving()
    parser = argparse.ArgumentParser(description="T-53 一键人工验收（离线、无副作用）")
    parser.add_argument("--skip-full-tests", action="store_true",
                        help="跳过 B-② 的全量后端测试（默认跑，约 100 秒）")
    args = parser.parse_args()

    say("T-53 人工验收：Nginx 同源托管 + HTTPS + 前端 dist 上传方案 + 升级 Runbook")
    say("仓库：%s" % REPO_DIR)
    say("Python：%s" % python_exe())

    if not os.path.isdir(NGINX_DIR):
        env_problem("找不到 deploy/nginx：%s" % NGINX_DIR)
        return 2

    segment_artifacts()
    segment_tests(args)
    segment_independent()
    segment_runbook()

    code = summary()
    if code != 0:
        return 1
    if ENV_PROBLEMS and any(SEG.get(k) is None for k in ("C", "D")):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
