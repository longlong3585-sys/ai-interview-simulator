"""T-16 破坏性验证：向协议层注入缺陷，确认测试**确实会失败**。

没有这一步，"26 项全绿"只说明测试跑了，不说明测试**有判别力**。
每个探针注入 → 跑测试 → 记录退出码 → `try/finally` 还原 → sha256 校验。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t16_protocol.py
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGET = os.path.join(ROOT, "services", "stores", "base.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

TEST_MODULE = "tests.test_stores_protocol"

# (名称, 原文, 替换)
#
# 注意两个"改法"上的讲究：
#   * PROBE-1 用 `import os`（标准库里**真实存在**但不在白名单内），
#     而不是 `import redis`。因为 redis 根本没装，`import redis` 会让
#     **整个测试模块导入失败**，看起来"被抓住了"，但其实测的是
#     "依赖没装"而非 ALLOWED_IMPORTS 这条断言。换成 import os 才能
#     精确命中白名单断言。
#   * PROBE-6 必须**连同装饰器一起删**。只把 `Protocol` 换成 `object`
#     会让 `@runtime_checkable` 在类创建时抛 TypeError，
#     同样变成"模块导入失败"的假阳性。
PROBES = [
    (
        "PROBE-1 引入白名单外的依赖（import os）",
        "from dataclasses import dataclass",
        "import os\nfrom dataclasses import dataclass",
    ),
    (
        "PROBE-1b 依赖第三方库（import redis，真实未安装）",
        "from dataclasses import dataclass",
        "import redis\nfrom dataclasses import dataclass",
    ),
    (
        "PROBE-2 快照不再不可变（frozen=True -> False）",
        "class SessionSnapshot(object):",
        None,  # 特殊处理：改的是 @dataclass(frozen=True) 装饰器
    ),
    (
        "PROBE-3 过期边界 <= 改成 <",
        "return self.expires_at <= now",
        "return self.expires_at < now",
    ),
    (
        "PROBE-4 协议漏掉一个方法（删 verify_and_consume）",
        "    def verify_and_consume(self, captcha_id: str, code: str, now: str) -> bool:",
        "    def verify_and_consume_RENAMED(self, captcha_id: str, code: str, now: str) -> bool:",
    ),
    (
        "PROBE-5 协议层泄露 SQL",
        "TERMINAL_STATUSES = (SessionStatus.FINISHED, SessionStatus.ABANDONED)",
        "TERMINAL_STATUSES = (SessionStatus.FINISHED, SessionStatus.ABANDONED)\n_LEAKED_QUERY = \"SELECT 1 FROM users\"",
    ),
    (
        "PROBE-6 去掉 @runtime_checkable（RateLimitStore）",
        "@runtime_checkable\nclass RateLimitStore(Protocol):",
        "class RateLimitStore(Protocol):",
    ),
    (
        "PROBE-7 参数改名（now -> at），破坏关键字调用契约",
        "    def abandon_all_expired(self, now: str) -> int:",
        "    def abandon_all_expired(self, at: str) -> int:",
    ),
    (
        "PROBE-8 快照字段与 DDL 漂移（删 last_reply）",
        "    last_reply: Optional[str] = None\n    #: `completed` / `timeout` / `manual`；仅终态有值（ADR-007R）。",
        "    #: `completed` / `timeout` / `manual`；仅终态有值（ADR-007R）。",
    ),
]


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_tests():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PY, "-m", "unittest", TEST_MODULE, "-v"],
        cwd=ROOT, capture_output=True, env=env,
    )
    text = (proc.stdout or b"").decode("utf-8", "replace") + \
           (proc.stderr or b"").decode("utf-8", "replace")
    ran = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("Ran ")]
    verdict = [ln.strip() for ln in text.splitlines()
               if ln.strip().startswith(("OK", "FAILED"))]
    # 具体失败的用例名，便于判断"失败的是不是该失败的那一项"
    failing = sorted({
        ln.split(" (")[0].replace("FAIL: ", "").replace("ERROR: ", "").strip()
        for ln in text.splitlines()
        if ln.startswith(("FAIL: ", "ERROR: "))
    })
    return proc.returncode, " | ".join(ran + verdict), failing


def apply_probe(original, name, needle, replacement):
    """PROBE-2 需要改装饰器：把 SessionSnapshot 的 frozen 翻掉。"""
    if replacement is None and "frozen" in name:
        target = (
            "@dataclass(frozen=True)\nclass SessionSnapshot(object):"
        )
        if target in original:
            return original.replace(
                target,
                "@dataclass(frozen=False)\nclass SessionSnapshot(object):",
            )
        return original
    if needle is None or needle not in original:
        return None
    return original.replace(needle, replacement, 1)


def main():
    original = read_text(TARGET)
    print("base.py sha256 BEFORE = %s" % sha(original))
    print("")
    print("=" * 72)
    print("BASELINE（无缺陷）")
    print("=" * 72)
    rc, line, failing = run_tests()
    print("  exit=%s  %s" % (rc, line))
    if rc != 0:
        print("  !! 基线未通过，后续探针结论无意义")
        return 1

    results = []
    try:
        for name, needle, replacement in PROBES:
            print("")
            print("=" * 72)
            print(name)
            print("=" * 72)
            write_text(TARGET, original)
            mutated = apply_probe(original, name, needle, replacement)
            if mutated is None or mutated == original:
                print("  !! 探针未生效（目标字符串未找到），本项无效")
                results.append((name, "INVALID", []))
                continue
            write_text(TARGET, mutated)
            rc, line, failing = run_tests()
            status = "CAUGHT" if rc != 0 else "MISSED"
            print("  exit=%s  %s  -> %s" % (rc, line, status))
            for f in failing:
                print("     失败用例: %s" % f)
            results.append((name, status, failing))
    finally:
        write_text(TARGET, original)

    print("")
    print("=" * 72)
    print("RESTORED（还原后复测）")
    print("=" * 72)
    rc, line, _ = run_tests()
    print("  exit=%s  %s" % (rc, line))

    print("")
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for name, status, failing in results:
        print("  %-8s %-52s 失败用例数=%d" % (status, name, len(failing)))
    print("")
    print("  restored exit=%s（应为 0）" % rc)
    print("  base.py sha256 AFTER  = %s" % sha(read_text(TARGET)))

    missed = [r for r in results if r[1] != "CAUGHT"]
    ok = (not missed) and rc == 0
    print("")
    print("  结论: %s" % ("全部探针被抓住，且已还原 [OK]" if ok
                          else "存在漏网探针或还原失败 [BAD]"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
