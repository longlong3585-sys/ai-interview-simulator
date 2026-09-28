"""T-17 验收工具：核对迁移 002 建成后的库。

两种模式
--------
    默认（只读）        只查结构、版本、行数、完整性。**可安全用于真库。**
    --behavioral        额外插删测试数据，验证部分唯一索引与级联删除的**真实行为**。
                        只能对副本使用 —— 脚本会拒绝在 `interview.db` 上运行，
                        除非显式加 `--force`（防止重演 T-09 用真实数据做破坏性验证的事故）。

用法
----
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t17.py interview.db
    .\\venv\\Scripts\\python.exe scripts\\verify_t17.py <副本.db> --behavioral

退出码：0 = 全部通过；1 = 有失败项（或参数被拒绝）。
"""
import argparse
import os
import sqlite3
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BACKEND_DIR, "interview.db")

NEW_TABLES = {
    "interview_sessions": ["session_id", "user_id", "role", "questions",
                           "question_status", "user_answers", "current_index",
                           "last_seq", "last_reply", "version", "status",
                           "created_at", "updated_at", "expires_at",
                           "ended_reason"],
    "captcha_store": ["captcha_id", "code", "expires_at", "used"],
    "auth_attempts": ["id", "ip", "attempted_at"],
    "token_blacklist": ["jti", "expires_at"],
}
LEGACY_TABLES = ["users", "interview_records", "notifications"]
INDEXES = ["idx_sessions_user", "idx_sessions_active", "idx_captcha_expires",
           "idx_attempts_ip_time", "idx_blacklist_expires"]

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


def behavioural_probe(path):
    """在**副本**上验证部分唯一索引与级联删除的真实行为。全部回滚，不留残留。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("INSERT INTO users (username, email, role, is_active) "
                     "VALUES ('t17probe', 't17probe@x.y', 'user', 1)")
        uid = conn.execute("SELECT id FROM users WHERE username='t17probe'").fetchone()[0]

        def add(sid, status):
            conn.execute(
                "INSERT INTO interview_sessions (session_id, user_id, role,"
                " questions, question_status, user_answers, created_at,"
                " updated_at, expires_at, status)"
                " VALUES (?, ?, 'r', '[]', '[]', '[]', 't', 't', 't', ?)",
                (sid, uid, status))

        add("p1", "active")
        try:
            add("p2", "active")
            check("同用户第二个 active 会话被拒", False, "竟然插入成功 —— 部分唯一索引失效")
        except sqlite3.IntegrityError as exc:
            check("同用户第二个 active 会话被拒", True, str(exc))

        conn.execute("UPDATE interview_sessions SET status='abandoned' "
                     "WHERE session_id='p1'")
        add("p3", "active")
        check("置 abandoned 后释放唯一锁，可立刻重开", True,
              "active 行数=%d" % conn.execute(
                  "SELECT count(*) FROM interview_sessions "
                  "WHERE user_id=? AND status='active'", (uid,)).fetchone()[0])

        add("p4", "finished")
        add("p5", "finished")
        # 该用户此刻应有：p1(abandoned)、p3(active)、p4/p5(finished) = 4 条
        # （p2 已被拒，从未插入）
        active = conn.execute(
            "SELECT count(*) FROM interview_sessions WHERE user_id=? AND status='active'",
            (uid,)).fetchone()[0]
        terminal = conn.execute(
            "SELECT count(*) FROM interview_sessions WHERE user_id=? AND status<>'active'",
            (uid,)).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM interview_sessions WHERE user_id=?",
                             (uid,)).fetchone()[0]
        check("多条终态会话可并存（不占锁）", active == 1 and terminal == 3 and total == 4,
              "active=%d terminal=%d total=%d（期望 1/3/4）" % (active, terminal, total))

        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        left = conn.execute("SELECT count(*) FROM interview_sessions "
                            "WHERE user_id=?", (uid,)).fetchone()[0]
        check("删除用户级联删除其会话", left == 0, "剩余 %d 条" % left)
    finally:
        conn.rollback()
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="T-17 迁移 002 验收核对")
    ap.add_argument("db", nargs="?", default=DEFAULT_DB)
    ap.add_argument("--behavioral", action="store_true",
                    help="插删测试数据验证索引/级联行为（仅副本）")
    ap.add_argument("--force", action="store_true",
                    help="允许在 interview.db 上跑 --behavioral（危险，仅在你确知后果时使用）")
    args = ap.parse_args()

    db = os.path.abspath(args.db)
    if not os.path.isfile(db):
        print("库不存在: %s" % db)
        return 1

    if args.behavioral and os.path.basename(db).lower() == "interview.db" and not args.force:
        print("拒绝执行：--behavioral 会写入并删除数据，不能在 interview.db 上运行。")
        print("")
        print("请先做副本，例如：")
        print("  python -c \"import sqlite3; s=sqlite3.connect('interview.db');"
              " d=sqlite3.connect('copy.db'); s.backup(d); d.close(); s.close()\"")
        print("然后：python scripts/verify_t17.py copy.db --behavioral")
        return 1

    print("库: %s" % db)
    print("模式: %s" % ("只读 + 行为验证" if args.behavioral else "只读"))

    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"))
        indexes = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"))
        revisions = [r[0] for r in conn.execute(
            "SELECT revision FROM schema_migrations ORDER BY revision")]
        legacy_counts = {t: conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
                         for t in LEGACY_TABLES}
        new_counts = {t: conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
                      for t in NEW_TABLES}
        columns = {t: [d[1] for d in conn.execute("PRAGMA table_info(%s)" % t)]
                   for t in NEW_TABLES}
        legacy_columns = {t: [d[1] for d in conn.execute("PRAGMA table_info(%s)" % t)]
                          for t in LEGACY_TABLES}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        index_sql = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"))
    finally:
        conn.close()

    section("1. 迁移版本")
    print("  已应用: %s" % ", ".join(revisions))
    check("002 已应用", "002" in revisions, "revisions=%r" % (revisions,))

    section("2. 四张新表与索引")
    for t in NEW_TABLES:
        check("表 %s 存在" % t, t in tables)
    for t, expected in NEW_TABLES.items():
        check("表 %s 的列与规格一致" % t, columns[t] == expected,
              "实际=%s" % ", ".join(columns[t]))
    for idx in INDEXES:
        check("索引 %s 存在" % idx, idx in indexes)

    active_sql = (index_sql.get("idx_sessions_active") or "").upper()
    check("idx_sessions_active 是 UNIQUE + 部分索引",
          "UNIQUE" in active_sql and "WHERE" in active_sql,
          (index_sql.get("idx_sessions_active") or "(缺失)"))

    section("3. 既有表未被 002 改动")
    check("users 未被加列（无 client_token）",
          "client_token" not in legacy_columns.get("users", []))
    check("users 未被加列（无 must_change_password）",
          "must_change_password" not in legacy_columns.get("users", []))
    check("notifications 仍保留 link_url（属 T-18 范围）",
          "link_url" in legacy_columns.get("notifications", []))
    check("四张新表迁移后为空", all(v == 0 for v in new_counts.values()),
          ", ".join("%s=%d" % kv for kv in sorted(new_counts.items())))

    section("4. 既有数据与完整性")
    print("  既有表行数: %s" % ", ".join("%s=%d" % kv for kv in sorted(legacy_counts.items())))
    check("integrity_check = ok", integrity == "ok", integrity)
    check("foreign_key_check 无违规", not fk_violations, "violations=%d" % len(fk_violations))

    if args.behavioral:
        section("5. 行为验证（部分唯一索引 / 级联删除）")
        behavioural_probe(db)

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
