"""T-15 修订验收：直接观察「读不持写锁、写持写锁」。

不经过测试框架 —— 用独立连接真的去抢写锁，看能不能抢到。
这是判断"锁在谁手里"最直接的行为证据。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\probe_t15r.py
    .\\venv\\Scripts\\python.exe scripts\\probe_t15r.py --deadlock-scenario

只用**临时库**，不碰 backend/interview.db。
"""
import argparse
import os
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

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    """`detail` **只在失败时**打印 —— 这些 detail 都写成"失败原因"的口吻，
    无条件打印会出现 `[PASS] … -> 读仍在持写锁` 这种自相矛盾的行
    （T-21 的验收脚本踩过同一个坑）。"""
    RESULTS.append((bool(ok), name, detail))
    if ok:
        print("  [PASS] %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  -> " + detail) if detail else ""))
    return bool(ok)


def build_engine(db_path):
    """按 T-15 修订后的 engine 契约建 engine（复用生产的 PRAGMA 实现）。"""
    from sqlalchemy import create_engine, event

    import database

    engine = create_engine(
        "sqlite:///" + db_path.replace("\\", "/"),
        connect_args=dict(database.SQLITE_CONNECT_ARGS),
        pool_pre_ping=True,
        isolation_level=None,
    )
    event.listen(engine, "connect", database._apply_sqlite_pragmas)
    return engine


def can_write(db_path, tag):
    """另一条独立连接此刻能否写入 —— False 表示写锁在别人手里。"""
    conn = sqlite3.connect(db_path, timeout=0.3)
    try:
        conn.execute(
            "INSERT INTO users (username, email, role, is_active) VALUES (?,?,?,1)",
            (tag, tag + "@t.local", "user"),
        )
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="T-15 修订验收观察")
    ap.add_argument("--deadlock-scenario", action="store_true",
                    help="复现 T-23 的『同请求内鉴权读 + 业务写』场景")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="t15r-")
    try:
        return _run(tmp, args.deadlock_scenario)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp, deadlock_scenario):
    from migrations.runner import run as migrate
    from services.stores._sqlite_tx import begin_write

    db = os.path.join(tmp, "probe.db")
    migrate(db, backup=False, log=lambda *a, **k: None)
    engine = build_engine(db)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (username,email,role,is_active) "
                          "VALUES ('t15r','t15r@x.local','user',1)"))

    print("临时库: %s" % db)
    print("")

    if deadlock_scenario:
        print("=" * 70)
        print("T-23 场景：同一「请求」内先读用户、再调存储层写")
        print("=" * 70)
        s = Session()
        try:
            # 1) 模拟 get_current_user：读一次用户
            row = s.execute(text("SELECT id FROM users WHERE username='t15r'")).fetchone()
            uid = row[0]
            print("  读用户（模拟 get_current_user）      : ok (id=%s)" % uid)
            # 2) 模拟处理函数里调用存储层写入（**同一请求**，鉴权 session 尚未关闭）
            from services.stores.sqlite_store import SQLiteSessionStore
            store = SQLiteSessionStore(session_factory=Session)
            from services.stores.base import SessionDraft, iso_after, utcnow_iso
            now = utcnow_iso()
            snap = store.create(SessionDraft(
                session_id="t15r-1", user_id=uid, role="r",
                questions=["q"], question_status=["pending"], user_answers=[None],
                current_index=0, created_at=now, updated_at=now,
                expires_at=iso_after(3600, now)))
            print("  紧接着写会话（模拟 store.create）    : ok (session=%s)"
                  % snap.session_id)
            check("同请求内『鉴权读 + 业务写』不再自锁", True)
        except sqlite3.OperationalError as exc:
            check("同请求内『鉴权读 + 业务写』不再自锁", False,
                  "database is locked（修复前就是这个问题）：%s" % exc)
        finally:
            s.close()
    else:
        print("=" * 70)
        print("1) 只读事务是否仍持写锁？（修订后应当 **不持**）")
        print("=" * 70)
        conn = engine.connect()
        try:
            conn.execute(text("SELECT 1"))
            wrote = can_write(db, "t15r_read")
            check("只读期间另一连接能写入（读不持写锁）", wrote,
                  "读仍在持写锁 —— T-23 的同请求死锁会复现")
        finally:
            conn.close()

        print("")
        print("=" * 70)
        print("2) 显式 begin_write 之后呢？（应当 **持锁**）")
        print("=" * 70)
        conn = engine.connect()
        try:
            begin_write(conn)
            blocked = not can_write(db, "t15r_write")
            check("写事务期间另一连接被挡住（写保护仍在）", blocked,
                  "写锁没生效 —— ADR-004 的并发控制失效")
        finally:
            conn.close()

        print("")
        print("=" * 70)
        print("3) 提交后写锁是否释放？")
        print("=" * 70)
        conn = engine.connect()
        try:
            begin_write(conn)
            conn.execute(text(
                "INSERT INTO users (username,email,role,is_active) "
                "VALUES ('t15r_c','t15r_c@x.local','user',1)"))
            conn.commit()
        finally:
            conn.close()
        check("提交后另一连接能写入（锁已释放）", can_write(db, "t15r_after"),
              "锁没释放，后续请求会一直排队")

    print("")
    failed = [r for r in RESULTS if not r[0]]
    print("  共 %d 项，通过 %d 项，失败 %d 项"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for _, name, detail in failed:
        print("  FAIL: %s  -> %s" % (name, detail))
    print("")
    print("  结论: %s" % ("全部通过 [ALL PASS]" if not failed else "存在失败项 [FAILED]"))
    engine.dispose()
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
