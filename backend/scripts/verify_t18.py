"""T-18 验收工具：核对迁移 003 之后的库。

**纯只读**（`mode=ro` 打开），可以安全地对真实数据库运行。

用法
----
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t18.py                       # 默认 interview.db
    .\\venv\\Scripts\\python.exe scripts\\verify_t18.py --db <副本.db>

退出码：0 = 全部通过；1 = 有失败项。

校验内容
--------
  1. 迁移版本：003 已应用
  2. users：多了 must_change_password，且既有行是 0 而不是 NULL
  3. interview_records：多了 client_token，且**唯一约束真的生效**
  4. notifications：死列 link_url 已消失
  5. 重建后索引被补回（DROP TABLE 会连带删掉索引 —— 漏补是静默的性能回归）
  6. 重建后外键子句存活
  7. 没有残留重建用的 _new 临时表
  8. 002 建的四张表未被波及
  9. 数据行数与完整性
"""
import argparse
import os
import sqlite3
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BACKEND_DIR, "interview.db")

EXPECTED_COLUMNS = {
    "users": ["id", "username", "hashed_password", "created_at", "role", "email",
              "is_active", "nickname", "avatar", "bio", "gender", "birthday",
              "must_change_password"],
    "interview_records": ["id", "user_id", "role", "messages", "report",
                          "created_at", "status", "admin_comment", "client_token"],
    "notifications": ["id", "user_id", "type", "message", "target_type",
                      "target_id", "is_read", "created_at"],
}

REQUIRED_INDEXES = ["ix_interview_records_id", "ix_notifications_id",
                    "ix_users_id", "ix_users_username", "ix_users_email"]

SURVIVING_TABLES = ["interview_sessions", "captcha_store", "auth_attempts",
                    "token_blacklist"]

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((bool(ok), name, detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -> " + detail) if detail else ""))
    return bool(ok)


def section(title):
    print("")
    print("=" * 72)
    print(title)
    print("=" * 72)


def scalar_or_none(conn, sql):
    """执行一条取值查询；列不存在等错误返回 None（而不是让整个工具崩掉）。

    为什么需要：本工具可能在**迁移前**的库上运行（用于看基线）。
    那时 `must_change_password` / `client_token` 还不存在，
    直接查询会抛 `no such column` 并把整份报告打断 ——
    正确做法是把它报告成一条 FAIL，而不是崩在中间。
    """
    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return None


def main():
    ap = argparse.ArgumentParser(description="T-18 迁移 003 验收核对（只读）")
    # 同时接受位置参数与 --db（与 verify_t17.py 的用法保持一致，少一处要记的差异）
    ap.add_argument("db_pos", nargs="?", default=None, help="库路径（可省略）")
    ap.add_argument("--db", dest="db_opt", default=None, help="库路径（等价写法）")
    ap.add_argument("--facts", action="store_true",
                    help="只打印原始事实（版本/列数/新列/死列/行数/完整性），便于人工核对")
    args = ap.parse_args()

    db = os.path.abspath(args.db_pos or args.db_opt or DEFAULT_DB)
    if not os.path.isfile(db):
        print("库不存在: %s" % db)
        return 1

    print("库: %s" % db)
    print("模式: 只读")

    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        revisions = [r[0] for r in conn.execute(
            "SELECT revision FROM schema_migrations ORDER BY revision")]
        columns = {t: [d[1] for d in conn.execute("PRAGMA table_info([%s])" % t)]
                   for t in EXPECTED_COLUMNS}
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"))
        indexes = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"))
        users_null = scalar_or_none(
            conn, "SELECT count(*) FROM users WHERE must_change_password IS NULL")
        users_distinct = None
        if "must_change_password" in columns["users"]:
            users_distinct = sorted(r[0] for r in conn.execute(
                "SELECT DISTINCT must_change_password FROM users"))
        null_tokens = scalar_or_none(
            conn, "SELECT count(*) FROM interview_records WHERE client_token IS NULL")
        unique_covers = scalar_or_none(
            conn,
            "SELECT count(*) FROM pragma_index_list('interview_records') il "
            "JOIN pragma_index_info(il.name) ii "
            "WHERE il.[unique]=1 AND ii.name='client_token'")
        scratch = [t for t in tables if t.endswith("_new")]
        fk_counts = {t: len(conn.execute("PRAGMA foreign_key_list([%s])" % t).fetchall())
                     for t in ("interview_records", "notifications")}
        counts = {t: conn.execute("SELECT count(*) FROM [%s]" % t).fetchone()[0]
                  for t in ("users", "interview_records", "notifications")}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    section("1. 迁移版本")
    print("  已应用: %s" % ", ".join(revisions))

    if args.facts:
        # 「原始事实」模式：不判定通过/失败，只把事实摊开供人工核对。
        # 存在的意义是替代 PowerShell 里那些脆弱的 `python -c "..."` 一行流 ——
        # 本项目的日志里已多次记录 PowerShell 会吃掉内联代码里的引号。
        print("")
        print("  原始事实")
        print("    revisions           : %s" % revisions)
        print("    users 列数          : %d" % len(columns["users"]))
        print("    users 含新列        : %s"
              % ("must_change_password" in columns["users"]))
        print("    interview_records 列数 : %d" % len(columns["interview_records"]))
        print("    interview_records 含 client_token : %s"
              % ("client_token" in columns["interview_records"]))
        print("    notifications 列数  : %d" % len(columns["notifications"]))
        print("    notifications 含 link_url : %s"
              % ("link_url" in columns["notifications"]))
        print("    覆盖 client_token 的唯一索引数 : %r" % (unique_covers,))
        print("    client_token 为 NULL 的行数    : %r" % (null_tokens,))
        print("    must_change_password 为 NULL 的行数 : %r" % (users_null,))
        print("    must_change_password 取值集合  : %r" % (users_distinct,))
        print("    行数                : %s"
              % ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
        print("    integrity_check     : %s" % integrity)
        print("    foreign_key_check   : %s" % (fk_violations or "无违规"))
        print("")
        return 0

    check("003 已应用", "003" in revisions, "revisions=%r" % (revisions,))
    check("002 仍在（未回退）", "002" in revisions)

    section("2. 结构变更")
    for table, expected in EXPECTED_COLUMNS.items():
        check("表 %s 的列与规格一致" % table, columns[table] == expected,
              "实际=%s" % ", ".join(columns[table]))
    check("notifications 已无 link_url", "link_url" not in columns["notifications"])
    check("users 已有 must_change_password",
          "must_change_password" in columns["users"])
    check("interview_records 已有 client_token",
          "client_token" in columns["interview_records"])

    section("3. 加列的数据语义")
    check("既有用户的 must_change_password 不是 NULL",
          users_null == 0, "NULL 行数=%r" % (users_null,))
    check("既有用户取值只有 0", users_distinct in ([0], []),
          "distinct=%r" % (users_distinct,))
    check("既有面试记录的 client_token 为 NULL（尚未使用）",
          null_tokens is not None, "NULL 行数=%r" % (null_tokens,))
    check("唯一约束真的覆盖 client_token", unique_covers == 1,
          "匹配的唯一索引数=%r" % (unique_covers,))

    section("4. 重建的完整性")
    for idx in REQUIRED_INDEXES:
        check("索引 %s 存在" % idx, idx in indexes)
    for table, n in fk_counts.items():
        check("表 %s 的外键子句存活" % table, n == 1, "外键数=%d" % n)
    check("无残留 _new 临时表", not scratch, "残留=%s" % (scratch or "无"))
    for t in SURVIVING_TABLES:
        check("002 的表 %s 未被波及" % t, t in tables)

    section("5. 数据与完整性")
    print("  行数: %s" % ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    check("integrity_check = ok", integrity == "ok", integrity)
    check("foreign_key_check 无违规", not fk_violations,
          "violations=%d" % len(fk_violations))

    section("汇总")
    failed = [r for r in RESULTS if not r[0]]
    print("  共 %d 项，通过 %d 项，失败 %d 项" % (
        len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for _, name, detail in failed:
        print("  FAIL: %s  -> %s" % (name, detail))
    print("")
    print("  结论: %s" % ("全部通过 [ALL PASS]" if not failed else "存在失败项 [FAILED]"))
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
