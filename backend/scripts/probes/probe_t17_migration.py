"""T-17 破坏性验证：向迁移 002 注入缺陷，确认测试**确实会失败**。

同时跑两个测试模块（`tests.test_migrations` + `tests.test_migration_002`），
因为安全闸（不得含破坏性语句、必须 IF NOT EXISTS、迁移链完整）在
test_migrations 里，而结构/语义断言在 test_migration_002 里。

每个探针注入 → 跑测试 → 记录退出码 → `try/finally` 还原 → sha256 校验。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t17_migration.py
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGET = os.path.join(ROOT, "migrations", "versions", "002_sessions_and_stores.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

TEST_MODULES = ["tests.test_migrations", "tests.test_migration_002"]

PARTIAL_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_active \"\n"
                 "    \"ON interview_sessions(user_id) WHERE status = 'active'\"")

PROBES = [
    (
        "PROBE-1 部分唯一索引退化为普通唯一索引（去掉 WHERE）",
        "ON interview_sessions(user_id) WHERE status = 'active'",
        "ON interview_sessions(user_id)",
    ),
    (
        "PROBE-2 漏建 ended_reason 列（协议与 DDL 漂移）",
        "        expires_at      DATETIME NOT NULL,\n        ended_reason    VARCHAR",
        "        expires_at      DATETIME NOT NULL",
    ),
    (
        "PROBE-3 去掉 ON DELETE CASCADE",
        "REFERENCES users(id) ON DELETE CASCADE",
        "REFERENCES users(id)",
    ),
    (
        "PROBE-4 漏建一个索引（idx_captcha_expires）",
        '    "CREATE INDEX IF NOT EXISTS idx_captcha_expires ON captcha_store(expires_at)",',
        "",
    ),
    (
        "PROBE-5 建表语句去掉 IF NOT EXISTS（失去幂等）",
        "CREATE TABLE IF NOT EXISTS captcha_store",
        "CREATE TABLE captcha_store",
    ),
    (
        "PROBE-6 混入破坏性语句 DROP TABLE",
        '    "CREATE INDEX IF NOT EXISTS idx_blacklist_expires ON token_blacklist(expires_at)",',
        '    "CREATE INDEX IF NOT EXISTS idx_blacklist_expires ON token_blacklist(expires_at)",\n'
        '    "DROP TABLE IF EXISTS notifications",',
    ),
    (
        "PROBE-7 role 列去掉 NOT NULL",
        "        role            VARCHAR NOT NULL,",
        "        role            VARCHAR,",
    ),
    (
        "PROBE-8 status 默认值不再是 active",
        "        status          VARCHAR NOT NULL DEFAULT 'active',",
        "        status          VARCHAR NOT NULL DEFAULT 'finished',",
    ),
    (
        "PROBE-9 captcha_store 主键换到 code（captcha_id 不再唯一）",
        "        captcha_id VARCHAR PRIMARY KEY,",
        "        captcha_id VARCHAR,",
    ),
    (
        "PROBE-10 越界：顺手给 users 加 client_token（那是 T-18 的活）",
        "REVISION = \"002\"",
        "REVISION = \"002\"",
    ),
    (
        "PROBE-11 迁移链断裂（DOWN_REVISION 写错）",
        'DOWN_REVISION = "001"',
        'DOWN_REVISION = "000"',
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
        [PY, "-m", "unittest"] + TEST_MODULES,
        cwd=ROOT, capture_output=True, env=env,
    )
    text = (proc.stdout or b"").decode("utf-8", "replace") + \
           (proc.stderr or b"").decode("utf-8", "replace")
    verdict = [ln.strip() for ln in text.splitlines()
               if ln.strip().startswith(("OK", "FAILED"))]
    failing = sorted({
        ln.split(" (")[0].replace("FAIL: ", "").replace("ERROR: ", "").strip()
        for ln in text.splitlines()
        if ln.startswith(("FAIL: ", "ERROR: "))
    })
    return proc.returncode, (verdict[-1] if verdict else "(无结论行)"), failing


def main():
    original = read_text(TARGET)
    print("002_sessions_and_stores.py sha256 BEFORE = %s" % sha(original))
    print("")
    print("=" * 72)
    print("BASELINE（无缺陷）")
    print("=" * 72)
    rc, line, _ = run_tests()
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

            # PROBE-10 是"越界"探针：往 002 里塞一条给 users 加列的语句
            if name.startswith("PROBE-10"):
                mutated = original.replace(
                    'DOWN_REVISION = "001"',
                    'DOWN_REVISION = "001"\n\nUPGRADE_EXTRA = []',
                    1,
                ).replace(
                    "UPGRADE_STATEMENTS = [",
                    'UPGRADE_STATEMENTS = [\n'
                    '    "ALTER TABLE users ADD COLUMN client_token VARCHAR",',
                    1,
                )
            else:
                if needle not in original:
                    print("  !! 目标字符串未找到，探针无效")
                    results.append((name, "INVALID", []))
                    continue
                mutated = original.replace(needle, replacement, 1)

            if mutated == original:
                print("  !! 注入后内容未变化，探针无效")
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
        print("  %-8s %-56s 失败用例数=%d" % (status, name, len(failing)))
    print("")
    print("  restored exit=%s（应为 0）" % rc)
    print("  sha256 AFTER  = %s" % sha(read_text(TARGET)))

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
