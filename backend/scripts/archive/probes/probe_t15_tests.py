"""Run the same 6 destructive probes against the unittest file (not the harness).

Proves the committed test suite -- not just the manual harness -- has the
discriminating power for every line of T-15's contract.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DBPY = os.path.join(ROOT, "database.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

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


def read_text(p):
    with open(p, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(p, t):
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(t)


def run_units():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [PY, "-m", "unittest", "tests.test_sqlite_engine_config", "-v"],
        cwd=ROOT, capture_output=True, env=env,
    )
    text = (proc.stdout or b"").decode("utf-8", "replace") + \
           (proc.stderr or b"").decode("utf-8", "replace")
    ran = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("Ran ")]
    verdict = [ln.strip() for ln in text.splitlines()
               if ln.strip().startswith(("OK", "FAILED"))]
    n_fail = len([ln for ln in text.splitlines() if " ... FAIL" in ln or " ... ERROR" in ln])
    return proc.returncode, " | ".join(ran + verdict), n_fail


def main():
    original = read_text(DBPY)
    print("=" * 72)
    print("BASELINE -- full T-15 unit test module, no defect")
    print("=" * 72)
    rc, line, nf = run_units()
    print("  exit=%s  %s" % (rc, line))
    if rc != 0:
        print("  !! baseline failed; aborting")
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
                print("  !! needle not found: %s" % needle)
                results.append((name, "INVALID", 0))
                continue
            write_text(DBPY, original.replace(needle, "pass"))
            rc, line, nf = run_units()
            status = "CAUGHT" if rc != 0 else "MISSED"
            print("  exit=%s  %s  -> %s" % (rc, line, status))
            results.append((name, status, nf))
    finally:
        write_text(DBPY, original)

    print("")
    print("=" * 72)
    print("RESTORED -- re-run")
    print("=" * 72)
    rc, line, nf = run_units()
    print("  exit=%s  %s" % (rc, line))

    print("")
    print("=" * 72)
    print("SUMMARY (unittest suite)")
    print("=" * 72)
    for name, status, nf in results:
        print("  %-8s %-46s failing_tests=%d" % (status, name, nf))
    missed = [r for r in results if r[1] != "CAUGHT"]
    print("")
    print("  结论: %s" % ("全部探针被测试套件抓住 [OK]" if not missed else "有探针漏网 [BAD]"))
    return 0 if (not missed and rc == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
