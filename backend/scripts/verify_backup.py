"""T-01 备份可恢复性校验（T-01 的验收测试）。

用途：证明 make_backup.py 产出的数据库备份**真的能被打开并可用**，
      而不是"文件存在即视为成功"。

校验内容：
  1. 文件存在且大小 > 0
  2. PRAGMA integrity_check == ok
  3. 业务表齐全（users / interview_records / notifications）
  4. 能用标准查询读出行数（即真的可查询）
  5. 若提供 manifest，则逐项比对行数与 sha256

用法：
    python backend/scripts/verify_backup.py --db backup/interview_20260928_120000.db
    python backend/scripts/verify_backup.py --db <db> --manifest backup/manifest_xxx.json

退出码：0 = 通过；1 = 校验失败；2 = 用法/文件错误。
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys

EXPECTED_TABLES = {"users", "interview_records", "notifications"}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_db(db_path):
    """以只读方式打开备份，返回结构信息。失败则抛异常。"""
    uri = "file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        counts = {}
        for t in tables:
            counts[t] = conn.execute("SELECT COUNT(*) FROM [%s]" % t).fetchone()[0]
        return {"integrity_check": integrity, "tables": tables, "row_counts": counts}
    finally:
        conn.close()


def verify(db_path, manifest_path=None):
    """返回 (ok: bool, problems: list[str], info: dict)。"""
    problems = []

    if not os.path.isfile(db_path):
        return False, ["backup file not found: %s" % db_path], {}
    if os.path.getsize(db_path) == 0:
        return False, ["backup file is empty: %s" % db_path], {}

    try:
        info = inspect_db(db_path)
    except sqlite3.Error as exc:
        return False, ["cannot open backup as SQLite database: %s" % exc], {}

    if info["integrity_check"] != "ok":
        problems.append("integrity_check = %r (expected 'ok')" % info["integrity_check"])

    missing = EXPECTED_TABLES - set(info["tables"])
    if missing:
        problems.append("missing expected tables: %s" % sorted(missing))

    if manifest_path:
        if not os.path.isfile(manifest_path):
            problems.append("manifest not found: %s" % manifest_path)
        else:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            db_meta = manifest.get("database", {})

            if db_meta.get("sha256"):
                actual = sha256_of(db_path)
                if actual != db_meta["sha256"]:
                    problems.append(
                        "sha256 mismatch: manifest=%s actual=%s"
                        % (db_meta["sha256"][:16], actual[:16])
                    )

            for table, expected in (db_meta.get("row_counts") or {}).items():
                actual = info["row_counts"].get(table)
                if actual != expected:
                    problems.append(
                        "row_count mismatch for %s: manifest=%s actual=%s"
                        % (table, expected, actual)
                    )

    return (not problems), problems, info


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify a database backup is restorable.")
    ap.add_argument("--db", required=True, help="path to the .db backup")
    ap.add_argument("--manifest", default=None, help="optional manifest json to cross-check")
    args = ap.parse_args(argv)

    ok, problems, info = verify(args.db, args.manifest)

    if info:
        print("tables    : %s" % ", ".join(info["tables"]))
        print("row_counts: %s" % json.dumps(info["row_counts"]))
        print("integrity : %s" % info["integrity_check"])

    if ok:
        print("VERIFY_OK %s" % args.db)
        return 0

    for p in problems:
        print("PROBLEM: %s" % p, file=sys.stderr)
    print("VERIFY_FAILED %s" % args.db, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
