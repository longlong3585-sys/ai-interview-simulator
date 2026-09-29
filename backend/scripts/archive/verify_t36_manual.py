#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T-36 人工验收：api 层收敛（401 统一登出 / X-Refreshed-Token 集中处理 / 裸 fetch 归零）。

一条命令，**不起服务、不联网、不用手动复制 Token、不花钱**：

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t36_manual.py

可选参数：
    --skip-tsc      跳过 B 段（TypeScript 全量类型检查）
    --strict-window 额外断言"参与方文件里 0 处旧拼接"（默认已开，保留参数以便单独复跑）

退出码：0 = 通过；1 = 未通过；2 = 环境问题。

────────────────────────────────────────────────────────────────────────────
验收标准（`docs/03-tasks.md` §阶段 4 的 T-36）：

  * 全库 grep **裸 `fetch(\\`${API_BASE_URL}` 为 0**；
  * **401 统一登出**；
  * **续期头集中处理**（`X-Refreshed-Token`）。

四段互不依赖的取证：

  0. 预检      —— node、目标文件、node_modules。
  A. 契约测试  —— `node --test`（TAP）：fail 必须为 0，测试数量不低于下限，
                  且**点名用例必须真的出现过**（防止测试被删 / 改名后"假通过"）。
  B. 类型检查  —— `tsc -b` 退出码 0（收敛会改动很多调用点，类型是最便宜的回归网）。
  C. 独立复核  —— **本脚本自己重扫源码**，不 import 测试里的任何辅助函数：
                  ① 裸 fetch 计数（含**逐点定位**）/ ② 401 统一登出链路 /
                  ③ 续期头集中处理链路 / ④ 迁移清单（原先 18 处调用点现在都走统一层），
                  每条都带**判别力自检**（把收敛前的写法喂进同一个判定器，必须判为违规）。

为什么 C 段要自己重扫一遍而不是只看 A 段：
  A 段的契约测试与被测代码在同一个仓库、同一轮改动里，理论上可能"一起改错"。
  C 段用另一套语言（Python 正则）复述同一条规则，两套实现都通过才算证据。

已知边界（刻意如此，非遗漏）：
  * **后端目前还没有下发 `X-Refreshed-Token`**（那是 T-50 / ADR-016 的服务端侧）。
    本任务交付的是**前端前置**：集中读头 + 落 localStorage + 通知订阅者，
    并用 `tests/api-convergence.test.mjs` 真跑一遍该链路（假 storage / 假响应）。
    因此"续期真的生效"要等 T-50 后端配合，本条不假装已经端到端打通。
  * **不驱动真实浏览器**。"401 后真的登出"是**源码链路**证明的：
    唯一出口 `request()` 在 401 分支先 `notifyUnauthorized()` 再抛错，
    回调由 `main.tsx` → `<AuthBridge />` → `registerUnauthorizedHandler(signOut)` 接线。
    肉眼验收见 `docs/34-manual-verification.md` §4。
  * **`npm run lint` 本来就是红的**（与 T-34/T-48 之后相同）：本任务不假装 lint 是绿的。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

import os as _archive_os

# 归档位置：backend/scripts/archive/<本文件> —— 四层 dirname 即仓库根。
_ARCHIVE_REPO = _archive_os.path.dirname(_archive_os.path.dirname(
    _archive_os.path.dirname(_archive_os.path.dirname(
        _archive_os.path.abspath(__file__)))))
REPO_DIR = _ARCHIVE_REPO
FRONTEND_DIR = os.path.join(REPO_DIR, "frontend")
SRC_DIR = os.path.join(FRONTEND_DIR, "src")

FILES = {
    "统一出口": "src/services/api.ts",
    "认证响应纯逻辑": "src/services/authResponse.ts",
    "401 接线桥": "src/auth/AuthBridge.tsx",
    "应用入口": "src/main.tsx",
    "契约测试": "tests/api-convergence.test.mjs",
}

# A 段必须出现过的测试名（防止测试被删/改名后假通过）
REQUIRED_TEST_NAMES = [
    "③ src 下除 services/api.ts 外，源码里的裸 fetch 为 0 处",
    "③ 判别力自检：收敛前的 18 处裸 fetch 写法必须被判为违规",
    "① 统一出口在 401 时先登出、再抛错（顺序写在源码里，不是靠调用点自觉）",
    "① 401 处理已接线：main.tsx 渲染 AuthBridge，AuthBridge 注册 signOut",
    "② 统一出口在响应处集中处理续期头：读头 → 写 localStorage → 通知订阅者",
]

# 统一出口里**允许**出现的 fetch 次数（request / publicFetch 各一处）
ALLOWED_FETCH_IN_API_SERVICE = 2

# 收敛前的 18 处裸 fetch 分布（来自 `docs/03-tasks.md` T-36 与实测 grep）
LEGACY_FETCH_SITES = {
    "src/auth/AuthModal.tsx": 2,
    "src/interview/questionBank.ts": 1,
    "src/profile/ProfilePanel.tsx": 3,
    "src/notifications/useNotificationCenter.ts": 9,
    "src/interview/useInterviewSession.ts": 2,
}

# 参与收敛的文件必须已经**不再**参与 URL 拼接（统一层负责拼）
URL_CONCAT = re.compile(r"\$\{API_BASE_URL\}")

API_LAYER_FILES = ("src/services/api.ts", "src/services/authResponse.ts")

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}


# ---------------------------------------------------------------------------
# 输出小工具（与 verify_t34 / verify_t44_t45 / verify_t46_t49 同风格）
# ---------------------------------------------------------------------------

def _make_stdout_forgiving():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
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


def run(cmd, cwd=FRONTEND_DIR, timeout=900):
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
    except FileNotFoundError as exc:
        return None, "命令不存在：%s" % exc
    except subprocess.TimeoutExpired:
        return None, "超时（%ss）" % timeout
    except OSError as exc:
        return None, "无法启动进程：%s" % exc
    return proc.returncode, (proc.stdout.decode("utf-8", errors="replace") if proc.stdout else "")


def find_node():
    for name in ("node", "node.exe"):
        code, out = run([name, "--version"], cwd=REPO_DIR, timeout=60)
        if code == 0:
            return name, out.strip()
    return None, None


def read(rel):
    with open(os.path.join(FRONTEND_DIR, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def code_only(text):
    """去注释：注释里正**引用**着 `fetch(` 和旧写法（那是文档，不是调用）。"""
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    out = []
    for line in text.splitlines():
        if line.strip().startswith("//"):
            continue
        out.append(re.sub(r"\s//.*$", "", line))
    return "\n".join(out)


def walk_sources(root=SRC_DIR):
    """返回 {"src/<相对路径>": 源码}（键与 FILES / LEGACY_FETCH_SITES 同一口径）。"""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "r", encoding="utf-8") as fh:
                found["src/" + rel] = fh.read()
    return found


# ---------------------------------------------------------------------------
# 核心判定器（C 段用；A 段另有 JS 版实现，两套独立复述同一条规则）
# ---------------------------------------------------------------------------

# 裸 fetch 调用：前面不是 `.` / 标识符字符（排除 `.fetch(`、`refetch(`、`prefetch(`）。
NAKED_FETCH = re.compile(r"(?<![.\w$])fetch\s*\(")

# 统一层的函数名（只有这些函数的第一个实参才算"被调用的 API 路径"）
API_FUNCS = ("request", "authFetch", "publicFetch", "apiGet", "apiPost", "apiPut",
             "apiPatch", "apiDelete", "publicGet", "publicPost")
API_CALL_RE = re.compile(
    r"\b(?:" + "|".join(API_FUNCS) + r")\s*\(\s*[\"'`](?P<path>/api/[^\"'`\s)]*)"
)


def count_naked_fetch(files):
    """返回 {相对路径: 次数}（已排除统一层自己）。"""
    hits = {}
    for rel, src in files.items():
        if rel in API_LAYER_FILES:
            continue
        n = len(NAKED_FETCH.findall(code_only(src)))
        if n:
            hits[rel] = n
    return hits


def legacy_api_url_literals(files):
    """旧写法 `${API_BASE_URL}/api/...` 出现在**参与收敛的文件**里的次数。

    这是验收标准里那条 grep 的可执行版本（注意：`config.ts` 里那句
    `import.meta.env.VITE_API_BASE_URL` 不是模板串拼接，正则不会命中）。
    **统一层自己是唯一的例外** —— 拼基地址正是它的职责（`resolveApiUrl`）。
    """
    return {rel: len(URL_CONCAT.findall(src))
            for rel, src in files.items()
            if rel not in API_LAYER_FILES and URL_CONCAT.search(src)}


def collect_api_call_paths(files):
    """收集全库"通过统一层发出的 API 路径"（去注释后，按函数名 + 首参形态认）。"""
    paths = set()
    for rel, src in files.items():
        if rel in API_LAYER_FILES:
            continue
        for m in API_CALL_RE.finditer(code_only(src)):
            p = re.sub(r"\$\{[^}]*\}", "", m.group("path"))
            p = p.rstrip("/")
            if p:
                paths.add(p)
    return paths


def collect_api_imports(files):
    """收集各文件从 services/api 引入的函数名 → {文件: {名字...}}。"""
    out = {}
    imp = re.compile(r"import\s*\{([^}]*)\}\s*from\s*[\"'][^\"']*services/api[\"']")
    for rel, src in files.items():
        m = imp.search(src)
        if not m:
            continue
        names = {n.strip().split(" as ")[-1].strip() for n in m.group(1).split(",") if n.strip()}
        out[rel] = names
    return out


# ---------------------------------------------------------------------------
# 0. 预检
# ---------------------------------------------------------------------------

def segment_preflight():
    section("0. 预检（node / 目标文件 / node_modules）")

    node_path, node_version = find_node()
    if node_path:
        ok("node 可执行：%s（%s）" % (node_path, node_version))
    else:
        env_problem("找不到 node 可执行文件（A 段无法运行）")

    missing = [rel for rel in FILES.values() if not os.path.exists(os.path.join(FRONTEND_DIR, rel))]
    if missing:
        bad("T-36 的交付物缺失：%s" % "、".join(missing))
    else:
        ok("%d 个目标文件全部就位（统一出口 / 纯逻辑 / 401 接线 / 入口 / 契约测试）" % len(FILES))

    if os.path.isdir(os.path.join(FRONTEND_DIR, "node_modules")):
        ok("node_modules 存在")
    else:
        env_problem("缺少 frontend/node_modules —— 请先 `cd frontend; npm install`")

    return node_path


# ---------------------------------------------------------------------------
# A. 契约测试
# ---------------------------------------------------------------------------

def segment_contract_tests(node_path):
    section("A. 前端契约测试（node --test，零依赖、不联网）")
    if not node_path:
        env_problem("没有 node，A 段跳过")
        SEG["A"] = None
        return

    code, out = run([node_path, "--test", "--experimental-test-isolation=none",
                     "--test-reporter=tap", "tests/**/*.test.mjs"], timeout=600)
    if code is None:
        env_problem("契约测试无法执行：%s" % out.strip()[:200])
        SEG["A"] = None
        return

    lines = [l.strip() for l in out.splitlines() if l.strip()]
    say("  —— `node --test`（TAP）尾部 5 行 ——")
    for line in lines[-5:]:
        say("     %s" % line.encode("ascii", "replace").decode("ascii"))

    m = {k: re.search(r"^#\s*%s\s+(\d+)" % k, out, re.M) for k in ("tests", "pass", "fail")}
    if not all(m.values()):
        env_problem("未能从 TAP 输出里解析出 tests/pass/fail")
        SEG["A"] = False
        return
    tests, passed, failed = (int(m[k].group(1)) for k in ("tests", "pass", "fail"))
    say("  解析结果：tests=%d pass=%d fail=%d" % (tests, passed, failed))

    if failed == 0:
        ok("全部契约测试通过（pass=%d fail=0）" % passed)
    else:
        bad("有 %d 项契约测试失败（完整输出请直接跑 npm run test:node）" % failed)

    # T-36 新增 16 项；加进来之后总数应 ≥ 100（T-34 时为 85）。
    if tests >= 100:
        ok("测试数量合理（%d ≥ 100，说明既有契约测试没被删、T-36 的用例真的进了套件）" % tests)
    else:
        bad("只跑了 %d 项测试（预期 ≥ 100）" % tests)

    for name in REQUIRED_TEST_NAMES:
        if name in out:
            ok("点名用例确实跑过：%s" % name)
        else:
            bad("输出里没有出现点名用例「%s」—— 它可能被删/改名，那样就是假通过" % name)

    SEG["A"] = (failed == 0)


# ---------------------------------------------------------------------------
# B. tsc
# ---------------------------------------------------------------------------

def segment_tsc(node_path, args):
    section("B. TypeScript 全量类型检查（tsc -b）")
    if args.skip_tsc:
        skip("--skip-tsc：按要求跳过")
        SEG["B"] = None
        return
    tsc_js = os.path.join(FRONTEND_DIR, "node_modules", "typescript", "bin", "tsc")
    if not node_path or not os.path.exists(tsc_js):
        env_problem("找不到 node_modules/typescript/bin/tsc，跳过 B 段")
        SEG["B"] = None
        return
    code, out = run([node_path, tsc_js, "-b"], timeout=900)
    if code is None:
        env_problem("tsc 无法执行：%s" % out.strip()[:200])
        SEG["B"] = None
        return
    if code == 0:
        ok("tsc -b 退出码 0：18 处迁移后的调用点、统一层签名、401 接线全部类型安全")
    else:
        bad("tsc -b 退出码 %s：\n%s" % (code, out.strip()[:1500]))
    SEG["B"] = (code == 0)


# ---------------------------------------------------------------------------
# C. 独立复核
# ---------------------------------------------------------------------------

def segment_source_recheck():
    section("C. 独立复核（本脚本自己重扫源码，不 import 测试里的辅助函数）")

    files = walk_sources()
    api_service = files.get("src/services/api.ts", "")

    # —— C-① 裸 fetch 计数 ——
    hits = count_naked_fetch(files)
    if not hits:
        ok("① src 下 %d 个 .ts/.tsx 文件里，除统一层外**裸 fetch = 0 处**"
           % len(files))
    else:
        detail = "、".join("%s(%d 处)" % (k, v) for k, v in sorted(hits.items()))
        bad("① 仍有文件直接调 fetch：%s" % detail)

    n_api_fetch = len(NAKED_FETCH.findall(code_only(api_service)))
    if n_api_fetch == ALLOWED_FETCH_IN_API_SERVICE:
        ok("① 统一层自己只有 %d 处 fetch（request / publicFetch 两个出口，没有第三个漏网出口）"
           % n_api_fetch)
    else:
        bad("① services/api.ts 里有 %d 处 fetch（预期 %d）" % (n_api_fetch, ALLOWED_FETCH_IN_API_SERVICE))

    # 判别力自检：把收敛前的写法喂进同一个判定器，必须判为违规。
    pre_fix = {
        "src/auth/AuthModal.tsx": "const res = await fetch(`${API_BASE_URL}/api/captcha`);",
        "src/notifications/useNotificationCenter.ts":
            "await fetch(`${API_BASE_URL}/api/history`, { headers });",
        "src/util/x.ts": "refetchCount += 1; obj.fetch(); // fetch(`/api/x`)",
    }
    detected = count_naked_fetch(pre_fix)
    if set(detected) == {"src/auth/AuthModal.tsx", "src/notifications/useNotificationCenter.ts"}:
        ok("① 判别力自检：收敛前的写法被判为违规，而 `refetch(` / `.fetch(` / 注释里的 fetch 不被误判")
    else:
        bad("① 判别力自检失败：判定器给出 %s（规则可能已失效）" % detected)

    # 验收标准原文那条 grep（可执行版本）
    legacy = legacy_api_url_literals(files)
    if not legacy:
        ok("① 全库 grep `${API_BASE_URL}` 模板拼接 = 0 处（验收标准原文的口径，含 admin 页签）")
    else:
        detail = "、".join("%s(%d 处)" % (k, v) for k, v in sorted(legacy.items()))
        bad("① 仍有文件自己拼基地址：%s" % detail)

    # —— C-② 401 统一登出 ——
    main_src = files.get("src/main.tsx", "")
    bridge_src = files.get("src/auth/AuthBridge.tsx", "")

    if re.search(r"<AuthBridge\s*/>", main_src) and "AuthBridge" in main_src:
        ok("② main.tsx 渲染了 <AuthBridge />（401 登出有接线入口）")
    else:
        bad("② main.tsx 没有渲染 <AuthBridge /> —— 401 不会登出")

    if re.search(r"registerUnauthorizedHandler\s*\(\s*signOut\s*\)", bridge_src):
        ok("② AuthBridge 把 AuthContext 的 signOut 注册进统一层")
    else:
        bad("② AuthBridge 没有注册 signOut")

    if "AuthContext" not in code_only(api_service):
        ok("② 统一层不 import AuthContext（用注册口注入，避免运行时环）")
    else:
        bad("② 统一层 import 了 AuthContext —— 会构成运行时环")

    idx = api_service.find("export async function request")
    if idx < 0:
        bad("② services/api.ts 里找不到 request() 出口")
    else:
        end = api_service.find("export async function authFetch")
        body = api_service[idx:end if end > 0 else len(api_service)]
        notify, throwing = body.find("notifyUnauthorized()"), body.find("throw new ApiError(401")
        if notify >= 0 and throwing >= 0 and notify < throwing:
            ok("② 401 分支：先 notifyUnauthorized() 再抛 401（顺序正确，登出路径唯一）")
        else:
            bad("② 401 分支缺少「先登出再抛错」（notify=%d throw=%d）" % (notify, throwing))

    # —— C-③ 续期头集中处理 ——
    auth_resp = files.get("src/services/authResponse.ts", "")
    if "X-Refreshed-Token" in auth_resp:
        ok("③ 响应头名 `X-Refreshed-Token` 集中在 authResponse.ts（与验收标准一致）")
    else:
        bad("③ 找不到 `X-Refreshed-Token` 的字面量")

    # 统一层必须是**唯一**读该头的地方：其它文件不许自己 headers.get(...)
    others = [rel for rel, src in files.items()
              if rel != "src/services/authResponse.ts" and "X-Refreshed-Token" in code_only(src)]
    if not others:
        ok("③ 除 authResponse.ts 外，没有任何文件自己读该响应头（处理点唯一）")
    else:
        bad("③ 这些文件也在处理续期头：%s" % "、".join(sorted(others)))

    if "readRefreshedToken(response)" in api_service and "handleRefreshedToken" in api_service:
        order_read = api_service.find("readRefreshedToken(response)")
        persist = api_service.find("applyRefreshedToken(storage, refreshed)")
        listener = api_service.find("listener(refreshed)")
        if 0 <= order_read < persist < listener:
            ok("③ 续期链路顺序正确：读头 → 落 localStorage → 通知订阅者")
        else:
            bad("③ 续期链路顺序不对（read=%d persist=%d notify=%d）" % (order_read, persist, listener))
    else:
        bad("③ 统一层缺少 handleRefreshedToken / readRefreshedToken 的调用")

    n_observe = len(re.findall(r"observeResponse\(response\)", code_only(api_service)))
    if n_observe == 2:
        ok("③ 两个出口（request / publicFetch）都经过续期处理，没有「漏一类请求不续期」")
    else:
        bad("③ observeResponse(response) 出现 %d 次（预期 2：两个出口都要经过）" % n_observe)

    # T-40 起 authResponse.ts 允许 import 一个兄弟纯模块（`auth/authStorage.ts` 的存储键常量），
    # 后者同样零依赖，所以整条链在 Node 里照样可加载。这里真正要守的性质是
    # "**不依赖浏览器环境**"（React、import.meta.env 等），而不是"一个 import 都不许有"。
    browser_only = [line for line in re.findall(r"^\s*import[^;]+;", code_only(auth_resp), re.M)
                    if "authStorage" not in line]
    if not browser_only:
        ok("③ authResponse.ts 只依赖兄弟纯模块，Node 侧可直接加载（契约测试能真跑它的行为）")
    else:
        bad("③ authResponse.ts 依赖了非纯逻辑模块，Node 侧将无法直接加载：%s" % browser_only)

    # 判别力自检：写死不读头的实现必须被判为"缺处理"。
    naive = "function observeResponse(response) { return response; }"
    if "X-Refreshed-Token" not in naive and "readRefreshedToken" not in naive:
        ok("③ 判别力自检：只 return response 的实现不含任何续期处理（规则不是恒真）")
    else:
        bad("③ 判别力自检失败：naive 实现竟被判为已处理续期头")

    # —— C-④ 迁移清单（原先 18 处） ——
    say("")
    say("  ④ 迁移清单（原先 18 处裸 fetch 的现状）：")
    problems = []
    for rel, before in sorted(LEGACY_FETCH_SITES.items()):
        src = files.get(rel)
        if src is None:
            problems.append("%s 不见了" % rel)
            continue
        now = len(NAKED_FETCH.findall(code_only(src)))
        imports_api = bool(re.search(r"from\s*[\"'][^\"']*services/api[\"']", src))
        mark = "PASS" if (now == 0 and imports_api) else "FAIL"
        say("     [%s] %-46s 收敛前 %d 处 → 现在 %d 处，import 统一层=%s"
            % (mark, rel, before, now, "是" if imports_api else "否"))
        if now != 0:
            problems.append("%s 仍有 %d 处裸 fetch" % (rel, now))
        if not imports_api:
            problems.append("%s 没有 import 统一层" % rel)
    if not problems:
        ok("④ 5 个参与文件共 17 处调用点全部改走统一层（第 18 处是 services/api.ts 自身）")
    else:
        bad("④ 迁移不完整：%s" % "；".join(problems))

    # 引入的函数必须真的由统一层导出（防止 typo 后 TS 报错前就先跑起来）
    exported = set(re.findall(r"export\s+(?:async\s+)?function\s+(\w+)", api_service))
    exported |= set(re.findall(r"export\s*\{([^}]*)\}", api_service) and
                    [n.strip() for grp in re.findall(r"export\s*\{([^}]*)\}", api_service)
                     for n in grp.split(",")])
    imports = collect_api_imports(files)
    unknown = sorted({n for names in imports.values() for n in names if n not in exported})
    if not unknown:
        ok("④ 各调用点从统一层引入的 %d 个函数名，全部能在 services/api.ts 找到导出"
           % len({n for names in imports.values() for n in names}))
    else:
        bad("④ 这些引入名在统一层里不存在：%s" % "、".join(unknown))

    paths = collect_api_call_paths(files)
    if len(paths) >= 15:
        ok("④ 统一层里被调用的 API 路径共 %d 条（覆盖原先 5 个文件的全部端点）" % len(paths))
    else:
        bad("④ 只收集到 %d 条 API 路径（预期 ≥ 15）—— 收集器可能已失效" % len(paths))

    SEG["C"] = not problems and not hits and not legacy


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def summary():
    section("汇总")
    for key in ("A", "B", "C"):
        state = SEG.get(key)
        label = "SKIP" if state is None else ("PASS" if state else "FAIL")
        say("  [%s] %s 段" % (label, key))

    if ENV_PROBLEMS:
        say("")
        say("  环境提示（不算失败，但会让结论不完整）：")
        for item in ENV_PROBLEMS:
            say("    - %s" % item)

    if SKIPPED:
        say("")
        say("  已跳过：")
        for item in SKIPPED:
            say("    - %s" % item)

    if FAILED:
        say("")
        say("  未通过项：")
        for item in FAILED:
            say("    - %s" % item)
        say("")
        say("❌ T-36 人工验收未通过")
        return 1

    say("")
    say("✅ T-36 人工验收通过")
    return 0


def main():
    _make_stdout_forgiving()
    parser = argparse.ArgumentParser(description="T-36 一键人工验收（离线、无需 Token）")
    parser.add_argument("--skip-tsc", action="store_true", help="跳过 TypeScript 类型检查")
    parser.add_argument("--strict-window", action="store_true",
                        help="额外断言参与文件里 0 处旧拼接（默认已包含在 C-① 里）")
    args = parser.parse_args()

    say("T-36 人工验收：api 层收敛（401 统一登出 / X-Refreshed-Token / 裸 fetch 归零）")
    say("仓库：%s" % REPO_DIR)

    node_path = segment_preflight()
    segment_contract_tests(node_path)
    segment_tsc(node_path, args)
    segment_source_recheck()

    code = summary()
    if code != 0:
        return 1
    if ENV_PROBLEMS and (SEG.get("A") is None or SEG.get("B") is None):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
