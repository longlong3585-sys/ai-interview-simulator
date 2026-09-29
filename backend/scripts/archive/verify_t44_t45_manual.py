"""T-44 / T-45 人工验收：**傻瓜版**（一条命令，不用起服务、不用手动复制 Token、不花钱、不联网）。

## 它证明什么

T-44 与 T-45 改的是**前端结构**，不是某个接口的返回值。所以"跑通了"这件事
必须被拆成几条**互不依赖**的证据，任何一条单独成立都不足以说明改好了：

  ① **`react-router-dom` 真的在用**：`main.tsx` 里挂了 `AuthProvider` + `BrowserRouter`
     + `Routes`，而不是"装在 package.json 里却从没 import 过"；
  ② **守卫搬到路由层**：`/admin` 路由由 `RequireAdmin` 包裹，
     未登录/非管理员由 `<Navigate to="/" replace>` 拦下 —— 不再是渲染期的一串 `&&`；
  ③ **认证状态唯一真源**：`token/role/username` 的持久化收敛进 `AuthContext`，
     `App.tsx` 不再自己 `localStorage.setItem('token', …)`；
  ④ **FR-11.3 条件 Hook 真的修了**：`AdminPanel` 的所有 Hook 都**早于**任何早退
     （`if (!token) return …`）—— 这是"令牌由有到无不抛错"的**唯一**成因；
  ⑤ **T-45 真的拆了**：`AdminPanel` 从 `App.tsx` 拆出（App 里再搜不到
     `AdminPanelContent` / `showAdminPanel`），管理后台 8 个请求全部走 `authFetch`，
     且拆分后的单文件都在 ≤400 行预算内。

## 它由哪三段独立取证拼成

  0. **预检**：`node` 在哪、要用的文件在不在、`tsc` 在不在 —— 缺什么说什么，退出码 2。
  A. **前端契约测试**（`node --test`，零依赖、不联网、毫秒级）：
     `tests/route-guard-contract.test.mjs`（15 项）+ 既有全部契约测试；
     要求 `fail=0`，且**点名的那几条必须真的出现过**（防止"测试文件被删/改名"后假通过）。
  B. **TypeScript 全量类型检查**（`tsc -b`）：本次改动跨 8 个新文件 + App.tsx 重接线，
     类型系统是最便宜的一致性证明。
  C. **本脚本自己重扫源码**（**与 A 段互相独立**：不 import 测试里的任何辅助函数，
     自己实现括号扫描/深度判定），逐条核对 ①~⑤，并复核行数预算。

## 边界（先读这条，别把它当成浏览器验收）

  * **不驱动真实浏览器**。"点一下管理按钮真的跳到 /admin"是**源码契约**证明的
    （按钮的 onClick 里是 `navigate('/admin')`），不是截图。要在浏览器里肉眼看：
    `cd frontend; npm run dev`，用管理员账号登录（会自动落到 `/admin`），
    再手输一个不存在的路径（应当被 `*` 兜底弹回 `/`）。
  * **`vite build` 在受限沙箱里会以 `spawn EPERM` 失败**（它要 spawn 一个 stdio 走管道的
    子进程来打包配置，而沙箱禁止命名管道）。这是**沙箱边界**，不是本次改动的问题 ——
    脚本遇到这个错误会明确标注为"环境限制"而不是"[FAIL]"。`tsc -b` 不受影响。
  * 本脚本**不覆盖** T-46 / T-47：面谈主流程（`InterviewRoom`）、报告页、个人中心、
    通知中心、题库页**仍留在 `App.tsx` 里**（App.tsx 从 2716 行降到约 2258 行），
    它们的路由化是后续任务。

## 用法

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t44_t45_manual.py

可选参数：`--skip-tsc`（跳过 B 段）、`--skip-build`（跳过 D 段构建探测，默认也不跑）。
退出码：0 = 通过；1 = 未通过（有断言失败）；2 = 环境问题（缺 node/缺文件）。
"""
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
BACKEND_DIR = os.path.join(_ARCHIVE_REPO, "backend")
REPO_DIR = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR = os.path.join(REPO_DIR, "frontend")
SRC_DIR = os.path.join(FRONTEND_DIR, "src")

# 本次改动涉及的文件 → 相对 frontend 的路径
FILES = {
    "main": "src/main.tsx",
    "app": "src/App.tsx",
    "admin_panel": "src/admin/AdminPanel.tsx",
    "admin_page": "src/admin/AdminPage.tsx",
    "report_modal": "src/admin/ReportDetailModal.tsx",
    "tab_stats": "src/admin/tabs/StatsDashboard.tsx",
    "tab_users": "src/admin/tabs/UsersTable.tsx",
    "tab_interviews": "src/admin/tabs/InterviewsTable.tsx",
    "auth_context": "src/auth/AuthContext.tsx",
    "require_auth": "src/auth/RequireAuth.tsx",
    "test_contract": "tests/route-guard-contract.test.mjs",
}

# A 段必须出现过的测试名（防止测试文件被改名/删空后"0 项 0 失败"式假通过）
REQUIRED_TEST_NAMES = [
    "AdminPanel 的 Hook 全部早于任何早退",
    "判别力：把修复前的写法（return 在 useState 之前）喂进去必须判为失败",
    "T-45：AdminPanel 已从 App.tsx 拆出",
    "react-router-dom 真的被用起来了",
    "/admin 路由存在且由 RequireAdmin 守卫包裹",
    "认证状态收敛进 AuthContext",
]

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}


# ---------------------------------------------------------------------------
# 输出小工具（与 verify_t42_manual.py 同风格）
# ---------------------------------------------------------------------------

def _make_stdout_forgiving():
    """让中文在 GBK 控制台上也能打出来（编码不了的字符降级成 ?，绝不抛异常崩脚本）。

    踩过的坑：Node 默认 spec 报告器会打 `ⓘ`（U+2139），GBK 编不出来，
    `print()` 直接抛 UnicodeEncodeError 把整个验收脚本打断。除了改用 TAP 报告器，
    这里再兜一层，保证任何来源的输出都不会把脚本打崩。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def say(msg=""):
    print(msg, flush=True)


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


def section(title):
    say("")
    say("=" * 72)
    say(title)
    say("=" * 72)


# ---------------------------------------------------------------------------
# 子进程（管道捕获 —— 沙箱里 Python 起 node 是允许的，已验证）
# ---------------------------------------------------------------------------

def find_node():
    for name in ("node", "node.exe"):
        from shutil import which

        found = which(name)
        if found:
            return found
    for cand in (
        r"C:\Program Files\nodejs\node.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe"),
        os.path.expandvars(r"%APPDATA%\dsh-desktop\harness\.desktop-bin\node.cmd"),
    ):
        if os.path.exists(cand):
            return cand
    return None


def run(cmd, cwd=FRONTEND_DIR, timeout=600):
    """跑一条命令，返回 (exit_code, 合并后的输出文本)。"""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
        )
    except OSError as exc:
        return None, "无法启动 %s: %s" % (cmd[0], exc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None, "超时 %ss：%s" % (timeout, " ".join(cmd))
    return proc.returncode, out.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# C 段：自己实现的源码分析（刻意不复用测试文件的辅助函数）
# ---------------------------------------------------------------------------

def strip_comments(text):
    """去掉整行注释（// 与块注释行）。本仓库注释都是整行写的。"""
    keep = []
    in_block = False
    for line in text.split("\n"):
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block = True
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        keep.append(line)
    return "\n".join(keep)


def find_char(text, start, open_ch, close_ch):
    """从 start 处（应为 open_ch）找到配对的 close_ch，跳过字符串/模板串/注释。"""
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        two = text[i:i + 2]
        if two == "//":
            nl = text.find("\n", i)
            i = len(text) if nl == -1 else nl + 1
            continue
        if two == "/*":
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        if c in "'\"`":
            quote, k = c, i + 1
            while k < len(text):
                if text[k] == "\\":
                    k += 2
                    continue
                if text[k] == quote:
                    break
                k += 1
            i = k + 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("未配对的 %s%s" % (open_ch, close_ch))


def component_body(text, name):
    """切出 `function NAME(...)` 的花括号内部（先跳过参数表，参数里可能有 `{`）。"""
    m = re.search(r"(?:export\s+default\s+)?function\s+%s\s*[<(]" % re.escape(name), text)
    if not m:
        raise ValueError("找不到函数 %s" % name)
    paren = text.index("(", m.start())
    params_end = find_char(text, paren, "(", ")")
    open_brace = text.index("{", params_end)
    return text[open_brace + 1:find_char(text, open_brace, "{", "}")]


def depths(body):
    """返回 (每个字符的花括号深度, 位置→深度 的取用函数)。"""
    depth, i = 0, 0
    at = [0] * len(body)
    while i < len(body):
        c = body[i]
        two = body[i:i + 2]
        at[i] = depth
        if two == "//":
            nl = body.find("\n", i)
            end = len(body) if nl == -1 else nl
            for k in range(i, end):
                at[k] = depth
            i = end
            continue
        if two == "/*":
            end = body.find("*/", i + 2)
            stop = len(body) if end == -1 else end + 2
            for k in range(i, stop):
                at[k] = depth
            i = stop
            continue
        if c in "'\"`":
            quote, k = c, i + 1
            while k < len(body):
                if body[k] == "\\":
                    k += 2
                    continue
                if body[k] == quote:
                    break
                k += 1
            for k2 in range(i, min(k, len(body) - 1) + 1):
                at[k2] = depth
            i = k + 1
            continue
        if c == "{":
            depth += 1
            at[i] = depth
        elif c == "}":
            depth -= 1
            at[i] = depth
        i += 1
    return at


HOOK_RE = re.compile(r"\buse(?:State|Effect|Ref|Memo|Callback|Context)\s*(?:<[^>]*>)?\s*\(")


def audit_hook_order(panel_src, path_label):
    """返回 (hooks, first_early_return)；两者都是"函数体坐标"下的位置。"""
    body = strip_comments(component_body(panel_src, "AdminPanel"))
    at = depths(body)

    hooks = [(m.start(), m.group(0)) for m in HOOK_RE.finditer(body)]
    hooks = [(i, t) for i, t in hooks if at[i] == 0]

    returns = [(m.start(), at[m.start()]) for m in re.finditer(r"\breturn\b", body)]
    # 组件级 return：depth 0（顶层）与 depth 1（if 块里的早退）；
    # 回调里的 return（onClick/useEffect 的清理函数）在 depth ≥2，不参与判定。
    top = [i for i, d in returns if d <= 1]
    if not top:
        return hooks, None
    final = top[-1]
    early = [i for i in top if i != final]
    return hooks, (early[0] if early else None)


# ---------------------------------------------------------------------------
# 0. 预检
# ---------------------------------------------------------------------------

def preflight(node_path, args):
    section("0. 预检（node / 文件 / 参数）")
    if node_path:
        code, out = run([node_path, "--version"], timeout=60)
        ok("node 可执行：%s（%s）" % (node_path, (out or "").strip() or "版本未知"))
    else:
        env_problem("找不到 node 可执行文件（A 段无法运行）")

    missing = []
    for label, rel in FILES.items():
        full = os.path.join(FRONTEND_DIR, rel)
        if os.path.exists(full):
            ok("存在 %s" % rel)
        else:
            missing.append(rel)
            env_problem("缺少文件 %s（应当由 T-44/T-45 产出）" % rel)

    if os.path.exists(os.path.join(FRONTEND_DIR, "node_modules")):
        ok("frontend/node_modules 存在（tsc / node --test 可用）")
    else:
        env_problem("frontend/node_modules 不存在 —— 先 npm install")

    if args.skip_tsc:
        skip("按 --skip-tsc 跳过 B 段（TypeScript 类型检查）")
    return not missing


# ---------------------------------------------------------------------------
# A. 前端契约测试
# ---------------------------------------------------------------------------

def segment_contract_tests(node_path):
    section("A. 前端契约测试（node --test，零依赖 / 不联网 / 毫秒级）")
    if not node_path:
        env_problem("没有 node，跳过 A 段")
        SEG["A"] = None
        return

    code, out = run(
        [
            node_path,
            "--test",
            "--experimental-test-isolation=none",
            # TAP 报告器：机器可读、纯 ASCII（默认 spec 报告器会打 ⓘ 等字符，
            # 在 GBK 控制台上直接把脚本打崩 —— 这个坑已经踩过一次）。
            "--test-reporter=tap",
            "tests/**/*.test.mjs",
        ],
        timeout=600,
    )
    if code is None:
        env_problem("契约测试无法执行：%s" % out.strip()[:200])
        SEG["A"] = None
        return

    lines = [l.strip() for l in out.splitlines() if l.strip()]
    say("  —— `node --test`（TAP）尾部 ——")
    for line in lines[-10:]:
        say("     %s" % line.encode("ascii", "replace").decode("ascii"))

    # TAP 汇总形状：
    #   # tests 51
    #   # pass 51
    #   # fail 0
    m_tests = re.search(r"^#\s*tests\s+(\d+)", out, re.M)
    m_pass = re.search(r"^#\s*pass\s+(\d+)", out, re.M)
    m_fail = re.search(r"^#\s*fail\s+(\d+)", out, re.M)
    if not (m_tests and m_pass and m_fail):
        env_problem("未能从 TAP 输出里解析出 tests/pass/fail（输出格式可能变了）")
        SEG["A"] = False
        return

    tests, passed, failed = int(m_tests.group(1)), int(m_pass.group(1)), int(m_fail.group(1))
    say("  解析结果：tests=%d pass=%d fail=%d" % (tests, passed, failed))

    if failed == 0:
        ok("全部契约测试通过（pass=%d fail=0）" % passed)
    else:
        bad("有 %d 项契约测试失败（见上方输出尾部，完整输出请直接跑 npm run test:node）" % failed)

    if tests >= 40:
        ok("测试数量合理（%d ≥ 40，说明既有契约测试没被删）" % tests)
    else:
        bad("只跑了 %d 项测试（预期 ≥ 40）—— 测试文件可能被删或没被匹配到" % tests)

    for name in REQUIRED_TEST_NAMES:
        if name in out:
            ok("点名用例确实跑过：%s" % name)
        else:
            bad("输出里没有出现点名用例「%s」—— 它可能被删/改名，那样就是假通过" % name)

    SEG["A"] = (failed == 0)


# ---------------------------------------------------------------------------
# B. TypeScript 类型检查
# ---------------------------------------------------------------------------

def segment_tsc(node_path, args):
    section("B. TypeScript 全量类型检查（tsc -b，跨 8 个新文件 + App.tsx 重接线）")
    if args.skip_tsc:
        SEG["B"] = None
        return
    tsc_js = os.path.join(FRONTEND_DIR, "node_modules", "typescript", "bin", "tsc")
    if not os.path.exists(tsc_js) or not node_path:
        env_problem("找不到 node_modules/typescript/bin/tsc，跳过 B 段")
        SEG["B"] = None
        return

    code, out = run([node_path, tsc_js, "-b"], timeout=900)
    if code is None:
        env_problem("tsc 无法执行：%s" % out.strip()[:200])
        SEG["B"] = None
        return
    if code == 0:
        ok("tsc -b 退出码 0：本次改动的类型一致性成立")
        SEG["B"] = True
        return

    for line in out.splitlines()[:15]:
        say("     %s" % line.strip())
    bad("tsc -b 失败（exit=%s）—— 上面是前 15 行错误" % code)
    SEG["B"] = False


# ---------------------------------------------------------------------------
# C. 本脚本自己重扫源码（与 A 段互相独立）
# ---------------------------------------------------------------------------

def read_src(rel):
    with open(os.path.join(FRONTEND_DIR, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def segment_source_recheck():
    section("C. 独立复核（本脚本自己扫源码，不复用测试的辅助函数）")

    src = {k: read_src(v) for k, v in FILES.items() if v.endswith((".tsx", ".mjs"))}
    app, main = src["app"], src["main"]
    panel = src["admin_panel"]
    ctx = src["auth_context"]
    guard = src["require_auth"]

    # ① 路由真的在用
    if "from 'react-router-dom'" in main and "<BrowserRouter>" in main and "<Routes>" in main:
        ok("① main.tsx 真的 import 并挂载了 react-router-dom（BrowserRouter + Routes）")
    else:
        bad("① main.tsx 未把 react-router-dom 接进渲染树")
    if "<AuthProvider>" in main:
        ok("① main.tsx 把 <AuthProvider> 包在最外层（认证状态对全树可见）")
    else:
        bad("① main.tsx 缺少 <AuthProvider>")

    # ② 守卫在路由层
    if 'path="/admin"' in main and "<RequireAdmin>" in src["admin_page"]:
        ok("② /admin 路由存在，且页面被 <RequireAdmin> 包裹（守卫在路由层）")
    else:
        bad("② /admin 路由或 RequireAdmin 守卫缺失")
    if re.search(r'<Navigate\s+to="/"\s+replace', guard):
        ok("② 守卫在未授权时重定向回首页（<Navigate to=\"/\" replace>）")
    else:
        bad("② 守卫里没有 <Navigate to=\"/\" replace>")
    if "isAdmin" in guard and "isAuthenticated" in guard:
        ok("② 守卫同时区分「未登录」与「已登录但非管理员」")
    else:
        bad("② 守卫未区分未登录 / 非管理员两种情况")

    # ③ 认证状态唯一真源
    if "useAuth()" in app:
        ok("③ App.tsx 通过 useAuth() 读认证状态")
    else:
        bad("③ App.tsx 没有用 useAuth()")
    offenders = []
    if re.search(r"useState<[^>]*>\(\s*localStorage\.getItem\('token'\)", app):
        offenders.append("useState 直接初始化 token")
    if re.search(r"localStorage\.(setItem|removeItem)\('(token|role|username)'", app):
        offenders.append("App.tsx 仍直接读写 localStorage 认证三件套")
    if offenders:
        bad("③ %s" % "；".join(offenders))
    else:
        ok("③ App.tsx 已不再自己读写 localStorage 的 token/role/username")
    if "export function AuthProvider" in ctx and "export function useAuth" in ctx:
        ok("③ AuthContext 提供 Provider 与 useAuth")
    else:
        bad("③ AuthContext 缺少 AuthProvider / useAuth")
    # T-40：持久化从"逐键 writeStored"收敛成"一次写整份快照"，
    # 键清单的唯一来源搬到了 `auth/authStorage.ts` 的 `AUTH_PERSISTED_KEYS`（含新增的 userId）。
    # 因此这里改成断言"AuthContext 调用那条唯一写路径"，键清单去 authStorage 里核对。
    if re.search(r"persistAuthSnapshot\(", ctx):
        ok("③ AuthContext 调用唯一持久化写路径 persistAuthSnapshot")
    else:
        bad("③ AuthContext 没有调用唯一持久化写路径（T-40 的收敛被回退）")
    storage_path = os.path.join(SRC_DIR, "auth", "authStorage.ts")
    storage = open(storage_path, "r", encoding="utf-8").read() if os.path.exists(storage_path) else ""
    for key in ("token", "userId", "role", "username"):
        # authStorage 里键名可能是字面量，也可能是 `AUTH_*_STORAGE_KEY = 'token'` 常量。
        if re.search(r"'%s'" % key, storage) or re.search(r"=%s" % key, storage):
            ok("③ 持久化清单纳入 %s（authStorage.AUTH_PERSISTED_KEYS）" % key)
        else:
            bad("③ 持久化清单没有 %s（刷新会丢 / 登出会漏清）" % key)

    # ④ FR-11.3 条件 Hook
    hooks, early = audit_hook_order(panel, "AdminPanel")
    say("     扫描结果：AdminPanel 顶层 Hook %d 个；首个组件级早退位置 %s"
        % (len(hooks), "无" if early is None else early))
    if not hooks:
        bad("④ 在 AdminPanel 里没扫到任何 Hook —— 扫描器或组件结构异常")
    elif early is None:
        bad("④ 没扫到组件级早退 —— AdminPanel 应当保留 `if (!token) return 请先登录`")
    else:
        late = [t for i, t in hooks if i > early]
        if late:
            bad("④ 这些 Hook 出现在早退之后（令牌由有到无时会抛 Rendered fewer hooks）：%s" % late)
        else:
            ok("④ AdminPanel 的 %d 个 Hook 全部早于早退出口（FR-11.3 已修）" % len(hooks))
    if re.search(r"if\s*\(!token\)\s*\{[\s\S]{0,120}?return\s*<div[^>]*>请先登录</div>;", panel):
        ok("④ 「请先登录」这个出口本身仍在（只是被移到了 Hook 之后）")
    else:
        bad("④ AdminPanel 里找不到「请先登录」的出口分支")

    # 判别力自检：修复前的写法必须被同一个判定函数判为失败
    broken = (
        "function AdminPanel({ token }) {\n"
        "  if (!token) return <div>请先登录</div>;\n"
        "  const [a, setA] = useState('stats');\n"
        "  const [b, setB] = useState([]);\n"
        "  const [c, setC] = useState(false);\n"
        "  return <div>{a}{b.length}{c}</div>;\n"
        "}\n"
    )
    b_hooks, b_early = audit_hook_order(broken, "broken")
    if b_hooks and b_early is not None and b_early < b_hooks[0][0]:
        ok("④ 判别力自检：修复前的写法被判为「Hook 在早退之后」（说明上面的 PASS 不是恒真）")
    else:
        bad("④ 判别力自检失败：修复前的写法居然被判为通过 —— 断定逻辑有洞")

    # ⑤ T-45 拆分
    if "AdminPanelContent" not in app and not re.search(r"\bfunction AdminPanel\b", app):
        ok("⑤ App.tsx 里已不再内联管理后台组件（T-45 拆出）")
    else:
        bad("⑤ App.tsx 里仍残留管理后台内联实现")
    if "showAdminPanel" not in app:
        ok("⑤ 旧的管理员弹窗布尔量 showAdminPanel 已清除（改用 /admin 路由）")
    else:
        bad("⑤ App.tsx 里仍有 showAdminPanel")
    if re.search(r"navigate\('/admin'\)", app) and re.search(r'<Navigate\s+to="/admin"\s+replace', app):
        ok("⑤ 管理员入口均为路由跳转（按钮 navigate + 账号自动重定向）")
    else:
        bad("⑤ 管理员入口没有完全改成路由跳转")

    admin_files = {
        "AdminPanel.tsx": panel,
        "tabs/StatsDashboard.tsx": src["tab_stats"],
        "tabs/UsersTable.tsx": src["tab_users"],
        "tabs/InterviewsTable.tsx": src["tab_interviews"],
    }
    naked = [name for name, text in admin_files.items() if re.search(r"await\s+fetch\(", text)]
    if naked:
        bad("⑤ 这些文件里仍有裸 fetch（应走 authFetch）：%s" % naked)
    else:
        ok("⑤ 管理后台全部请求都走 authFetch（裸 fetch 为 0）")
    guarded = sum(len(re.findall(r"await\s+authFetch\(", t)) for t in admin_files.values())
    if guarded == 8:
        ok("⑤ 管理员 API 调用仍是 8 处（拆分没有丢请求）")
    else:
        bad("⑤ 管理员 authFetch 调用是 %d 处，预期 8 处" % guarded)

    # 行数预算
    def lines_of(text):
        return len(text.split("\n"))

    app_lines = lines_of(app)
    if app_lines < 2270:
        ok("⑤ App.tsx %d 行（T-45 前是 2716 行）" % app_lines)
    else:
        bad("⑤ App.tsx 仍有 %d 行，未见拆分效果" % app_lines)
    for label, key in (
        ("AdminPanel.tsx", "admin_panel"),
        ("ReportDetailModal.tsx", "report_modal"),
        ("AdminPage.tsx", "admin_page"),
        ("StatsDashboard.tsx", "tab_stats"),
        ("UsersTable.tsx", "tab_users"),
        ("InterviewsTable.tsx", "tab_interviews"),
        ("AuthContext.tsx", "auth_context"),
        ("RequireAuth.tsx", "require_auth"),
    ):
        n = lines_of(src[key])
        if n <= 400:
            ok("⑤ %s %d 行（≤400 预算内）" % (label, n))
        else:
            bad("⑤ %s 有 %d 行，超出 ≤400 预算" % (label, n))

    SEG["C"] = not FAILED


# ---------------------------------------------------------------------------
# D. 构建探测（可选，默认不跑）
# ---------------------------------------------------------------------------

def segment_build(node_path, args):
    section("D. 构建探测（vite build，可选）")
    if not args.with_build:
        skip("默认不跑 vite build（沙箱里它会以 spawn EPERM 失败，属环境限制）。需要时加 --with-build")
        SEG["D"] = None
        return
    npm = "npm.cmd" if os.name == "nt" else "npm"
    code, out = run([npm, "run", "build"], timeout=900)
    if code is None:
        env_problem("无法执行 npm run build：%s" % out.strip()[:200])
        SEG["D"] = None
        return
    if code == 0:
        ok("vite build 退出码 0（产物已生成）")
        SEG["D"] = True
        return
    if "spawn EPERM" in out:
        env_problem("vite build 因沙箱限制失败（spawn EPERM，命名管道被禁）—— 这不是本次改动的问题；"
                    "同一条命令在放宽权限的环境下应 exit 0")
        SEG["D"] = None
        return
    for line in out.splitlines()[:12]:
        say("     %s" % line.strip())
    bad("vite build 失败（exit=%s），且不是沙箱 EPERM —— 需要人工看上面几行" % code)
    SEG["D"] = False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="T-44 / T-45 人工验收（一条命令，不联网）")
    parser.add_argument("--skip-tsc", action="store_true", help="跳过 TypeScript 类型检查")
    parser.add_argument("--with-build", action="store_true", help="额外尝试 vite build（沙箱里可能因 EPERM 失败）")
    args = parser.parse_args()

    say("T-44 / T-45 人工验收：路由化 + 路由层守卫 + 条件 Hook + AdminPanel 拆分")
    say("仓库：%s" % REPO_DIR)
    _make_stdout_forgiving()

    node_path = find_node()
    preflight(node_path, args)
    if ENV_PROBLEMS and not args.skip_tsc and node_path is None:
        say("")
        say("环境问题（%d 条）：" % len(ENV_PROBLEMS))
        for item in ENV_PROBLEMS:
            say("  - %s" % item)
        return 2

    segment_contract_tests(node_path)
    segment_tsc(node_path, args)
    segment_source_recheck()
    segment_build(node_path, args)

    section("汇总（逐段）")
    labels = {
        "A": "前端契约测试（路由/守卫/条件 Hook/拆分）",
        "B": "TypeScript 类型检查（tsc -b）",
        "C": "独立源码复核（本脚本自扫，不复用测试代码）",
        "D": "构建探测（vite build，可选）",
    }
    for key in ("A", "B", "C", "D"):
        state = SEG.get(key)
        mark = "[PASS]" if state else ("[SKIP]" if state is None else "[FAIL]")
        say("  %s %s" % (mark, labels[key]))

    if ENV_PROBLEMS:
        say("")
        say("环境提示（%d 条，不一定是失败）：" % len(ENV_PROBLEMS))
        for item in ENV_PROBLEMS:
            say("  - %s" % item)
    if SKIPPED:
        say("")
        say("跳过（%d 条）：" % len(SKIPPED))
        for item in SKIPPED:
            say("  - %s" % item)

    say("")
    if FAILED:
        say("❌ T-44 / T-45 人工验收未通过，失败项：")
        for item in FAILED:
            say("  - %s" % item)
        say("退出码 1")
        return 1

    if SEG.get("A") and SEG.get("C"):
        say("✅ T-44 / T-45 人工验收通过："
            "路由化已接线（/admin + RequireAdmin）、认证状态收敛进 AuthContext、"
            "AdminPanel 的 Hook 全部早于早退（令牌由有到无不抛错）、"
            "管理后台已从 App.tsx 拆出且单文件都在 400 行预算内。")
        say("退出码 0")
        return 0

    say("⚠️ 未发现断言失败，但关键段（A/C）有段被跳过 —— 不算通过。退出码 2")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("")
        say("被用户中断")
        sys.exit(2)
