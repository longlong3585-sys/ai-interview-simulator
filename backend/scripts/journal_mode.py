"""查看 / 切换 SQLite 库的 journal_mode（WAL <-> DELETE）。

背景
----
T-15 让应用在**每条连接**上执行 `PRAGMA journal_mode=WAL`。WAL 是**库文件属性**，
只要应用启动过一次（并建立过连接），`interview.db` 就会被永久转为 WAL，
并出现 `interview.db-wal` / `interview.db-shm` 两个附属文件。

这是**预期且安全**的（ADR-002 / ADR-004R 已批准），但要能随时看清状态、
也能随时还原，所以提供本工具。

为什么不用一行 python -c：Windows PowerShell 会把内联代码里的引号吃掉，
本项目的日志里已经踩过两次同类问题。脚本文件没有这个风险。

用法
----
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\journal_mode.py --status
    .\\venv\\Scripts\\python.exe scripts\\journal_mode.py --to wal
    .\\venv\\Scripts\\python.exe scripts\\journal_mode.py --to delete
    .\\venv\\Scripts\\python.exe scripts\\journal_mode.py --db 某库.db --status

注意：切换前必须**停掉后端服务**，否则会因其它连接持锁而报 database is locked。
"""
import argparse
import os
import sqlite3
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BACKEND_DIR, "interview.db")


def status(path):
    if not os.path.exists(path):
        print("库不存在: %s" % path)
        return 1

    size = os.path.getsize(path)
    side = {}
    for suffix in ("-wal", "-shm"):
        p = path + suffix
        side[suffix] = os.path.getsize(p) if os.path.exists(p) else None

    conn = sqlite3.connect(path, timeout=5.0)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    print("库文件      : %s" % path)
    print("主文件大小  : %d 字节" % size)
    print("journal_mode: %s" % mode)
    for suffix in ("-wal", "-shm"):
        v = side[suffix]
        print("%-11s : %s" % (suffix, ("%d 字节" % v) if v is not None else "（不存在）"))

    if mode.lower() == "wal":
        print("")
        print("说明: 已处于 WAL。这是应用启动过之后的状态，属预期（ADR-002）。")
        if side["-wal"] is None:
            print("      -wal 不存在说明已 checkpoint 干净，数据全在主文件里。")
    else:
        print("")
        print("说明: 当前不是 WAL —— 应用尚未建立过连接，或已被还原为 %s。" % mode)
    return 0


def switch(path, target):
    if not os.path.exists(path):
        print("库不存在: %s" % path)
        return 1

    conn = sqlite3.connect(path, timeout=5.0)
    try:
        before = conn.execute("PRAGMA journal_mode").fetchone()[0]

        if target == "delete" and str(before).lower() == "wal":
            # 切换 journal_mode 前先把 WAL 内容并回主文件：
            # 否则 -wal 里的已提交数据在没有 -wal 文件时会丢失。
            busy, _log, checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            print("wal_checkpoint(TRUNCATE): busy=%s checkpointed=%s" % (busy, checkpointed))
            if busy != 0:
                print("")
                print("!! checkpoint 未能完成（busy=%s）—— 仍有其它连接在用这个库。" % busy)
                print("!! 请先停掉后端服务（确认 8000 端口无监听者）再重试，不要继续切换。")
                return 1

        after = conn.execute("PRAGMA journal_mode=%s" % target.upper()).fetchone()[0]
    except sqlite3.OperationalError as exc:
        print("切换失败: %s" % exc)
        print("")
        print("常见原因：后端服务还在运行，或别的工具正持有该库。")
        print("请先停服务（并确认 8000 端口无监听者）后重试。")
        return 1
    finally:
        conn.close()

    print("journal_mode: %s -> %s（请求 %s）" % (before, after, target.upper()))

    if target == "delete":
        left = [s for s in ("-wal", "-shm") if os.path.exists(path + s)]
        if left:
            print("注意：附属文件仍存在: %s（通常会在最后一个连接关闭后消失）" % ", ".join(left))
        else:
            print("附属文件 -wal / -shm 已消失。")

    print("")
    return status(path)


def main():
    ap = argparse.ArgumentParser(description="查看/切换 SQLite journal_mode")
    ap.add_argument("--db", default=DEFAULT_DB, help="库路径（默认 backend/interview.db）")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="只查看，不改动")
    group.add_argument("--to", choices=["wal", "delete"], help="切换目标模式")
    args = ap.parse_args()

    path = os.path.abspath(args.db)
    if args.status:
        return status(path)
    return switch(path, args.to)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
