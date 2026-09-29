"""T-53 测试：Nginx 站点配置的静态检查 + **location 选路规则复核**。

开发机是 Windows、没有 nginx，所以这些配置在本地无法真跑；
真正的 `nginx -t` 与浏览器验收在阿里云上做（见 `deploy/nginx/README.md`
与 `docs/39-aliyun-upgrade-runbook.md`）。但配置里最容易出错的几件事
**全部可以在本地钉住**，而且它们出错的后果都不是"报错"，而是**静默失效**：

| 坑 | 静默后果 |
|---|---|
| `client_max_body_size` 没调大 | 上传 >5MB 返回 413 而不是 400（验收标准直接不达标） |
| `/admin` 回退写成 `return 301` | 地址栏跳走，`/admin` 永远进不去，且没有任何报错 |
| `proxy_pass` 带结尾 `/` | `/api` 前缀被吃掉 → **全线 404** |
| location 里写了 `add_header` 却没 include 安全头 | CSP / nosniff / HSTS **静默消失** |
| `server_name` 两文件不一致 | 切 HTTPS 后另一个域名/裸 IP 打不开 |
| 少了 `X-Forwarded-For` | 后端拿到的客户端 IP 全是 127.0.0.1 → **一人连错 5 次锁死全员** |

## 为什么本地还能验"选路"

`location` 的优先级是**有确定定义**的（nginx 文档 §location）：

1. `location = /path` 精确匹配 —— 命中即结束；
2. `location ^~ /prefix` 前缀匹配 —— 命中即结束（不再看正则）；
3. `location ~ regex` 正则 —— **按配置文件里出现的先后**，先命中者胜；
4. `location /prefix` 普通前缀 —— 取**最长**匹配。

上面的 `_select_location()` 就是这段规则的实现。它不是为了替代 `nginx -t`
（语法错误还是得靠真 nginx），而是为了回答一个**光看语法看不出来**的问题：
"请求 `/admin` 到底会落到哪个 location？" —— 这正是 T-53 最核心的验收点。
"""

import os
import re
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
NGINX_DIR = os.path.join(REPO_ROOT, "deploy", "nginx")

HTTP_CONF = os.path.join(NGINX_DIR, "ai-interview.conf")
SSL_CONF = os.path.join(NGINX_DIR, "ai-interview-ssl.conf")
ACME_CONF = os.path.join(NGINX_DIR, "acme-challenge.conf")
SECURITY_CONF = os.path.join(NGINX_DIR, "security-headers.conf")
HTML_NOCACHE_CONF = os.path.join(NGINX_DIR, "html-no-cache.conf")
ASSETS_CACHE_CONF = os.path.join(NGINX_DIR, "assets-cache.conf")
README = os.path.join(NGINX_DIR, "README.md")

#: 部署时会被替换的占位符（Runbook 第 6/7 步用 sed 替换）
DOMAIN_PLACEHOLDER = "YOUR_DOMAIN"
APP_PLACEHOLDER = "APP_DIR"

_SECURITY_INCLUDE = "include /etc/nginx/snippets/security-headers.conf;"


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


#: nginx 里的 include 路径 -> 仓库里的真实文件。
#: **必须展开**：`/etc/nginx/snippets/acme-challenge.conf` 提供的是一个
#: `location ^~ /.well-known/acme-challenge/`，如果解析时不展开，
#: "续期请求会不会被 301 吃掉"这个最关键的问题就测不到 —— 而它正是
#: "证书 90 天后突然过期"的头号原因。
SNIPPET_MAP = {
    "/etc/nginx/snippets/acme-challenge.conf": "acme-challenge.conf",
    "/etc/nginx/snippets/security-headers.conf": "security-headers.conf",
    "/etc/nginx/snippets/html-no-cache.conf": "html-no-cache.conf",
    "/etc/nginx/snippets/assets-cache.conf": "assets-cache.conf",
}


def expand_includes(text):
    """把 `include /etc/nginx/snippets/x.conf;` 就地替换成片段正文。"""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("include "):
            target = stripped[len("include "):].rstrip(";").strip()
            name = SNIPPET_MAP.get(target)
            if name is None:
                out.append(line)
                continue
            path = os.path.join(NGINX_DIR, name)
            if os.path.isfile(path):
                out.append("# --- begin include %s ---" % target)
                out.extend(read(path).splitlines())
                out.append("# --- end include %s ---" % target)
                continue
        out.append(line)
    return "\n".join(out)


def strip_comments(text):
    """去掉整行注释。

    为什么必须去掉：本项目的配置文件里**大量注释在解释这些坑**
    （例如"不要写成 `return 301 /`"），用整份文本做断言会把解释性文字
    也算成违规 —— 与 T-17 把 `ON DELETE CASCADE` 的注释误报为破坏性语句
    是同一类假阳性。
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("#", 1)[0] if "#" in line else line)
    return "\n".join(out)


def parse_servers(text):
    """粗略解析出 server 块：返回 [{line_no, header, body}]。

    够用即可：这些配置是我们自己写的，格式受控；不引第三方解析器。
    """
    servers = []
    depth = 0
    current = None
    for idx, line in enumerate(text.splitlines(), start=1):
        bare = line.split("#", 1)[0]
        if re.search(r"\bserver\s*\{", bare) and depth == 0:
            current = {"line_no": idx, "lines": []}
            depth = 1
            continue
        if current is not None:
            depth += bare.count("{") - bare.count("}")
            if depth <= 0:
                servers.append(current)
                current = None
                depth = 0
                continue
            current["lines"].append((idx, bare))
    return servers


def parse_locations(server_body):
    """从 server 块正文里抽 location 指令（含其花括号内的行）。"""
    locations = []
    depth = 0
    current = None
    for line in server_body:
        bare = line[1] if isinstance(line, tuple) else line
        m = re.match(r"\s*location\s+(=|\^~|~\*|~)?\s*(\S+)\s*\{", bare)
        if m and depth == 0:
            modifier = m.group(1) or ""
            current = {"modifier": modifier, "pattern": m.group(2), "body": []}
            depth = 1
            continue
        if current is not None:
            depth += bare.count("{") - bare.count("}")
            if depth <= 0:
                locations.append(current)
                current = None
                depth = 0
                continue
            current["body"].append(bare)
    return locations


def _select_location(locations, uri):
    """按 nginx 的 location 优先级规则选出生效的那个（见模块 docstring）。

    返回 location dict，或 None（都没匹配上）。
    """
    # 1) 精确匹配
    for loc in locations:
        if loc["modifier"] == "=" and loc["pattern"] == uri:
            return loc
    # 2) ^~ 前缀：取最长的那个
    caret = [loc for loc in locations
             if loc["modifier"] == "^~" and uri.startswith(loc["pattern"])]
    if caret:
        return max(caret, key=lambda loc: len(loc["pattern"]))
    # 3) 正则：按出现顺序，先命中者胜
    for loc in locations:
        if loc["modifier"] in ("~", "~*"):
            pattern = loc["pattern"]
            flags = re.IGNORECASE if loc["modifier"] == "~*" else 0
            # nginx 的 `location ~` 是"部分匹配"（不锚定首尾）
            if re.search(pattern, uri, flags):
                return loc
    # 4) 普通前缀：最长匹配
    plain = [loc for loc in locations
             if loc["modifier"] == "" and uri.startswith(loc["pattern"])]
    if plain:
        return max(plain, key=lambda loc: len(loc["pattern"]))
    return None


def location_of(conf_path, server_index, uri):
    text = strip_comments(expand_includes(read(conf_path)))
    servers = parse_servers(text)
    locations = parse_locations(servers[server_index]["lines"])
    return _select_location(locations, uri)


class LocationContestTests(unittest.TestCase):
    """**选路复核**：请求到底落到哪个 location。"""

    def setUp(self):
        self.text = strip_comments(expand_includes(read(HTTP_CONF)))
        self.servers = parse_servers(self.text)
        self.locations = parse_locations(self.servers[0]["lines"])

    def test_admin_exact_matches_the_admin_rule(self):
        loc = _select_location(self.locations, "/admin")
        self.assertEqual(loc["pattern"], "/admin")
        self.assertEqual(loc["modifier"], "=")

    def test_admin_subtree_matches_the_caret_rule(self):
        loc = _select_location(self.locations, "/admin/users")
        self.assertEqual(loc["pattern"], "/admin/")
        self.assertEqual(loc["modifier"], "^~")

    def test_admin_is_not_stolen_by_the_static_regex(self):
        """`/admin` 里没有点号，不该落到正则 location（真落到就会找文件、404）。"""
        loc = _select_location(self.locations, "/admin")
        self.assertNotIn(loc["modifier"], ("~", "~*"))

    def test_api_and_api_subpath_match_the_api_rule(self):
        for uri in ("/api", "/api/login", "/api/interview/start"):
            loc = _select_location(self.locations, uri)
            self.assertEqual(loc["pattern"], "/api", "uri=%s 落到了 %s" % (uri, loc))

    def test_uploads_subpath_matches_the_uploads_rule(self):
        """**回归用例**：头像 URL 必须落到 `/uploads/`，不能被静态文件正则抢走。

        nginx 的规则是"正则优先于**普通**前缀"。所以 `/uploads/` 若不写 `^~`，
        `/uploads/avatars/user_1_ab.jpg` 会命中
        `location ~* \.(jpg|png|...)$`，nginx 转头去 `root`（= dist 目录）
        找这个 jpg —— 找不到就 404，报错还只说"文件不存在"。
        """
        loc = _select_location(self.locations, "/uploads/avatars/user_1_ab.jpg")
        self.assertEqual(loc["pattern"], "/uploads/")
        self.assertEqual(loc["modifier"], "^~",
                         "/uploads/ 必须是 `^~` 前缀，否则会被静态文件正则抢走")

    def test_hashed_assets_match_the_assets_rule(self):
        loc = _select_location(self.locations, "/assets/index-B616GuUq.js")
        self.assertEqual(loc["pattern"], "/assets/")

    def test_root_level_svg_matches_the_static_regex_not_assets(self):
        """`/favicon.svg` 不在 /assets/ 下，要落到静态文件正则那条。"""
        loc = _select_location(self.locations, "/favicon.svg")
        self.assertIn(loc["modifier"], ("~", "~*"))

    def test_site_root_matches_the_spa_fallback(self):
        loc = _select_location(self.locations, "/")
        self.assertEqual(loc["pattern"], "/")
        self.assertEqual(loc["modifier"], "")

    def test_dotfiles_match_the_deny_rule(self):
        """`.env` / `.git/config` 必须落到 deny 那条（正则先于普通前缀）。"""
        for uri in ("/.env", "/.git/config"):
            loc = _select_location(self.locations, uri)
            self.assertIn(loc["modifier"], ("~", "~*"), "uri=%s 落到了 %s" % (uri, loc))
            self.assertIn("deny all", "\n".join(loc["body"]),
                          "隐藏文件规则里没有 deny all")


class SpaFallbackTests(unittest.TestCase):
    """T-53 的核心验收点：`/admin` 刷新必须 **200**，不能 301/404。"""

    def setUp(self):
        self.text = strip_comments(expand_includes(read(HTTP_CONF)))
        self.server0 = parse_servers(self.text)[0]
        self.locations = parse_locations(self.server0["lines"])

    def _body_of(self, pattern):
        for loc in self.locations:
            if loc["pattern"] == pattern:
                return "\n".join(loc["body"])
        self.fail("找不到 location %s" % pattern)

    def test_admin_locations_use_try_files_not_redirect(self):
        for pattern in ("/admin", "/admin/"):
            body = self._body_of(pattern)
            self.assertIn("try_files /index.html", body,
                          "%s 没有内部回退到 index.html" % pattern)
            self.assertNotIn("return 30", body,
                             "%s 用了 3xx 跳转 —— 地址栏会跳走，/admin 永远进不去"
                             % pattern)
            self.assertNotIn("rewrite", body,
                             "%s 用了 rewrite（若带 redirect/permanent 同样是 3xx）"
                             % pattern)

    def test_spa_fallback_serves_index_html_last(self):
        body = self._body_of("/")
        self.assertIn("try_files $uri $uri/ /index.html", body)

    def test_admin_location_is_declared_before_the_root_location(self):
        """顺序虽不影响优先级，但 `=`/`^~` 写在前面可读性最好且不易被误改。"""
        patterns = [loc["pattern"] for loc in self.locations]
        self.assertLess(patterns.index("/admin"), patterns.index("/"))

    def test_acceptance_from_the_task_file(self):
        """逐字对照 `docs/03-tasks.md` 的 T-53 验收标准做一次选路复核。"""
        # "`/admin` 的 SPA history 回退（BrowserRouter 刷新 /admin 会真的请求该路径）"
        for uri in ("/admin", "/admin/"):
            loc = _select_location(self.locations, uri)
            body = "\n".join(loc["body"])
            self.assertIn("try_files /index.html", body, "uri=%s" % uri)


class ProxyContractTests(unittest.TestCase):
    """反代契约：路径不被改写、真实 IP 能传到后端、上传体积放得下。"""

    def setUp(self):
        self.raw = strip_comments(expand_includes(read(HTTP_CONF)))
        self.server0 = parse_servers(self.raw)[0]
        self.locations = parse_locations(self.server0["lines"])

    def _body_of(self, pattern):
        for loc in self.locations:
            if loc["pattern"] == pattern:
                return "\n".join(loc["body"])
        self.fail("找不到 location %s" % pattern)

    def test_api_proxy_pass_does_not_rewrite_the_prefix(self):
        """`proxy_pass http://upstream/;` 会把 `/api` 吃掉 → 全线 404。"""
        body = self._body_of("/api")
        m = re.search(r"proxy_pass\s+(\S+);", body)
        self.assertIsNotNone(m, "/api 没有 proxy_pass")
        target = m.group(1)
        self.assertFalse(target.endswith("/"),
                         "proxy_pass 结尾带 `/` 会剥掉 /api 前缀：%s" % target)
        self.assertNotIn("$uri", target, "proxy_pass 里不该出现 $uri 改写路径")

    def test_api_forwards_real_client_ip(self):
        body = self._body_of("/api")
        self.assertIn("proxy_set_header X-Real-IP         $remote_addr;", body)
        self.assertIn("X-Forwarded-For   $proxy_add_x_forwarded_for;", body)
        self.assertNotIn("X-Forwarded-For   $http_x_forwarded_for;", body,
                         "直接透传客户端伪造的 XFF —— 限流会被绕过")

    def test_api_forwards_scheme_and_host(self):
        body = self._body_of("/api")
        self.assertIn("X-Forwarded-Proto $scheme;", body)
        self.assertIn("X-Forwarded-Host  $host;", body)

    def test_api_does_not_buffer_streaming_responses(self):
        self.assertIn("proxy_buffering off;", self._body_of("/api"))

    def test_uploads_is_proxied_so_there_is_only_one_path_rule(self):
        """与后端 `app.mount("/uploads", StaticFiles(directory="uploads"))` 对齐。

        用 alias 直接读盘会多出"第二个真相"（Nginx 读哪个目录），
        而后端用的是**相对路径** `uploads/avatars`（取决于 uvicorn 的 cwd）。
        """
        body = self._body_of("/uploads/")
        self.assertIn("proxy_pass", body)
        self.assertNotIn("alias", body)

    def test_upload_body_size_lets_the_app_return_400(self):
        """验收标准："上传 >5MB 简历返回 400（**不是 413**）"。

        后端 `MAX_FILE_SIZE = 5MB` 在应用里判；Nginx 默认 1m 会先掐掉。
        因此这里必须 ≥ 8m（给 5MB 边界留出 multipart 开销余量）。
        """
        m = re.search(r"client_max_body_size\s+(\d+)m;", self.raw)
        self.assertIsNotNone(m, "没有配置 client_max_body_size（默认 1m 会返回 413）")
        self.assertGreaterEqual(int(m.group(1)), 8,
                                "client_max_body_size=%sm 太小，>5MB 会变成 413" % m.group(1))

    def test_upstream_points_at_loopback_8000(self):
        m = re.search(r"upstream\s+ai_interview_backend\s*\{([^}]*)\}", self.raw)
        self.assertIsNotNone(m, "找不到 upstream 定义")
        self.assertIn("127.0.0.1:8000", m.group(1),
                      "后端只监听本机 8000，反代目标必须是回环地址")


class SecurityHeaderTests(unittest.TestCase):
    """`add_header` 不叠加：写了缓存头的 location 必须显式 include 安全头。"""

    def setUp(self):
        self.text = strip_comments(read(HTTP_CONF))
        self.locations = parse_locations(parse_servers(self.text)[0]["lines"])

    def test_every_location_with_add_header_also_includes_security_headers(self):
        """这是本文件最容易被后人改坏的一条。

        nginx 的规则是"子级写了 add_header，父级的整族失效"（不是叠加），
        所以在 location 里写 `add_header Cache-Control ...` 会让
        server 级的 CSP/nosniff/HSTS **全部消失**，而页面照常打开 ——
        只有安全扫描或真被 iframe 嵌套时才暴露。
        """
        offenders = []
        for loc in self.locations:
            body = "\n".join(loc["body"])
            if "add_header" in body and _SECURITY_INCLUDE not in body:
                offenders.append(loc["pattern"])
        self.assertEqual(offenders, [],
                         "这些 location 写了 add_header 却没 include 安全头，"
                         "CSP/nosniff 会被静默顶掉：%s" % offenders)

    def test_snippet_defines_the_expected_headers(self):
        text = strip_comments(read(SECURITY_CONF))
        for header in ("X-Content-Type-Options", "X-Frame-Options",
                       "Referrer-Policy", "Content-Security-Policy"):
            self.assertIn(header, text, "安全头片段里没有 %s" % header)

    def test_headers_use_always_so_error_responses_carry_them(self):
        text = strip_comments(read(SECURITY_CONF))
        for line in text.splitlines():
            if line.strip().startswith("add_header"):
                self.assertIn("always", line,
                              "缺少 always，4xx/5xx 响应不会带这个头：%s" % line.strip())

    def test_csp_allows_self_only_for_scripts(self):
        text = read(SECURITY_CONF)
        m = re.search(r"Content-Security-Policy \"([^\"]+)\"", text)
        self.assertIsNotNone(m)
        csp = m.group(1)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp,
                         "脚本不该放开 unsafe-inline")
        self.assertIn("style-src 'self' 'unsafe-inline'", csp,
                      "Vite 会内联首屏样式，样式必须允许 unsafe-inline")

    def test_html_is_never_long_cached(self):
        """入口 HTML 长缓存 = 用户永远拿到指向旧 hash 的页面。"""
        text = strip_comments(read(HTML_NOCACHE_CONF))
        self.assertIn('Cache-Control "no-store"', text)

    def test_hashed_assets_are_immutable(self):
        text = strip_comments(read(ASSETS_CACHE_CONF))
        self.assertIn("immutable", text)

    def test_admin_and_root_locations_include_the_no_cache_snippet(self):
        # 注意：这里刻意用**未展开 include** 的原文 —— 展开之后只看得到
        # add_header 的效果，看不到"是否 include 了片段"这件事本身。
        text = strip_comments(read(HTTP_CONF))
        for pattern in ("/admin", "/admin/", "/"):
            loc = None
            for candidate in parse_locations(parse_servers(text)[0]["lines"]):
                if candidate["pattern"] == pattern:
                    loc = candidate
            self.assertIsNotNone(loc, "找不到 location %s" % pattern)
            self.assertIn("include /etc/nginx/snippets/html-no-cache.conf;",
                          "\n".join(loc["body"]),
                          "%s 没有 include 入口 HTML 禁缓存片段" % pattern)


class DeploymentHygieneTests(unittest.TestCase):
    """部署件的卫生：占位符、文件齐全、开启顺序。"""

    def test_http_and_https_share_the_same_server_name_placeholder(self):
        """两个文件必须用**同一个**占位符，否则 Runbook 的 sed 只换一个，
        切到 HTTPS 后另一个域名打不开。"""
        for path in (HTTP_CONF, SSL_CONF):
            self.assertIn("server_name %s;" % DOMAIN_PLACEHOLDER, read(path),
                          "%s 里没有统一的 server_name 占位符" % os.path.basename(path))

    def test_placeholders_are_only_known_ones(self):
        """配置里不该出现"某次部署临时填的"真实域名/IP —— 那会让下一个人
        在另一个环境里踩坑。占位符只允许这两个（或其拼接形式）。"""
        for path in (HTTP_CONF, SSL_CONF):
            text = strip_comments(read(path))
            for token in re.findall(r"^\s*(?:server_name|root)\s+([^;]+);", text, re.M):
                for word in token.split():
                    if word.startswith("$") or word.startswith("/etc/") \
                            or word.startswith("/var/"):
                        continue        # 系统级固定路径（证书、snippets、webroot）
                    allowed = (
                        word == DOMAIN_PLACEHOLDER
                        or word == APP_PLACEHOLDER
                        or word.startswith(APP_PLACEHOLDER + "/")
                    )
                    self.assertTrue(
                        allowed,
                        "%s 里出现了非占位符的部署相关值：%r"
                        % (os.path.basename(path), word))
            # 明确禁止把真实 IP 写进配置（占位符之外）
            self.assertNotIn("112.124.54.223", text,
                             "配置里写死了线上 IP，别人拿去部署会踩坑")

    def test_ssl_config_is_the_only_one_with_ssl_listen(self):
        """HTTP 文件里出现 `listen 443` 会让"先 HTTP 后 HTTPS"的顺序失去意义。"""
        self.assertNotIn("listen 443", strip_comments(read(HTTP_CONF)))
        self.assertIn("listen 443 ssl;", strip_comments(read(SSL_CONF)))

    def test_ssl_config_redirects_http_to_https_with_301(self):
        text = strip_comments(read(SSL_CONF))
        self.assertIn("return 301 https://$host$request_uri;", text,
                      "80 端口没有 301 跳转（验收标准要求 http → 301 → https）")

    def test_both_configs_keep_the_acme_location_on_port_80(self):
        """续期必须还能走 80：被 301 吃掉是"证书突然过期"的头号原因。"""
        for path in (HTTP_CONF, SSL_CONF):
            self.assertIn("acme-challenge.conf", read(path),
                          "%s 没有 ACME 校验片段" % os.path.basename(path))

    def test_acme_snippet_beats_the_redirect_by_priority(self):
        """用选路规则证明：`/.well-known/acme-challenge/xxx` 不会落到 301 那条。"""
        text = strip_comments(expand_includes(read(SSL_CONF)))
        locations = parse_locations(parse_servers(text)[1]["lines"])
        self.assertTrue(locations, "80 端口那个 server 块里没有 location")
        chosen = _select_location(locations, "/.well-known/acme-challenge/token123")
        self.assertIsNotNone(chosen)
        self.assertNotEqual(chosen["pattern"], "/",
                            "续期请求落到了 `location /` 的 301 上")
        self.assertEqual(chosen["modifier"], "^~")
        self.assertIn("root /var/www/certbot;", read(ACME_CONF))

    def test_upstream_defined_exactly_once_per_file(self):
        """重复定义会让 nginx 直接拒绝启动（duplicate upstream）。"""
        for path in (HTTP_CONF, SSL_CONF):
            count = strip_comments(read(path)).count("upstream ai_interview_backend")
            self.assertEqual(count, 1,
                             "%s 里 upstream 定义了 %d 次" % (os.path.basename(path), count))

    def test_only_http_config_mentions_the_initial_install_path(self):
        """sites-available 的文件名要能对上 Runbook 的 ln -sf 命令。"""
        for path in (HTTP_CONF, SSL_CONF):
            text = read(path)
            expected = ("sites-available/ai-interview-ssl"
                        if path == SSL_CONF else "sites-available/ai-interview")
            self.assertIn(expected, text)

    def test_nginx_version_guidance_is_present(self):
        """nginx ≥1.25.1 弃用了 `listen ... http2`；配置里必须写明判断方法。"""
        self.assertIn("1.25.1", read(SSL_CONF))

    def test_hsts_starts_small(self):
        """一上来就 preload/1 年，证书出问题期间站点会彻底打不开。"""
        text = strip_comments(read(SSL_CONF))
        m = re.search(r'max-age=(\d+)', text)
        self.assertIsNotNone(m, "没有 HSTS")
        self.assertLessEqual(int(m.group(1)), 2592000,
                             "HSTS 初始 max-age 太长（先观察一轮续期再加）")
        self.assertNotIn("preload", text, "初始不应开 preload")

    def test_readme_exists_and_covers_install_and_tls(self):
        self.assertTrue(os.path.isfile(README), "缺少 deploy/nginx/README.md")
        text = read(README)
        for token in ("sites-available", "certbot", "nginx -t",
                      "dist", "413", "/admin"):
            self.assertIn(token, text, "README 里没提到 %r" % token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
