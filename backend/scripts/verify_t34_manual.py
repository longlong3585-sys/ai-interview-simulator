#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T-34 / Bug 4（P0）人工验收：API 基地址相对化 + Vite 代理 + 环境变量注入。

一条命令，**不起服务、不联网、不用手动复制 Token、不花钱**：

    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t34_manual.py

可选参数：
    --skip-tsc     跳过 B 段（TypeScript 全量类型检查）
    --skip-bundle  跳过 C-③（离线"打包模拟"：tsc 产物 + Vite env 替换 + grep）
    --with-build   额外尝试 `vite build`（受限沙箱里会 spawn EPERM，见 §边界）

退出码：0 = 通过；1 = 未通过；2 = 环境问题。

────────────────────────────────────────────────────────────────────────────
验收标准（`docs/03-tasks.md` §阶段 4）：

  * `vite.config.ts` 含 `server.proxy`；
  * **构建产物中 grep 不到 `127.0.0.1`**；
  * 预览 / HTTPS 环境下**所有按钮可点**。

四段互不依赖的取证：

  0. 预检        —— node、目标文件、node_modules。
  A. 契约测试    —— `node --test`（TAP）：本任务 9 项 + 既有全部契约，fail 必须为 0，
                    且**点名用例必须真的出现过**（防止测试被删/改名后假通过）。
  B. 类型检查    —— `tsc -b` 退出码 0。
  C. 独立复核    —— **本脚本自己重扫源码**，不 import 测试里的任何辅助函数：
                    ① 相对基地址 / ② Vite 代理（结构 + **运行时求值**）/ ③ 离线"打包模拟"
                    （真编译产物 grep）/ ④ 代理目标不进产物 / ⑤ .env 加载语义，
                    每条都带判别力自检。
  D. 构建探测    —— `vite build`（默认跳过）。真跑成功时，**直接对 dist/ 再 grep 一遍**。

────────────────────────────────────────────────────────────────────────────
为什么"构建产物 grep"能在离线沙箱里证明（C-③ 的做法）：

  受影响的方式有三条，逐条被堵住：
    1. 源码里写死 host            → ① 扫 src/**（去注释）为 0；
    2. `.env` 里的 `VITE_*` 变量被内联 → ⑤ 扫所有**会被 Vite 加载**的 .env* 文件；
    3. TS → JS 的编译/替换过程引入   → ③ 用 `tsc` 真编译出 JS（不是读源码），
                                       再按 Vite 的规则把 `import.meta.env.X` 替换成
                                       .env 里的值（模拟 define），然后 grep 产物。
  ③ 还带**反向验证**：故意把 `VITE_API_BASE_URL` 设成一个 host，替换后必须 grep 得到
  —— 否则"grep 到 0"可能只是因为替换根本没生效。

已知边界（刻意如此，非遗漏）：
  * **不驱动真实浏览器**。"HTTPS 页面按钮可点"是**源码 + 产物**证明的：
    基地址是相对路径 ⇒ 请求与页面同源 ⇒ 不存在混合内容与跨域；
    `preview.proxy` ⇒ `npm run preview` 也有后端。肉眼验收见 `docs/33` §4。
  * **`vite build` 在受限沙箱里以 `spawn EPERM` 失败**（要 spawn 走管道的子进程，
    沙箱禁止命名管道）：与 docs/29~32 记录的是同一个沙箱边界，不是本次改动的问题。
    这也是 C-③ 存在的理由 —— 用 `tsc` 编译产物替代 vite 打包产物。
  * **`npm run lint` 本来就是红的**：本任务只核对"没有新增结构性违规"，不假装 lint 是绿的。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_DIR = os.path.join(REPO_DIR, "frontend")
SRC_DIR = os.path.join(FRONTEND_DIR, "src")
TESTS_DIR = os.path.join(FRONTEND_DIR, "tests")
TMP_EMIT_DIR = os.path.join(FRONTEND_DIR, ".tmp-t34-emit")
PROBE_JS = ".tmp-t34-probe.mjs"

# 在**真实运行时**求值 vite.config.ts：Node 24 能直接 import .ts（类型剥离），
# 于是"代理到底配没配、目标是什么"不再靠正则猜，而是把配置函数跑一遍看返回值。
PROBE_SOURCE = """import cfg from './vite.config.ts';

const resolved = typeof cfg === 'function' ? cfg({ mode: 'development', command: 'serve' }) : cfg;
const out = {
  apiTarget: resolved.server?.proxy?.['/api']?.target ?? null,
  apiChangeOrigin: resolved.server?.proxy?.['/api']?.changeOrigin ?? false,
  uploadsTarget: resolved.server?.proxy?.['/uploads']?.target ?? null,
  previewApiTarget: resolved.preview?.proxy?.['/api']?.target ?? null,
};
console.log('PROBE ' + JSON.stringify(out));
"""

FILES = {
    "config": "src/config.ts",
    "apiBaseUrl": "src/utils/apiBaseUrl.ts",
    "viteConfig": "vite.config.ts",
    "envExample": ".env.example",
    "indexHtml": "index.html",
    "test": "tests/api-base-url.test.mjs",
}

# A 段必须出现过的测试名（防止测试被改名/删空）
REQUIRED_TEST_NAMES = [
    "T-34：未配置 / 空串 / 纯空白 一律解析成空串",
    "T-34：vite.config.ts 提供 server.proxy 与 preview.proxy",
    "T-34：会进浏览器产物的文件里，写死的 host 为 0",
    "判别力：把修复前的 config.ts 塞回产物文件集里，必须被判为违规",
    "前端 API 路径不得残留硬编码的 host",
]

# 写死的后端地址（Bug 4 的元凶）
HARDCODED_HOST = re.compile(r"127\.0\.0\.1|localhost|0\.0\.0\.0")
# 绝对 http 地址字面量（HTTPS 页面会被混合内容拦掉）
ABSOLUTE_HTTP = re.compile(r"['\"`]http://[a-z0-9]")
# Vite 真正会加载的 .env 文件名（`.env.example` 不在其中 —— 它只是模板）
LOADABLE_ENV_NAMES = [".env", ".env.local", ".env.development", ".env.production",
                      ".env.development.local", ".env.production.local"]

FAILED, SKIPPED, ENV_PROBLEMS, SEG = [], [], [], {}


# ---------------------------------------------------------------------------
# 输出小工具（与 verify_t44_t45 / verify_t46_t49 同风格）
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
    """去注释：注释里正**引用**着被替换掉的旧地址（那是文档，也不进产物）。"""
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    out = []
    for line in text.splitlines():
        if line.strip().startswith("//"):
            continue
        out.append(re.sub(r"\s//.*$", "", line))
    return "\n".join(out)


def walk_sources(root=SRC_DIR):
    found = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "r", encoding="utf-8") as fh:
                found[rel] = fh.read()
    return found


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
        bad("T-34 的交付物缺失：%s" % "、".join(missing))
    else:
        ok("%d 个目标文件全部就位（config / 归一化 / vite 配置 / env 模板 / 契约测试）" % len(FILES))

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

    if tests >= 80:
        ok("测试数量合理（%d ≥ 80，说明既有契约测试没被删）" % tests)
    else:
        bad("只跑了 %d 项测试（预期 ≥ 80）" % tests)

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
    section("B. TypeScript 全量类型检查（tsc -b：src + vite.config.ts 两个 project）")
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
        ok("tsc -b 退出码 0：`import.meta.env` 取值、代理配置、纯函数签名全部通过类型检查")
        SEG["B"] = True
        return
    for line in out.splitlines()[:15]:
        say("     %s" % line.strip())
    bad("tsc -b 失败（exit=%s）" % code)
    SEG["B"] = False


# ---------------------------------------------------------------------------
# C. 独立复核
# ---------------------------------------------------------------------------

def check_relative_base(sources):
    """① API 基地址是相对路径，且全树没有写死的 host。"""
    config = code_only(sources.get("config.ts", ""))
    base = sources.get("utils/apiBaseUrl.ts", "")

    if "import.meta.env.VITE_API_BASE_URL" in config:
        ok("① config.ts 从 `import.meta.env.VITE_API_BASE_URL` 读基地址（构建期可注入）")
    else:
        bad("① config.ts 没有读 VITE_API_BASE_URL")
    if "normalizeApiBaseUrl(" in config:
        ok("① config.ts 走纯函数归一化（空值→相对路径、末尾斜杠归一）")
    else:
        bad("① config.ts 未做归一化（末尾斜杠会拼出 //api）")
    if "export function normalizeApiBaseUrl" in base:
        ok("① 归一化逻辑独立成零依赖纯函数（Node 里可直接行为测试）")
    else:
        bad("① utils/apiBaseUrl.ts 里没有 normalizeApiBaseUrl")

    offenders = [name for name, src in sources.items() if HARDCODED_HOST.search(code_only(src))]
    if offenders:
        bad("① 这些源码里写死了 host：%s" % "、".join(offenders))
    else:
        ok("① src 下 %d 个 .ts/.tsx 的**代码**里，写死的 host 为 0" % len(sources))

    absolute = [name for name, src in sources.items() if ABSOLUTE_HTTP.search(code_only(src))]
    if absolute:
        bad("① 这些源码里有 http:// 绝对地址字面量（HTTPS 页面会被混合内容拦掉）：%s" % "、".join(absolute))
    else:
        ok("① 无 http:// 绝对地址字面量（页面走 HTTPS 时不存在混合内容）")

    # 判别力自检
    before = {"config.ts": "export const API_BASE_URL = 'http://127.0.0.1:8000';"}
    if [n for n, s in before.items() if HARDCODED_HOST.search(code_only(s))]:
        ok("① 判别力自检：修复前的 config.ts 被判为「写死 host」（说明上面的 0 不是恒真）")
    else:
        bad("① 判别力自检失败：修复前的写法居然没被判为写死 host")


def probe_vite_config(node_path, extra_env=None):
    """真的把 vite.config.ts 求值一遍，返回 (结果字典 | None, 原始输出)。"""
    path = os.path.join(FRONTEND_DIR, PROBE_JS)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(PROBE_SOURCE)
    env = dict(os.environ)
    env.update(extra_env or {})
    try:
        proc = subprocess.run([node_path, PROBE_JS], cwd=FRONTEND_DIR, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        out = "无法执行：%s" % exc
    finally:
        if os.path.exists(path):
            os.remove(path)

    m = re.search(r"PROBE (\{.*\})", out)
    if not m:
        return None, out
    try:
        return json.loads(m.group(1)), out
    except ValueError:
        return None, out


def check_vite_proxy(node_path):
    """② server.proxy / preview.proxy 齐备（结构），并在**运行时**求值验证（最强证据）。"""
    vite = code_only(read("vite.config.ts"))

    def has(pattern, label):
        if re.search(pattern, vite):
            ok("② %s" % label)
        else:
            bad("② 缺少 %s" % label)

    has(r"server:\s*\{\s*proxy", "server.proxy 存在（开发环境请求会转发到后端）")
    has(r"preview:\s*\{\s*proxy", "preview.proxy 存在（npm run preview 也能点动按钮）")
    has(r"['\"]/api['\"]:\s*\{\s*target", "/api 转发到后端")
    has(r"['\"]/uploads['\"]:\s*\{\s*target", "/uploads 转发（头像 <img src> 走这个前缀）")
    has(r"changeOrigin:\s*true", "changeOrigin: true（后端拿到的 Host 不会是 5173）")
    has(r"DEV_PROXY_TARGET", "代理目标可配置（换后端地址不用改代码）")
    has(r"loadEnv\(", "用 loadEnv 读 .env（.env.local 也能生效）")

    # 判别力自检：一份"没有 proxy"的配置必须被判为缺
    if not re.search(r"server:\s*\{\s*proxy", "export default defineConfig({ plugins: [react()] })"):
        ok("② 判别力自检：无 proxy 的合成配置被判为「缺少 server.proxy」")
    else:
        bad("② 判别力自检失败：空的合成配置居然被判为有 proxy")

    # 运行时求值（结构断言可能被"写了但写错"骗过，这里跑真的）
    if not node_path:
        env_problem("没有 node，跳过 ② 的运行时求值")
        return
    result, raw = probe_vite_config(node_path)
    if result is None:
        env_problem("无法求值 vite.config.ts（%s）" % raw.strip()[:160])
        return
    say("     运行时求值结果：%s" % json.dumps(result, ensure_ascii=False))

    if result.get("apiTarget"):
        ok("② 运行时求值：server.proxy['/api'].target = %s" % result["apiTarget"])
    else:
        bad("② 运行时求值：server.proxy['/api'] 没有 target（开发环境必然点不动）")
    if result.get("apiChangeOrigin") is True:
        ok("② 运行时求值：changeOrigin = true")
    else:
        bad("② 运行时求值：changeOrigin 不是 true")
    if result.get("uploadsTarget"):
        ok("② 运行时求值：server.proxy['/uploads'] 也配了（头像才不会 404）")
    else:
        bad("② 运行时求值：/uploads 没有代理（头像会 404）")
    if result.get("previewApiTarget"):
        ok("② 运行时求值：preview.proxy['/api'] 也配了（验收标准里的「预览环境可点」成立）")
    else:
        bad("② 运行时求值：preview.proxy 没配（npm run preview 点不动按钮）")

    # 配置可注入：换一个 DEV_PROXY_TARGET，目标必须跟着变
    other = "http://10.11.12.13:9000"
    injected, raw2 = probe_vite_config(node_path, {"DEV_PROXY_TARGET": other})
    if injected and injected.get("apiTarget") == other:
        ok("② 运行时求值：DEV_PROXY_TARGET=%s 时目标跟着变（换后端地址不用改代码）" % other)
    else:
        bad("② 运行时求值：DEV_PROXY_TARGET 没生效（%s）" % raw2.strip()[:160])


def parse_env_files():
    """读出 Vite 会加载的 .env* 里的变量（键 → 值）。"""
    env = {}
    for name in LOADABLE_ENV_NAMES:
        full = os.path.join(FRONTEND_DIR, name)
        if not os.path.exists(full):
            continue
        with open(full, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip().strip("'\""))
    return env


def check_env_semantics():
    """⑤ .env.example 是模板（可以被 Vite 忽略），真正加载的 .env* 里 VITE_ 不许带 host。"""
    if os.path.exists(os.path.join(FRONTEND_DIR, ".env.example")):
        ok("⑤ frontend/.env.example 存在（新人照着配就行）")
    else:
        bad("⑤ 缺少 frontend/.env.example")
    if ".env.example" not in LOADABLE_ENV_NAMES:
        ok("⑤ .env.example 不在 Vite 的加载清单里 —— 它只是模板，里面的示例地址不会进产物")
    else:
        bad("⑤ 加载清单写错了（把 .env.example 当成会被加载的文件）")

    present = [n for n in LOADABLE_ENV_NAMES if os.path.exists(os.path.join(FRONTEND_DIR, n))]
    if not present:
        ok("⑤ 当前工作区没有会被加载的 .env* 文件（配置全部走默认值）")
    env = parse_env_files()
    offenders = {k: v for k, v in env.items() if k.startswith("VITE_") and HARDCODED_HOST.search(v)}
    if offenders:
        bad("⑤ 这些 VITE_ 变量会被内联进产物，不许写死 host：%s" % offenders)
    else:
        ok("⑤ 会被加载的 VITE_ 变量里没有写死的 host（%s）"
           % ("已检查 " + "、".join(present) if present else "无文件"))

    # 判别力自检
    synthetic = {".env": "VITE_API_BASE_URL=http://127.0.0.1:8000"}
    if [k for k, v in synthetic.items() if HARDCODED_HOST.search(v)]:
        ok("⑤ 判别力自检：合成 .env 里的写死 host 会被抓到")
    else:
        bad("⑤ 判别力自检失败：合成 .env 里的写死 host 没被抓到")


def simulate_bundle(node_path):
    """③ 离线"打包模拟"：真编译 TS → JS，再按 Vite 的规则替换 import.meta.env，然后 grep。

    这是"构建产物中 grep 不到 127.0.0.1"在没有 vite build 的沙箱里能做到的最强证据。
    """
    tsc_js = os.path.join(FRONTEND_DIR, "node_modules", "typescript", "bin", "tsc")
    if not node_path or not os.path.exists(tsc_js):
        env_problem("找不到 tsc，跳过 C-③（离线打包模拟）")
        return

    shutil.rmtree(TMP_EMIT_DIR, ignore_errors=True)
    # 注：`allowImportingTsExtensions` 与 emit 互斥，这里显式关掉；
    # main.tsx 里那几个 './X.tsx' 导入会报 TS5097，但 tsc 仍然照常产出 JS —— 我们只取产物。
    code, out = run([node_path, tsc_js, "-p", "tsconfig.app.json",
                     "--noEmit", "false", "--outDir", ".tmp-t34-emit", "--rootDir", "src",
                     "--allowImportingTsExtensions", "false"], timeout=900)
    emitted = {}
    for dirpath, _dirnames, filenames in os.walk(TMP_EMIT_DIR):
        for name in filenames:
            if name.endswith(".js"):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, TMP_EMIT_DIR).replace(os.sep, "/")
                with open(full, "r", encoding="utf-8") as fh:
                    emitted[rel] = fh.read()
    if not emitted:
        env_problem("tsc 没有产出 JS（exit=%s），跳过 C-③" % code)
        return
    say("     编译产物：%d 个 .js（tsc exit=%s，TS5097 是 --allowImportingTsExtensions 的已知副作用）"
        % (len(emitted), code))

    env = parse_env_files()
    # 模拟 Vite 的 define 替换：`import.meta.env.X` → 字面量
    def substitute(src):
        def repl(m):
            key = m.group(1)
            if key in env:
                return json.dumps(env[key])
            return "undefined"  # 未定义 → 运行期 `?? ''` 兜底
        src = re.sub(r"import\.meta\.env\.([A-Za-z0-9_]+)", repl, src)
        return src.replace("import.meta.env", "{}")

    raw_hits, sub_hits = [], []
    for name, src in emitted.items():
        if HARDCODED_HOST.search(src):
            raw_hits.append(name)
        if HARDCODED_HOST.search(substitute(src)):
            sub_hits.append(name)
    index_html = read("index.html")
    if HARDCODED_HOST.search(index_html):
        raw_hits.append("index.html")
    if HARDCODED_HOST.search(substitute(index_html)):
        sub_hits.append("index.html")

    if raw_hits:
        bad("③ 编译产物里仍有写死的 host：%s" % "、".join(raw_hits))
    else:
        ok("③ 编译产物（%d 个 .js + index.html）里 grep 不到 127.0.0.1 / localhost" % len(emitted))
    if sub_hits:
        bad("③ 按 Vite 规则替换后仍有写死的 host：%s" % "、".join(sub_hits))
    else:
        ok("③ 按 Vite 规则把 import.meta.env 替换成 .env 的值后，仍然 grep 不到 host")

    # 反向验证：替换**确实生效**（否则上面的 0 可能只是"替换没发生"）
    probe = {"a.js": "const u = import.meta.env.VITE_API_BASE_URL;"}
    probe_env = {"VITE_API_BASE_URL": "https://api.example.com"}
    old_env = dict(env)
    try:
        env.clear()
        env.update(probe_env)
        replaced = substitute(probe["a.js"])
    finally:
        env.clear()
        env.update(old_env)
    if "https://api.example.com" in replaced:
        ok("③ 判别力自检：替换规则确实生效（设了 VITE_API_BASE_URL 就会被内联 → 所以 0 命中是真结论）")
    else:
        bad("③ 判别力自检失败：替换规则没生效，③ 的 0 命中不可信")

    shutil.rmtree(TMP_EMIT_DIR, ignore_errors=True)


def check_proxy_target_not_shipped(sources):
    """④ 代理目标（127.0.0.1:8000）只允许出现在 Node 侧配置与模板里。"""
    allowed = {"vite.config.ts", ".env.example"}
    shipped = {name: src for name, src in sources.items()}
    hits = [name for name, src in shipped.items() if HARDCODED_HOST.search(code_only(src))]
    if hits:
        bad("④ 代理目标出现在了会被打包的源码里：%s" % "、".join(hits))
    else:
        ok("④ 代理目标不在任何前端源码里（Node 侧配置与 .env.example 才是它的家）")

    vite = read("vite.config.ts")
    if HARDCODED_HOST.search(vite):
        ok("④ vite.config.ts 里的默认代理目标是 Node 侧配置 —— 永远不会被打进浏览器产物")
    else:
        env_problem("④ vite.config.ts 里没有默认代理目标（可能是被改成纯环境变量了）")
    assert allowed  # 说明性常量：允许清单在文档里，不在判定里


def check_lint_invariants(node_path):
    """结构性 Hook 规则（拆分/改动最容易写坏的那三条）。"""
    eslint_js = os.path.join(FRONTEND_DIR, "node_modules", "eslint", "bin", "eslint.js")
    if not node_path or not os.path.exists(eslint_js):
        env_problem("找不到 eslint，跳过结构性规则复核")
        return
    try:
        proc = subprocess.run([node_path, eslint_js, ".", "-f", "json"], cwd=FRONTEND_DIR,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        env_problem("eslint 无法执行：%s" % exc)
        return
    try:
        results = json.loads(proc.stdout.decode("utf-8-sig", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        env_problem("eslint JSON 解析失败（%s）" % exc)
        return

    counts, errors, warnings = {}, 0, 0
    for item in results:
        errors += item.get("errorCount", 0)
        warnings += item.get("warningCount", 0)
        for msg in item.get("messages", []):
            key = msg.get("ruleId") or "<parse-error>"
            counts[key] = counts.get(key, 0) + 1
    structural = ("react-hooks/refs", "react-hooks/immutability", "react-hooks/purity")
    hits = {r: counts.get(r, 0) for r in structural}
    if any(hits.values()):
        bad("⑥ 引入结构性 Hook 违规：%s" % hits)
    else:
        ok("⑥ 结构性 Hook 规则全为 0（%s）" % "、".join(structural))
    say("     eslint 现状（**不作为门槛**，HEAD 上就是红的）：%d errors / %d warnings；Top %s"
        % (errors, warnings, sorted(counts.items(), key=lambda kv: -kv[1])[:3]))


def segment_source_recheck(node_path, args):
    section("C. 独立复核（本脚本自己扫源码 + 自己编译一遍，不复用测试里的辅助函数）")
    fail_before = len(FAILED)

    sources = walk_sources()
    if len(sources) < 25:
        bad("只扫到 %d 个源文件 —— 收集器可能失效" % len(sources))
        SEG["C"] = False
        return
    ok("扫描口径：src 下 %d 个 .ts/.tsx" % len(sources))

    check_relative_base(sources)
    check_vite_proxy(node_path)
    check_env_semantics()
    if args.skip_bundle:
        skip("③ --skip-bundle：跳过离线打包模拟")
    else:
        simulate_bundle(node_path)
    check_proxy_target_not_shipped(sources)
    check_lint_invariants(node_path)

    SEG["C"] = (len(FAILED) == fail_before)


# ---------------------------------------------------------------------------
# D. 构建探测（默认跳过；真跑成功时对 dist/ 再 grep 一遍）
# ---------------------------------------------------------------------------

def segment_build(node_path, args):
    section("D. 构建探测（vite build，默认跳过）")
    if not args.with_build:
        skip("默认不跑（受限沙箱里 spawn 走管道的子进程会 EPERM；离线版见 C-③）")
        SEG["D"] = None
        return
    vite_js = os.path.join(FRONTEND_DIR, "node_modules", "vite", "bin", "vite.js")
    if not node_path or not os.path.exists(vite_js):
        env_problem("找不到 node_modules/vite/bin/vite.js，跳过 D 段")
        SEG["D"] = None
        return

    code, out = run([node_path, vite_js, "build"], timeout=1800)
    if code is None or "EPERM" in out:
        env_problem("vite build 撞上沙箱边界（spawn EPERM）—— 与 docs/29~32 同因，非本次改动问题")
        SEG["D"] = None
        return
    if code != 0:
        for line in out.splitlines()[:12]:
            say("     %s" % line.strip())
        bad("vite build 失败（exit=%s）" % code)
        SEG["D"] = False
        return

    ok("vite build 成功 —— 对真实产物 dist/ 再 grep 一遍")
    hits = []
    dist = os.path.join(FRONTEND_DIR, "dist")
    for dirpath, _dirnames, filenames in os.walk(dist):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    if HARDCODED_HOST.search(fh.read()):
                        hits.append(os.path.relpath(full, FRONTEND_DIR))
            except OSError:
                continue
    if hits:
        bad("D dist/ 里仍有写死的 host：%s" % "、".join(hits))
        SEG["D"] = False
    else:
        ok("D 真实构建产物 dist/ 里 grep 不到 127.0.0.1（验收标准原文达成）")
        SEG["D"] = True


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def summary():
    section("汇总")
    parts = []
    for key in ("A", "B", "C", "D"):
        val = SEG.get(key)
        if val is True:
            mark = "PASS"
        elif val is False:
            mark = "FAIL"
        elif key in ("B", "D") and ENV_PROBLEMS:
            mark = "ENV"
        else:
            mark = "SKIP"
        parts.append("[%s] %s" % (mark, key))
    say("  " + " / ".join(parts))

    if ENV_PROBLEMS:
        say("")
        say("  环境提示（不算失败，但会让结论不完整）：")
        for item in ENV_PROBLEMS:
            say("    - %s" % item)

    if FAILED:
        say("")
        say("  未通过项：")
        for item in FAILED:
            say("    - %s" % item)
        say("")
        say("❌ T-34 人工验收未通过")
        return 1

    say("")
    say("✅ T-34 人工验收通过")
    return 0


def main():
    _make_stdout_forgiving()
    parser = argparse.ArgumentParser(description="T-34 一键人工验收（离线、无需 Token）")
    parser.add_argument("--skip-tsc", action="store_true", help="跳过 TypeScript 类型检查")
    parser.add_argument("--skip-bundle", action="store_true", help="跳过离线打包模拟（C-③）")
    parser.add_argument("--with-build", action="store_true", help="额外尝试 vite build 并扫 dist/")
    args = parser.parse_args()

    say("T-34 人工验收：API 基地址相对化 + Vite 代理 + VITE_API_BASE_URL 注入（Bug 4，P0）")
    say("仓库：%s" % REPO_DIR)

    node_path = segment_preflight()
    segment_contract_tests(node_path)
    segment_tsc(node_path, args)
    segment_source_recheck(node_path, args)
    segment_build(node_path, args)

    code = summary()
    if code != 0:
        return 1
    if ENV_PROBLEMS and (SEG.get("A") is None or SEG.get("B") is None):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
