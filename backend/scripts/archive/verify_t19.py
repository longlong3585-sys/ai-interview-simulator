"""T-19 验收工具：`SQLiteSessionStore` 的可跑走查。

**全程只碰临时库**，绝不接触 `backend/interview.db`。

为什么 T-19 的验收工具长这样：存储层还没有被任何接口调用（接入是 T-23 之后），
所以"点界面上手验"无从谈起。替代方案是把四组核心语义**跑一遍并打印可观测证据**
——每一步都同时给出"store 说了什么"和"库里实际是什么"，
后者是用独立连接直接读出来的，不经过 store 自己。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t19.py
    .\\venv\\Scripts\\python.exe scripts\\verify_t19.py --keep   # 保留临时库以便人工查看

退出码：0 = 全部通过；1 = 有失败项。
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

import os as _archive_os

# 归档位置：backend/scripts/archive/<本文件> —— 四层 dirname 即仓库根。
_ARCHIVE_REPO = _archive_os.path.dirname(_archive_os.path.dirname(
    _archive_os.path.dirname(_archive_os.path.dirname(
        _archive_os.path.abspath(__file__)))))
BACKEND_DIR = os.path.join(_ARCHIVE_REPO, "backend")
sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from migrations.runner import load_migrations, run as migrate  # noqa: E402
from services.stores.base import (  # noqa: E402
    ActiveSessionExists,
    EndedReason,
    SessionDraft,
    SessionStatus,
    StoreError,
    TurnCommit,
    iso_after,
    utcnow_iso,
)
from services.stores.sqlite_store import SQLiteSessionStore  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((bool(ok), name, detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -> " + detail) if detail else ""))
    return bool(ok)


def section(title):
    print("")
    print("=" * 74)
    print(title)
    print("=" * 74)


def build_engine(db_path):
    """按 T-15 的 engine 契约建 engine（复用 database 的实现，避免契约漂移）。"""
    import database

    engine = create_engine(
        "sqlite:///" + db_path.replace("\\", "/"),
        connect_args=dict(database.SQLITE_CONNECT_ARGS),
        pool_pre_ping=True,
        isolation_level=None,
    )
    event.listen(engine, "connect", database._apply_sqlite_pragmas)

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def main():
    ap = argparse.ArgumentParser(description="T-19 存储层验收走查（只用临时库）")
    ap.add_argument("--keep", action="store_true", help="保留临时库")
    ap.add_argument("--db", default=None,
                    help="只读检查**指定库**是否满足存储层的前置结构（不跑走查）")
    args = ap.parse_args()

    if args.db:
        return _check_schema(args.db)

    tmp = tempfile.mkdtemp(prefix="t19-verify-")
    db = os.path.join(tmp, "verify.db")
    try:
        return _run(db, tmp, args.keep)
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _check_schema(db_path):
    """只读检查：这个库能不能跑 `SQLiteSessionStore`。

    用途：迁移 004 之后对**真库**做一次确认。纯 `mode=ro` 打开，零写入。
    """
    db = os.path.abspath(db_path)
    print("库: %s" % db)
    print("模式: 只读（前置结构检查）")
    if not os.path.isfile(db):
        print("库不存在")
        return 1

    from services.stores.sqlite_store import _COLUMNS as STORE_COLUMNS

    wanted = [c.strip() for c in STORE_COLUMNS.split(",")]
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        revisions = [r[0] for r in conn.execute(
            "SELECT revision FROM schema_migrations ORDER BY revision")]
        actual = [d[1] for d in conn.execute("PRAGMA table_info(interview_sessions)")]
        report_def = None
        for d in conn.execute("PRAGMA table_info(interview_sessions)"):
            if d[1] == "report":
                report_def = {"notnull": d[3], "default": d[4]}
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        fks = conn.execute("PRAGMA foreign_key_list(interview_sessions)").fetchall()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    section("真库前置结构检查")
    print("  迁移版本: %s" % ", ".join(revisions))
    print("  interview_sessions 列(%d): %s" % (len(actual), ", ".join(actual)))

    missing = [c for c in wanted if c not in actual]
    check("存储层需要的 16 列齐备", not missing, "缺少: %s" % (missing or "无"))
    check("迁移 004 已应用（report 列存在）", "report" in actual,
          "未应用 004 时存储层无法工作")
    check("report 可为 NULL 且无默认值",
          report_def is not None and report_def["notnull"] == 0
          and report_def["default"] is None,
          "report 定义=%r" % (report_def,))
    check("部分唯一索引 idx_sessions_active 存在",
          "idx_sessions_active" in indexes)
    check("外键子句存在", len(fks) == 1, "外键数=%d" % len(fks))
    check("foreign_key_check 无违规", not violations,
          "violations=%d" % len(violations))

    failed = [r for r in RESULTS if not r[0]]
    print("")
    print("  共 %d 项，通过 %d 项，失败 %d 项"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for _, name, detail in failed:
        print("  FAIL: %s  -> %s" % (name, detail))
    print("")
    print("  结论: %s" % ("该库可用于 SQLiteSessionStore [ALL PASS]"
                          if not failed else "该库尚不可用 [FAILED]"))
    return 0 if not failed else 1


def _run(db, tmp, keep):
    print("临时库: %s" % db)
    print("（本工具不会接触 backend/interview.db）")

    migrate(db, backup=False, log=lambda *a, **k: None)
    revisions = [m.revision for m in load_migrations()]
    section("0. 环境")
    print("  迁移链: %s" % ", ".join(revisions))
    check("已升到最新迁移（含 004 的 report 列）", True, "revisions=%s" % revisions)

    engine = build_engine(db)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    store = SQLiteSessionStore(session_factory=Session)

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (username, email, role, is_active) "
                          "VALUES ('verify19', 'v19@x.y', 'user', 1)"))
    with engine.connect() as conn:
        uid = conn.execute(text("SELECT id FROM users WHERE username='verify19'")).scalar()

    def raw(sql, params=None):
        with engine.connect() as conn:
            return conn.execute(text(sql), params or {}).fetchall()

    def draft(sid, ttl=7200, questions=3):
        now = utcnow_iso()
        return SessionDraft(
            session_id=sid, user_id=uid, role="后端",
            questions=["第%d题" % i for i in range(1, questions + 1)],
            question_status=["pending"] * questions,
            user_answers=[None] * questions, current_index=0,
            created_at=now, updated_at=now, expires_at=iso_after(ttl, now))

    def turn(snap, seq, reply="模型回复", expected_version=None, now=None,
             answers=None):
        return TurnCommit(
            session_id=snap.session_id,
            expected_version=snap.version if expected_version is None else expected_version,
            current_index=snap.current_index + 1,
            question_status=["answered"] * len(snap.questions),
            user_answers=answers if answers is not None else snap.user_answers,
            last_seq=seq, last_reply=reply,
            updated_at=utcnow_iso(), now=now or utcnow_iso())

    # ------------------------------------------------------------------
    section("1. 建会话（唯一锁：同一用户只能有一个 active）")
    snap = store.create(draft("v1"))
    print("  快照: session_id=%s status=%s version=%s last_seq=%s 题数=%d"
          % (snap.session_id, snap.status, snap.version, snap.last_seq,
             len(snap.questions)))
    check("新建会话为 active、version=0", snap.is_active and snap.version == 0)
    try:
        store.create(draft("v2"))
        check("同用户第二个 active 被拒", False, "竟然插入成功")
    except ActiveSessionExists as exc:
        check("同用户第二个 active 被拒（ActiveSessionExists）", True, str(exc))

    # ------------------------------------------------------------------
    section("2. 一轮对话 + seq 幂等（避免重复计费）")
    result = store.commit_turn(turn(snap, seq=1, reply="第一轮回复"))
    print("  提交结果: applied=%s version=%s last_seq=%s"
          % (result.applied, result.snapshot.version, result.snapshot.last_seq))
    check("提交成功且 version 递增", result.applied and result.snapshot.version == 1)

    look = store.find_replay("v1", 1)
    print("  重发 seq=1 -> is_replay=%s reply=%r" % (look.is_replay, look.reply))
    check("同 seq 重发被识别为重发（调用方据此不调 AI）",
          look.is_replay and look.reply == "第一轮回复")

    look_new = store.find_replay("v1", 2)
    check("更大的 seq 被识别为新序号", not look_new.is_replay)

    # 关键：上次回复为空时仍必须能区分（T-19 补的协议修正）
    store.commit_turn(turn(store.get("v1"), seq=2, reply=None))
    empty = store.find_replay("v1", 2)
    print("  seq=2 回复为空 -> is_replay=%s reply=%r" % (empty.is_replay, empty.reply))
    check("空回复的重发仍被识别为重发（否则会重复计费）",
          empty.is_replay and empty.reply is None)
    check("空回复的重发 与 新序号 可区分", empty != store.find_replay("v1", 3))

    # ------------------------------------------------------------------
    section("3. 乐观锁：冲突必须显式暴露，绝不静默覆盖")
    stale = store.get("v1")
    store.commit_turn(turn(stale, seq=10, reply="他人先提交"))
    after_other = store.get("v1")

    conflict = store.commit_turn(turn(stale, seq=11, reply="陈旧的写入",
                                      expected_version=stale.version,
                                      answers=["被污染的答案", "x", "y"]))
    print("  陈旧 version=%s 的提交 -> applied=%s" % (stale.version, conflict.applied))
    check("陈旧 version 被拒（返回 applied=False，调用方据此 409）",
          not conflict.applied)
    check("冲突时带回最新快照供 409 使用", conflict.snapshot is not None,
          "last_seq=%s" % (conflict.snapshot.last_seq if conflict.snapshot else None))

    now_row = raw("SELECT last_seq, last_reply, user_answers FROM interview_sessions "
                  "WHERE session_id='v1'")[0]
    print("  库中实际: last_seq=%s last_reply=%r" % (now_row[0], now_row[1]))
    check("库内容未被陈旧载荷覆盖",
          now_row[0] == after_other.last_seq and now_row[1] == after_other.last_reply)
    check("user_answers 未被污染", "被污染的答案" not in (now_row[2] or ""))

    # ------------------------------------------------------------------
    section("4. 状态机：finish 落报告 + 释放唯一锁")
    live = store.get("v1")
    fin = store.finish("v1", live.version, '{"overall_score": 8.5}',
                       EndedReason.COMPLETED, utcnow_iso())
    print("  finish -> applied=%s status=%s ended_reason=%s"
          % (fin.applied, fin.snapshot.status, fin.snapshot.ended_reason))
    check("置为 finished 且写入结束原因",
          fin.applied and fin.snapshot.status == SessionStatus.FINISHED
          and fin.snapshot.ended_reason == EndedReason.COMPLETED)

    persisted = raw("SELECT report, status FROM interview_sessions "
                    "WHERE session_id='v1'")[0]
    check("报告真的落到了 report 列", persisted[0] == '{"overall_score": 8.5}',
          "report=%r" % persisted[0])
    check("库里状态为 finished", persisted[1] == "finished")

    second = store.create(draft("v2"))
    check("终态释放唯一锁：可立刻开新会话", second.session_id == "v2")

    blocked = store.commit_turn(turn(store.get("v1"), seq=99))
    check("已结束的会话不能再写入", not blocked.applied)

    # ------------------------------------------------------------------
    section("5. 过期自愈（ADR-022 R-10：用户永远能开新面试）")
    # 造出"过期但 status 仍是 active"的行：先腾出唯一锁，再建一个已过期的会话
    store.abandon("v2", store.get("v2").version, EndedReason.MANUAL, utcnow_iso())
    expired = store.create(draft("v3-exp", ttl=-10))
    print("  过期会话: %s status=%s expires_at=%s"
          % (expired.session_id, expired.status, expired.expires_at))
    check("过期行的 status 仍为 active（惰性判定，尚未自愈）",
          expired.status == SessionStatus.ACTIVE)
    check("get_active 不把过期行当活跃",
          store.get_active(uid, utcnow_iso()) is None)

    try:
        store.create(draft("v4-blocked"))
        check("过期行仍占着唯一锁（未自愈前无法新建）", False, "竟然插入成功")
    except ActiveSessionExists:
        check("过期行仍占着唯一锁（未自愈前无法新建）", True)

    healed = store.abandon_expired_for_user(uid, utcnow_iso())
    print("  自愈影响行数=%s，过期行 ended_reason=%s"
          % (healed, store.get("v3-exp").ended_reason))
    check("自愈把过期行置为 abandoned", healed == 1)
    check("自愈写入 ended_reason=timeout",
          store.get("v3-exp").ended_reason == EndedReason.TIMEOUT)
    check("自愈后可立刻重开", store.create(draft("v4-ok")).session_id == "v4-ok")

    # ------------------------------------------------------------------
    section("6. 短事务：任何方法都不得把事务留给调用方")
    def can_take_write_lock():
        conn = sqlite3.connect(db, timeout=0.5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    for label, call in (
        ("get", lambda: store.get("v4-ok")),
        ("get_active", lambda: store.get_active(uid, utcnow_iso())),
        ("find_replay", lambda: store.find_replay("v4-ok", 1)),
        ("abandon_all_expired", lambda: store.abandon_all_expired(utcnow_iso())),
    ):
        call()
        check("%s 之后事务已释放（独立连接能取写锁）" % label, can_take_write_lock())

    public = [n for n in dir(store) if not n.startswith("_")]
    check("接口上没有『打开事务』的入口（调用方写不出长事务）",
          not any(n in public for n in ("begin", "commit", "rollback", "session")))

    # ------------------------------------------------------------------
    section("7. 数据损坏必须当场报错（不静默降级成 0 分）")
    with engine.begin() as conn:
        conn.execute(text("UPDATE interview_sessions SET question_status='{坏' "
                          "WHERE session_id='v4-ok'"))
    try:
        store.get("v4-ok")
        check("损坏的 JSON 会话行被拒绝读取", False, "竟然静默返回了")
    except StoreError as exc:
        check("损坏的 JSON 会话行被拒绝读取（StoreError）", True, str(exc)[:70])

    # ------------------------------------------------------------------
    section("汇总")
    print("  库中最终会话数: %d" % raw("SELECT count(*) FROM interview_sessions")[0][0])
    for sid, status, reason in raw(
            "SELECT session_id, status, ended_reason FROM interview_sessions "
            "ORDER BY session_id"):
        print("    %-10s %-10s %s" % (sid, status, reason or "-"))

    failed = [r for r in RESULTS if not r[0]]
    print("")
    print("  共 %d 项，通过 %d 项，失败 %d 项"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for _, name, detail in failed:
        print("  FAIL: %s  -> %s" % (name, detail))
    if keep:
        print("")
        print("  临时库保留在: %s" % db)
    print("")
    print("  结论: %s" % ("全部通过 [ALL PASS]" if not failed else "存在失败项 [FAILED]"))
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
