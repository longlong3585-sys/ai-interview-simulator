"""T-15 harness self-test: destructive probes against scripts/verify_t15.py.

Proves the manual-verification harness actually FAILS when the engine contract
is broken -- i.e. that "21/21 PASS" is evidence, not a tautology.

Restoration is guaranteed by try/finally; byte-level edit keeps CRLF intact.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DBPY = os.path.join(ROOT, "database.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")
HARNESS = os.path.join(ROOT, "scripts", "verify_t15.py")

PROBES = [
    ("PROBE-1 remove PRAGMA foreign_keys=ON",
     'cursor.execute("PRAGMA foreign_keys=ON")'),
    ("PROBE-2 remove dbapi isolation_level=None",
     "dbapi_conn.isolation_level = None"),
    ("PROBE-3 remove begin BEGIN IMMEDIATE",
     'conn.exec_driver_sql("BEGIN IMMEDIATE")'),
    ("PROBE-4 remove PRAGMA journal_mode=WAL",
     'cursor.execute("PRAGMA journal_mode=WAL")'),
    ("PROBE-5 remove PRAGMA busy_timeout",
     'cursor.execute("PRAGMA busy_timeout=%d" % SQLITE_BUSY_TIMEOUT_MS)'),
    ("PROBE-6 remove PRAGMA synchronous=NORMAL",
     'cursor.execute("PRAGMA synchronous=NORMAL")'),
]


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def run_harness(label):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([PY, HARNESS], capture_output=True, env=env)
    text = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    fails = [ln.strip() for ln in text.splitlines() if "[FAIL]" in ln]
    summary = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("共 ")]
    verdict = summary[0] if summary else "(NO SUMMARY LINE -- harness crashed?)"
    print("  exit=%s   %s" % (proc.returncode, verdict))
    for f in fails:
        print("     %s" % f)
    if not summary:
        print("     ---- stderr ----")
        for ln in err.strip().splitlines()[-8:]:
            print("     %s" % ln)
    return proc.returncode, fails


def main():
    original = read_text(DBPY)
    print("database.py sha256 BEFORE = %s" % __import__("hashlib").sha256(
        original.encode("utf-8")).hexdigest())
    print("")
    print("=" * 72)
    print("BASELINE (no defect injected)")
    print("=" * 72)
    base_rc, base_fails = run_harness("baseline")
    if base_rc != 0 or base_fails:
        print("!! 基线未通过，后续探针结论无意义，直接退出")
        return 1

    results = []
    try:
        for name, needle in PROBES:
            print("")
            print("=" * 72)
            print(name)
            print("=" * 72)
            write_text(DBPY, original)
            if needle not in original:
                print("  !! 未找到目标字符串，探针无效: %s" % needle)
                results.append((name, "INVALID", 0))
                continue
            write_text(DBPY, original.replace(needle, "pass"))
            rc, fails = run_harness(name)
            status = "CAUGHT" if (rc != 0 and fails) else "MISSED"
            results.append((name, status, len(fails)))
    finally:
        write_text(DBPY, original)

    print("")
    print("=" * 72)
    print("RESTORED (re-check)")
    print("=" * 72)
    rc, fails = run_harness("restored")

    print("")
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for name, status, n in results:
        print("  %-8s %-46s failures=%d" % (status, name, n))
    print("")
    print("  restored exit=%s（应为 0）" % rc)
    print("  database.py sha256 AFTER  = %s" % __import__("hashlib").sha256(
        read_text(DBPY).encode("utf-8")).hexdigest())

    missed = [r for r in results if r[1] != "CAUGHT"]
    ok = (not missed) and rc == 0
    print("")
    print("  结论: %s" % ("全部探针被抓住，且已还原 [OK]" if ok else "存在漏网探针或还原失败 [BAD]"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
