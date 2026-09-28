"""T-15 人工验收工具 —— SQLite engine 运行契约独立复算。

设计原则
--------
1. **默认不触碰真库**：先把真库用 sqlite3 backup API 复制一份（WAL 安全），
   所有断言都在副本上做。真库只做一次 `mode=ro` 只读核对。
2. **独立复算**：这里不 import 测试文件，用原始 sqlite3 + SQLAlchemy
   各自独立验证同一批结论。测试与验收工具同时错的可能性更低。
3. 退出码 0 = 全部通过；1 = 有失败项（可直接用于 CI/脚本判断）。

用法
----
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t15.py
    .\\venv\\Scripts\\python.exe scripts\\verify_t15.py --keep      # 保留副本
    .\\venv\\Scripts\\python.exe scripts\\verify_t15.py --db X.db   # 指定库（会转为 WAL）
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

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


def db_path_of(database):
    """从 DATABASE_URL 取出本地文件路径（仅支持 sqlite:///）。"""
    return database.DATABASE_URL.replace("sqlite:///", "")


# ---------------------------------------------------------------------------
# 1. 引擎契约（仅 PRAGMA / 配置层）
# ---------------------------------------------------------------------------
def check_engine_contract(database):
    """不建立连接就无法看 PRAGMA，但连接副本是安全的。"""
    from sqlalchemy import text

    section("1. 引擎契约（PRAGMA 生效值）")

    with database.engine.connect() as conn:
        rows = {
            "journal_mode": conn.exec_driver_sql("PRAGMA journal_mode").scalar(),
            "busy_timeout": conn.exec_driver_sql("PRAGMA busy_timeout").scalar(),
            "synchronous": conn.exec_driver_sql("PRAGMA synchronous").scalar(),
            "foreign_keys": conn.exec_driver_sql("PRAGMA foreign_keys").scalar(),
        }

    check("journal_mode = wal", str(rows["journal_mode"]).lower() == "wal", repr(rows["journal_mode"]))
    check("busy_timeout = 15000", int(rows["busy_timeout"]) == 15000, repr(rows["busy_timeout"]))
    check("synchronous = NORMAL(1)", int(rows["synchronous"]) == 1, repr(rows["synchronous"]))
    check("foreign_keys = ON(1)", int(rows["foreign_keys"]) == 1, repr(rows["foreign_keys"]))

    # 单一来源：驱动层 timeout 必须与 busy_timeout 同值，否则"以为等 15 秒其实 5 秒就失败"
    args = database.SQLITE_CONNECT_ARGS
    same = abs(float(args["timeout"]) * 1000 - database.SQLITE_BUSY_TIMEOUT_MS) < 1e-6
    check("驱动层 timeout 与 busy_timeout 同源同值", same, "timeout=%r ms=%r" % (args["timeout"], database.SQLITE_BUSY_TIMEOUT_MS))

    # 连接级验证：PRAGMA 是逐连接的，必须换新连接再看一次
    ok_new = True
    detail = []
    for i in range(3):
        with database.engine.connect() as conn:
            fk = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
            jm = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        detail.append("conn%d fk=%s jm=%s" % (i, fk, jm))
        if int(fk) != 1 or str(jm).lower() != "wal":
            ok_new = False
    check("每一条新连接都重新设置 PRAGMA（连接级）", ok_new, "; ".join(detail))

    # WAL 是**文件属性**：第三方只读工具也应看到 wal
    #
    # 注意：这里必须显式 close()。`with sqlite3.connect(...)` 只提交事务、
    # **不关闭连接** —— 遗留的读事务会在 WAL 下阻止后续切换 journal_mode
    # （踩过一次：next 步的 PRAGMA journal_mode=DELETE 报 database is locked）。
    raw = sqlite3.connect(db_path_of(database))
    try:
        raw_mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        raw.close()
    check("WAL 已落到文件（原始 sqlite3 连接同样看到）", str(raw_mode).lower() == "wal", repr(raw_mode))

    # DBAPI 层必须退出 legacy 事务模式（ADR-002R 的核心修正）
    #
    # 注意：必须先前 dispose 连接池。连接池复用已有 DBAPI 连接时不会再触发
    # "connect" 事件 —— 首版验收工具就在这里踩了坑：captured 一直是空的，
    # 却因为 .get() 的默认值恰好也是 None 而"看起来像通过"。
    database.engine.dispose()
    captured = {}

    def grab(dbapi_conn, _rec):
        captured["isolation_level"] = dbapi_conn.isolation_level

    from sqlalchemy import event as sa_event

    sa_event.listen(database.engine, "connect", grab)
    try:
        with database.engine.connect():
            pass
    finally:
        sa_event.remove(database.engine, "connect", grab)
    check(
        "DBAPI isolation_level 已关闭（None，非 legacy ''）",
        "isolation_level" in captured and captured["isolation_level"] is None,
        "捕获到=%s (dbapi=%r / dialect=%r)"
        % (
            "是" if "isolation_level" in captured else "否（事件未触发）",
            captured.get("isolation_level"),
            database.engine.dialect.isolation_level,
        ),
    )
    check(
        "SQLAlchemy dialect isolation_level = None",
        database.engine.dialect.isolation_level is None,
        repr(database.engine.dialect.isolation_level),
    )


def check_bare_connection_pragmas(database):
    """直接验证 `_apply_sqlite_pragmas` 这一函数本身，用**独立的一次性库文件**。

    为什么必须"绕开引擎"：`sqlite3.connect(timeout=15.0)` 自己就会把
    busy_timeout 设成 15000（实测 timeout=0.001 -> 1ms、5.0 -> 5000ms）。
    因此在引擎路径上，`PRAGMA busy_timeout` 那一行是**观测冗余**的 ——
    破坏性验证证实：把它整行删掉，任何引擎级断言都不会失败。
    只有用 timeout=0.001 的裸连接才具备判别力（before=1 / after=15000）。

    为什么用独立库文件：engine 的连接池会保持若干已打开的连接，
    在 WAL 下会阻止同一文件上的 `PRAGMA journal_mode=DELETE`（踩过一次：
    database is locked）。独立文件同时还多验证了一件事 ——
    **全新空库也能被正确设为 WAL**。
    """
    section("1b. 裸连接直接验证 _apply_sqlite_pragmas（独立一次性库）")

    fd, fresh = tempfile.mkstemp(prefix="t15-fresh-", suffix=".db")
    os.close(fd)
    try:
        raw = sqlite3.connect(fresh, timeout=0.001)
        try:
            before = raw.execute("PRAGMA busy_timeout").fetchone()[0]
            fresh_jm = raw.execute("PRAGMA journal_mode").fetchone()[0]
            fresh_fk = raw.execute("PRAGMA foreign_keys").fetchone()[0]
            check(
                "前置条件：裸连接默认未开外键、journal 非 WAL",
                fresh_fk == 0 and str(fresh_jm).lower() != "wal",
                "fk=%r journal_mode=%r busy_timeout=%r" % (fresh_fk, fresh_jm, before),
            )
            check(
                "前置条件：驱动层 timeout 只给出 1ms（保证本项有判别力）",
                before == 1,
                "busy_timeout=%r" % (before,),
            )

            database._apply_sqlite_pragmas(raw, None)

            after = raw.execute("PRAGMA busy_timeout").fetchone()[0]
            got = {
                "journal_mode": raw.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": raw.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": raw.execute("PRAGMA foreign_keys").fetchone()[0],
                "isolation_level": raw.isolation_level,
            }
            check(
                "PRAGMA busy_timeout 这行本身生效（1ms -> 15000ms，引擎路径测不出）",
                before == 1 and after == database.SQLITE_BUSY_TIMEOUT_MS,
                "before=%r after=%r" % (before, after),
            )
            check(
                "全新空库被设为 WAL", str(got["journal_mode"]).lower() == "wal", repr(got["journal_mode"])
            )
            check("synchronous = NORMAL(1)", got["synchronous"] == 1, repr(got["synchronous"]))
            check("foreign_keys = ON(1)", got["foreign_keys"] == 1, repr(got["foreign_keys"]))
            check(
                "DBAPI isolation_level 被置为 None",
                got["isolation_level"] is None,
                repr(got["isolation_level"]),
            )
        finally:
            raw.close()

        # 幂等性：重复应用不应报错、结果一致
        raw = sqlite3.connect(fresh, timeout=0.001)
        try:
            database._apply_sqlite_pragmas(raw, None)
            database._apply_sqlite_pragmas(raw, None)
            twice = raw.execute("PRAGMA busy_timeout").fetchone()[0]
            check("重复应用是幂等的", twice == database.SQLITE_BUSY_TIMEOUT_MS, repr(twice))
        finally:
            raw.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(fresh + suffix)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 2. BEGIN IMMEDIATE 行为判别（不看日志，看锁行为）
# ---------------------------------------------------------------------------
def check_begin_immediate(database):
    section("2. BEGIN IMMEDIATE 行为判别（黑盒，不看日志）")

    db_path = db_path_of(database)

    # 让引擎只执行一条 SELECT —— 若发出的是 BEGIN IMMEDIATE，它此时已持写锁
    conn = database.engine.connect()
    try:
        conn.exec_driver_sql("SELECT 1")
        verdict = None
        try:
            probe = sqlite3.connect(db_path, timeout=0.3)
            try:
                probe.execute("BEGIN IMMEDIATE")
                verdict = "not-locked"
            except sqlite3.OperationalError as exc:
                verdict = "locked: %s" % exc
            finally:
                probe.close()
        except Exception as exc:  # noqa: BLE001
            verdict = "probe-error: %s" % exc

        check(
            "只读事务即持写锁 => 发出的是 BEGIN IMMEDIATE（而非 DEFERRED）",
            str(verdict).startswith("locked"),
            verdict,
        )

        # 对照：WAL 下 RESERVED 锁**不阻塞**其它连接的纯读
        try:
            reader = sqlite3.connect(db_path, timeout=0.3)
            try:
                val = reader.execute("SELECT count(*) FROM users").fetchone()[0]
                read_ok = True
            finally:
                reader.close()
        except Exception as exc:  # noqa: BLE001
            read_ok, val = False, exc
        check("对照组：WAL 下并发纯读不受写锁影响", read_ok, "users=%r" % (val,))
    finally:
        conn.close()

    # 事务结束后写锁必须释放（验证 dbapi isolation_level=None 未破坏 commit 路径）
    try:
        probe = sqlite3.connect(db_path, timeout=0.3)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            released = True
            err = ""
        except sqlite3.OperationalError as exc:
            released, err = False, str(exc)
        finally:
            probe.close()
    except Exception as exc:  # noqa: BLE001
        released, err = False, str(exc)
    check("事务结束后写锁已释放（commit/rollback 路径未被破坏）", released, err or "acquired")


# ---------------------------------------------------------------------------
# 3. 外键真的生效（副本上做，含负向断言）
# ---------------------------------------------------------------------------
def check_foreign_keys(database):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    section("3. 外键真生效（副本库，破坏性但可回滚）")

    # 负向 1：插入孤儿行必须被拒
    orphan_rejected, detail = False, ""
    try:
        with database.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO interview_records (user_id, role, messages, status) "
                    "VALUES (999999, 'probe', '[]', 'pending')"
                )
            )
        detail = "插入竟然成功 —— 外键未生效！"
    except IntegrityError as exc:
        orphan_rejected = True
        detail = str(exc.orig)
    except Exception as exc:  # noqa: BLE001
        detail = "预期 IntegrityError，实得 %s: %s" % (type(exc).__name__, exc)
    check("插入不存在用户的记录被拒（IntegrityError）", orphan_rejected, detail)

    # 负向 2：删除仍有记录的用户必须被拒
    delete_rejected, detail = False, ""
    try:
        with database.engine.begin() as conn:
            target = conn.execute(
                text(
                    "SELECT u.id FROM users u JOIN interview_records r ON r.user_id = u.id LIMIT 1"
                )
            ).scalar()
            if target is None:
                detail = "副本中找不到'有记录的用户'，跳过"
                check("删除仍有记录的用户被拒（IntegrityError）", False, detail)
                return
            conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": target})
        detail = "删除竟然成功 —— 外键未生效！(uid=%s)" % target
    except IntegrityError as exc:
        delete_rejected = True
        detail = "uid 被拒: %s" % str(exc.orig)
    except Exception as exc:  # noqa: BLE001
        detail = "预期 IntegrityError，实得 %s: %s" % (type(exc).__name__, exc)
    check("删除仍有记录的用户被拒（IntegrityError）", delete_rejected, detail)

    # 正向：自带事务的写入与回滚仍然正常（外键开启 + isolation_level=None 的回归）
    try:
        with database.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO notifications (user_id, type, message, is_read) "
                    "VALUES ((SELECT id FROM users LIMIT 1), 'system', 't15-probe', 0)"
                )
            )
        with database.engine.begin() as conn:
            n = conn.execute(text("SELECT count(*) FROM notifications WHERE message='t15-probe'")).scalar()
        ok, detail = n == 1, "插入并提交成功，count=%s" % n
        # 清理，保持副本数据面干净（若失败也不影响真库）
        with database.engine.begin() as conn:
            conn.execute(text("DELETE FROM notifications WHERE message='t15-probe'"))
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
    check("开启外键 + isolation_level=None 后正常写入仍可用", ok, detail)


# ---------------------------------------------------------------------------
# 4. 并发两事务串行化（已知代价，显式确认而不是当意外）
# ---------------------------------------------------------------------------
def check_serialization(database):
    section("4. 已知代价：经由本引擎的并发事务会串行化（ADR-002R）")

    db_path = db_path_of(database)
    c1 = database.engine.connect()
    try:
        c1.exec_driver_sql("SELECT 1")  # 持写锁
        blocked = False
        try:
            with database.engine.connect() as c2:
                c2.exec_driver_sql("SELECT 1")
        except Exception:  # noqa: BLE001
            blocked = True
        check("第二个引擎事务被第一个挡住（串行化的直接证据）", blocked, "blocked=%s" % blocked)
    finally:
        c1.close()

    # 释放后必须立刻可再用（不能死锁）
    ok, detail = False, ""
    try:
        with database.engine.connect() as c3:
            c3.exec_driver_sql("SELECT 1")
        ok, detail = True, "写锁已释放，可正常使用"
    except Exception as exc:  # noqa: BLE001
        detail = "%s: %s" % (type(exc).__name__, exc)
    check("释放后引擎可继续使用（无死锁残留）", ok, detail)


# ---------------------------------------------------------------------------
# 5. 真库只读核对（唯一接触真库的地方，mode=ro）
# ---------------------------------------------------------------------------
def check_real_db_readonly(real_db):
    section("5. 真库只读核对（mode=ro，零写入）")

    if not os.path.exists(real_db):
        check("真库存在", False, real_db)
        return
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % real_db.replace("\\", "/"), uri=True)
    except sqlite3.OperationalError as exc:
        check("真库可以只读打开", False, str(exc))
        return

    try:
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' order by name")]
        users = conn.execute("select count(*) from users").fetchone()[0]
        records = conn.execute("select count(*) from interview_records").fetchone()[0]
        notifs = conn.execute("select count(*) from notifications").fetchone()[0]
        revs = [r[0] for r in conn.execute("select revision from schema_migrations order by revision")]
        orphan_r = conn.execute(
            "select count(*) from interview_records r left join users u on u.id=r.user_id where u.id is null"
        ).fetchone()[0]
        orphan_n = conn.execute(
            "select count(*) from notifications n left join users u on u.id=n.user_id where u.id is null"
        ).fetchone()[0]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        jm = conn.execute("PRAGMA journal_mode").fetchone()[0]

        print("  真库: %s" % real_db)
        print("  表: %s" % ", ".join(tables))
        print("  数据: users=%d records=%d notifications=%d" % (users, records, notifs))
        print("  迁移版本: %s" % (revs or "(无)"))
        print("  当前 journal_mode: %s" % jm)

        check("迁移状态为 managed 且已应用 001", revs == ["001"], "revisions=%r" % (revs,))
        check("无孤儿行（开启外键的前置条件）", orphan_r == 0 and orphan_n == 0,
              "records=%d notifications=%d" % (orphan_r, orphan_n))
        check("foreign_key_check 无违规", len(fk_violations) == 0, "violations=%d" % len(fk_violations))
        check("schema_migrations 列名为 revision（非 version）", True, "revs=%r" % (revs,))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="T-15 人工验收：SQLite engine 运行契约")
    parser.add_argument("--db", help="要验证的库路径（默认：真库的副本，零风险）")
    parser.add_argument("--keep", action="store_true", help="保留临时副本以便人工查看")
    args = parser.parse_args()

    real_db = os.path.join(BACKEND_DIR, "interview.db")
    tmpdir = None

    if args.db:
        target = os.path.abspath(args.db)
        print("注意：将对指定库 %s 执行验证，它会被转为 WAL 模式。" % target)
    else:
        tmpdir = tempfile.mkdtemp(prefix="t15-verify-")
        target = os.path.join(tmpdir, "copy.db")
        # 用 backup API 复制，WAL 模式下也能拿到一致快照
        src = sqlite3.connect(real_db)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        print("已在副本上验证（真库不会被执行引擎连接）：")
        print("  副本 = %s" % target)

    os.environ["DATABASE_URL"] = "sqlite:///" + target.replace("\\", "/")

    try:
        import database  # noqa: E402  —— 必须在 DATABASE_URL 设定之后导入
    except Exception as exc:  # noqa: BLE001
        print("导入 database 失败: %s: %s" % (type(exc).__name__, exc))
        return 1

    check_engine_contract(database)
    check_bare_connection_pragmas(database)
    check_begin_immediate(database)
    check_foreign_keys(database)
    check_serialization(database)
    check_real_db_readonly(real_db)

    section("汇总")
    failed = [r for r in RESULTS if not r[0]]
    print("  共 %d 项，通过 %d 项，失败 %d 项" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for _, name, detail in failed:
        print("  FAIL: %s  -> %s" % (name, detail))

    if tmpdir:
        if args.keep:
            print("  副本保留在: %s" % tmpdir)
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)

    print("")
    print("结论: %s" % ("全部通过 [ALL PASS]" if not failed else "存在失败项 [FAILED]"))
    return 0 if not failed else 1


if __name__ == "__main__":
    # 控制台可能是 GBK，中文/符号会抛 UnicodeEncodeError 而掩盖真实结论
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  (Python < 3.7 或非常规 stdout)
        pass
    sys.exit(main())
