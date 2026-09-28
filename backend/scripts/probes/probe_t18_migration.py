"""T-18 破坏性验证：向迁移 003 **与新增的三道框架闸门**注入缺陷。

T-18 有两层产物，都要证明"测试真的有判别力"：
  * 迁移 003 本身（重建表 / 加列 / 删列）
  * runner 里新加的三道闸（行数不减 / 声明式自检 / 外键完整）

因此探针分两组：M* 打迁移文件，F* 打 runner。每组都注入 -> 跑测试 ->
记录退出码 -> `try/finally` 还原 -> sha256 校验。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probes\\probe_t18_migration.py
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIG = os.path.join(ROOT, "migrations", "versions", "003_existing_tables.py")
RUNNER = os.path.join(ROOT, "migrations", "runner.py")
PY = os.path.join(ROOT, "venv", "Scripts", "python.exe")

TEST_MODULES = [
    "tests.test_migrations",
    "tests.test_migration_002",
    "tests.test_migration_003",
    "tests.test_migration_gates",
]

RECORDS_SELECT = (
    '"INSERT INTO interview_records_new "\n'
    '    "(id, user_id, role, messages, report, created_at, status, admin_comment) "\n'
    '    "SELECT id, user_id, role, messages, report, created_at, status, admin_comment "\n'
    '    "FROM interview_records",'
)

# (名称, 目标文件, 原文, 替换)
PROBES = [
    # ---------------- 迁移 003 本身 ----------------
    ("M1  users 不加 must_change_password",
     MIG,
     '"ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0",',
     ""),
    ("M2  重建时漏拷 messages 列（列丢数据）",
     MIG,
     '"INSERT INTO interview_records_new "\n'
     '    "(id, user_id, role, messages, report, created_at, status, admin_comment) "\n'
     '    "SELECT id, user_id, role, messages, report, created_at, status, admin_comment "\n'
     '    "FROM interview_records",',
     '"INSERT INTO interview_records_new "\n'
     '    "(id, user_id, role, report, created_at, status, admin_comment) "\n'
     '    "SELECT id, user_id, role, report, created_at, status, admin_comment "\n'
     '    "FROM interview_records",'),
    ("M3  重建时少拷行（WHERE 丢行）",
     MIG,
     '"FROM interview_records",',
     '"FROM interview_records WHERE id < 3",'),
    ("M4  重建后丢唯一约束（UNIQUE(client_token) 去掉）",
     MIG,
     "        UNIQUE (client_token)\n",
     ""),
    ("M5  重建后不补回索引",
     MIG,
     '    "CREATE INDEX IF NOT EXISTS ix_interview_records_id ON interview_records (id)",',
     ""),
    ("M6  notifications 重建后 link_url 仍在（死列没删掉）",
     MIG,
     "        created_at  DATETIME,\n        PRIMARY KEY (id),\n"
     "        FOREIGN KEY(user_id) REFERENCES users (id)\n    )\n    \"\"\",\n"
     '    "INSERT INTO notifications_new "',
     "        created_at  DATETIME,\n        link_url    VARCHAR,\n"
     "        PRIMARY KEY (id),\n        FOREIGN KEY(user_id) REFERENCES users (id)\n"
     "    )\n    \"\"\",\n"
     '    "INSERT INTO notifications_new "'),
    ("M7  重建时列错位（role/messages 互换）",
     MIG,
     '    "SELECT id, user_id, role, messages, report, created_at, status, admin_comment "\n'
     '    "FROM interview_records",',
     '    "SELECT id, user_id, messages, role, report, created_at, status, admin_comment "\n'
     '    "FROM interview_records",'),
    ("M8  重建后外键子句丢失",
     MIG,
     "        client_token  VARCHAR,\n        PRIMARY KEY (id),\n"
     "        FOREIGN KEY(user_id) REFERENCES users (id),\n"
     "        UNIQUE (client_token)\n    )",
     "        client_token  VARCHAR,\n        PRIMARY KEY (id),\n"
     "        UNIQUE (client_token)\n    )"),
    ("M9  去掉 ALLOWS_TABLE_REBUILD 声明（DROP 变成未声明的破坏性语句）",
     MIG,
     # 注意：必须带上换行符。文件里 "ALLOWS_TABLE_REBUILD = True" 出现**两次**
     # —— 一次在 docstring 的说明文字里，一次是真正的赋值。
     # 首版没带换行，`replace(..., 1)` 命中了 docstring，赋值原封不动，
     # 于是探针报"漏网"，实则**探针自己错了**（见 SUMMARY 的 count 校验）。
     "\nALLOWS_TABLE_REBUILD = True",
     "\nALLOWS_TABLE_REBUILD = False"),
    ("M10 删列时把不该删的列也删了（admin_comment 丢了）",
     MIG,
     '    "SELECT id, user_id, role, messages, report, created_at, status, admin_comment "\n'
     '    "FROM interview_records",',
     '    "SELECT id, user_id, role, messages, report, created_at, status, NULL "\n'
     '    "FROM interview_records",'),

    # ---------------- 框架三道闸 ----------------
    ("F1  停用闸门 6（行数不得减少）",
     RUNNER,
     "checked = _check_no_row_loss(before_counts, _row_counts(conn), migration)",
     "checked = len(before_counts)"),
    ("F2  停用闸门 7（声明式自检）",
     RUNNER,
     "n_verify = _run_verifications(conn, migration)",
     "n_verify = 0"),
    ("F3  停用闸门 8（foreign_key_check）",
     RUNNER,
     "_check_foreign_keys(conn, migration)",
     "pass"),
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
    proc = subprocess.run([PY, "-m", "unittest"] + TEST_MODULES,
                          cwd=ROOT, capture_output=True, env=env)
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
    originals = {MIG: read_text(MIG), RUNNER: read_text(RUNNER)}
    for path, text in originals.items():
        print("%s sha256 BEFORE = %s" % (os.path.basename(path), sha(text)))
    print("")
    print("=" * 74)
    print("BASELINE（无缺陷）")
    print("=" * 74)
    rc, line, _ = run_tests()
    print("  exit=%s  %s" % (rc, line))
    if rc != 0:
        print("  !! 基线未通过，后续探针结论无意义")
        return 1

    results = []
    try:
        for name, target, needle, replacement in PROBES:
            print("")
            print("=" * 74)
            print(name)
            print("=" * 74)
            for path, text in originals.items():
                write_text(path, text)
            # 关键校验：目标字符串必须**恰好出现一次**。
            # 首版 M9 的针出现了两次（docstring 里也有一处），
            # `replace(..., 1)` 命中了说明文字、真正的赋值没被改，
            # 于是把"探针写错了"误报成"测试漏网"。
            # 计数校验能让这类错误当场暴露，而不是浪费一轮排查。
            occurrences = originals[target].count(needle)
            if occurrences == 0:
                print("  !! 目标字符串未找到，探针无效")
                results.append((name, "INVALID", []))
                continue
            if occurrences > 1:
                print("  !! 目标字符串出现 %d 次（应为 1），探针可能改错位置 —— 视为无效"
                      % occurrences)
                results.append((name, "INVALID", []))
                continue
            write_text(target, originals[target].replace(needle, replacement, 1))
            rc, line, failing = run_tests()
            status = "CAUGHT" if rc != 0 else "MISSED"
            print("  exit=%s  %s  -> %s" % (rc, line, status))
            for f in failing[:8]:
                print("     失败用例: %s" % f)
            results.append((name, status, failing))
    finally:
        for path, text in originals.items():
            write_text(path, text)

    print("")
    print("=" * 74)
    print("RESTORED（还原后复测）")
    print("=" * 74)
    rc, line, _ = run_tests()
    print("  exit=%s  %s" % (rc, line))

    print("")
    print("=" * 74)
    print("SUMMARY")
    print("=" * 74)
    for name, status, failing in results:
        print("  %-8s %-52s 失败用例数=%d" % (status, name, len(failing)))
    print("")
    print("  restored exit=%s（应为 0）" % rc)
    for path in (MIG, RUNNER):
        print("  %s sha256 AFTER = %s" % (os.path.basename(path), sha(read_text(path))))

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
