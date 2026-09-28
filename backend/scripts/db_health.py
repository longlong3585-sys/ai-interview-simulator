"""阶段 1 回归体检：数据库结构与数据的健康检查（T-01 备份工具的姊妹脚本）。

用途：在人工回归测试前后各跑一次，用**客观数据**回答"数据库有没有被破坏"。

    # 体检前：存基线
    python backend/scripts/db_health.py --save baseline.json

    # 体检后：与基线比对
    python backend/scripts/db_health.py --compare baseline.json

设计要点：
  - 输出一律 ASCII，避免 Windows 控制台代码页把中文变成乱码
  - 以**只读**方式打开数据库（mode=ro），体检本身不可能改动数据
  - 比对时区分"预期变化"与"异常变化"：
      * interview_records 数量**增加** 属正常（你做了面试）
      * 表结构变化、users 行变化、记录数量**减少** 属异常
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys

EXPECTED_TABLES = ["interview_records", "notifications", "users"]

# 各表的关键列（缺列即视为结构被破坏）
EXPECTED_COLUMNS = {
    "users": ["id", "username", "hashed_password", "created_at", "role", "email",
              "is_active", "nickname", "avatar", "bio", "gender", "birthday"],
    "interview_records": ["id", "user_id", "role", "messages", "report",
                          "created_at", "status", "admin_comment"],
    "notifications": ["id", "user_id", "type", "message", "target_type",
                      "target_id", "is_read", "created_at"],
}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(REPO_ROOT, "backend", "interview.db")


def collect(db_path):
    """以只读方式采集数据库现状。"""
    uri = "file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type=? ORDER BY name", ("table",)
            )
            if not r[0].startswith("sqlite_")
        )
        columns = {
            t: [d[1] for d in conn.execute("PRAGMA table_info(%s)" % t)]
            for t in tables
        }
        counts = {
            t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            for t in tables
        }
        users = [
            {"id": r[0], "username": r[1], "role": r[2],
             "is_active": r[3], "avatar": r[4]}
            for r in conn.execute(
                "SELECT id, username, role, is_active, avatar FROM users ORDER BY id"
            )
        ]
        statuses = {
            r[0] or "(null)": r[1]
            for r in conn.execute(
                "SELECT status, COUNT(*) FROM interview_records GROUP BY status"
            )
        }
        with open(db_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        return {
            "db_path": os.path.abspath(db_path),
            "integrity_check": integrity,
            "tables": tables,
            "columns": columns,
            "counts": counts,
            "users": users,
            "record_statuses": statuses,
            "file_sha256": digest,
        }
    finally:
        conn.close()


def print_report(snap):
    print("DB health report")
    print("  path            : %s" % snap["db_path"])
    print("  integrity_check : %s" % snap["integrity_check"])
    print("  tables          : %s" % ", ".join(snap["tables"]))
    print("  counts          : %s" % json.dumps(snap["counts"]))
    print("  record statuses : %s" % json.dumps(snap["record_statuses"]))
    print("  users:")
    for u in snap["users"]:
        print("    id=%s username=%-10s role=%-6s is_active=%s avatar=%s"
              % (u["id"], u["username"], u["role"], u["is_active"], u["avatar"]))
    print("  columns:")
    for t, cols in snap["columns"].items():
        print("    %-20s %s" % (t, ",".join(cols)))
    print("  file_sha256     : %s" % snap["file_sha256"][:32])


def compare(base, now):
    """返回 (fatal_problems, expected_changes)。fatal 非空即视为数据库被破坏。"""
    fatal = []
    expected = []

    if now["integrity_check"] != "ok":
        fatal.append("integrity_check = %s (expected 'ok')" % now["integrity_check"])

    for t in EXPECTED_TABLES:
        if t not in now["tables"]:
            fatal.append("missing table: %s" % t)
    for t in now["tables"]:
        if t not in EXPECTED_TABLES:
            expected.append("new table appeared: %s" % t)

    for t, cols in EXPECTED_COLUMNS.items():
        if t not in now["columns"]:
            continue
        missing = [c for c in cols if c not in now["columns"][t]]
        if missing:
            fatal.append("table %s missing columns: %s" % (t, missing))

    if base["users"] != now["users"]:
        fatal.append("users 表内容发生变化（不应发生）")
        for b, n in zip(base["users"], now["users"]):
            if b != n:
                fatal.append("  before: %s" % json.dumps(b))
                fatal.append("  after : %s" % json.dumps(n))

    b_users = base["counts"].get("users")
    n_users = now["counts"].get("users")
    if b_users != n_users:
        fatal.append("users 行数变化: %s -> %s" % (b_users, n_users))

    b_rec = base["counts"].get("interview_records", 0)
    n_rec = now["counts"].get("interview_records", 0)
    if n_rec < b_rec:
        fatal.append("interview_records 行数减少: %s -> %s（数据丢失！）" % (b_rec, n_rec))
    elif n_rec > b_rec:
        expected.append("interview_records 增加 %d 条（你做了面试，属正常）" % (n_rec - b_rec))
    else:
        expected.append("interview_records 数量未变")

    return fatal, expected


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stage-1 regression DB health check.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--save", help="write a baseline snapshot to this json file")
    ap.add_argument("--compare", help="compare current state against a baseline json")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print("ERROR: database not found: %s" % args.db, file=sys.stderr)
        return 2

    snap = collect(args.db)
    print_report(snap)

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        print("\nBASELINE_SAVED %s" % args.save)

    if args.compare:
        with open(args.compare, "r", encoding="utf-8") as f:
            base = json.load(f)
        fatal, expected = compare(base, snap)
        print("\n=== comparison against %s ===" % args.compare)
        for line in expected:
            print("  [ok]   %s" % line)
        for line in fatal:
            print("  [FAIL] %s" % line)
        if fatal:
            print("\nDB_HEALTH_FAILED (%d problem(s))" % len(fatal))
            return 1
        print("\nDB_HEALTH_OK")

    return 0


if __name__ == "__main__":
    sys.exit(main())
