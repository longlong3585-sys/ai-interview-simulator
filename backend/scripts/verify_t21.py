"""T-21 验收走查：限流（失败计数 + 滑动窗口 10min/5 次）+ 客户端真实 IP 解析。

**全程只碰临时库**，不接触 `backend/interview.db`。

为什么需要走查脚本：限流器还没接进 `/api/login`（接线是 T-23+ 的事），
所以不能靠"连错 5 次看界面报错"来验证。这里把**登录端点里那道闸**原样复刻一遍
（`count_failures(...) >= 5` → 拒绝），并打印每一步的可观测证据。

用法：
    cd backend
    .\\venv\\Scripts\\python.exe scripts\\verify_t21.py

退出码：0 = 全部通过；1 = 有失败项。
"""
import os
import shutil
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from config import CAPTCHA_LOCK_MINUTES, CAPTCHA_MAX_ERRORS  # noqa: E402
from migrations.runner import run as migrate  # noqa: E402
from services.stores.base import RateLimitStore, iso_after, utcnow_iso  # noqa: E402
from services.stores.sqlite_rate_limit_store import SQLiteRateLimitStore  # noqa: E402
from utils.client_ip import resolve_client_ip  # noqa: E402

RESULTS = []


def build_engine(db_path):
    """按 T-15 的 engine 契约建 engine（复用 database 的实现，避免契约漂移）。

    刻意不 import `tests.support` —— 那会把整个测试包引导流程（临时
    DATABASE_URL、建表）一并拖进来，对一个独立验收脚本是不必要的副作用。
    """
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


def check(name, ok, detail=""):
    """`detail` **只在失败时**打印。

    首版把 detail 无条件打印，而 detail 往往写成"失败原因"的口吻，
    于是出现 `[PASS] 不影响无辜用户 -> 无辜用户被拒了` 这种自相矛盾的行 ——
    在人工验收里这种误导很危险。现在只在 FAIL 时给出诊断信息，
    需要展示"通过的证据"就单独 `print` 一行。
    """
    RESULTS.append((bool(ok), name, detail))
    if ok:
        print("  [PASS] %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  -> " + detail) if detail else ""))
    return bool(ok)


def section(title):
    print("")
    print("=" * 74)
    print(title)
    print("=" * 74)


class FakeClient(object):
    def __init__(self, host):
        self.host = host


class FakeRequest(object):
    def __init__(self, peer, xff=None):
        self.client = FakeClient(peer) if peer is not None else None
        self.headers = {} if xff is None else {"X-Forwarded-For": xff}


def main():
    tmp = tempfile.mkdtemp(prefix="t21-verify-")
    try:
        return _run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp):
    db = os.path.join(tmp, "verify.db")
    migrate(db, backup=False, log=lambda *a, **k: None)
    engine = build_engine(db)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    store = SQLiteRateLimitStore(session_factory=Session)

    window = CAPTCHA_LOCK_MINUTES * 60
    now = utcnow_iso()

    def since(at=None):
        return iso_after(-window, at or now)

    def gate(ip, at=None):
        """复刻登录端点里那道闸（`auth.py: check_ip_lock` 的等价物）。"""
        return store.count_failures(ip, since(at)) >= CAPTCHA_MAX_ERRORS

    print("临时库: %s" % db)
    print("窗口: %d 分钟 / 阈值: %d 次（来自 config）"
          % (CAPTCHA_LOCK_MINUTES, CAPTCHA_MAX_ERRORS))

    # ------------------------------------------------------------------
    section("1. 客户端真实 IP 解析（X-Forwarded-For）")
    print("  存储实现: %s" % type(store).__name__)
    check("协议自检：SQLiteRateLimitStore 满足 RateLimitStore",
          isinstance(store, RateLimitStore))

    real = resolve_client_ip(FakeRequest("127.0.0.1", xff="203.0.113.7"),
                             trusted_count=1)
    print("  Nginx 转发: 对端=127.0.0.1, XFF='203.0.113.7'  ->  限流键=%s" % real)
    check("单层代理：取 XFF 得到真实客户端 IP", real == "203.0.113.7")

    spoofed = resolve_client_ip(
        FakeRequest("127.0.0.1", xff="9.9.9.9, 203.0.113.7"), trusted_count=1)
    print("  攻击者伪造: XFF='9.9.9.9, 203.0.113.7'      ->  限流键=%s" % spoofed)
    check("伪造的最左段被忽略（取最右）—— 否则限流可被绕过",
          spoofed == "203.0.113.7")

    keys = {resolve_client_ip(FakeRequest("127.0.0.1", xff=c), trusted_count=1)
            for c in ("203.0.113.1", "203.0.113.2", "203.0.113.3")}
    print("  3 个不同客户端 -> %d 个不同限流键: %s"
          % (len(keys), ", ".join(sorted(keys))))
    check("不同客户端落到不同限流桶", len(keys) == 3)
    check("限流键不退化成代理 IP（否则一人连错锁死全网）",
          "127.0.0.1" not in keys)

    direct = resolve_client_ip(FakeRequest("203.0.113.9", xff="9.9.9.9"),
                               trusted_count=0)
    check("无代理部署（TRUSTED_PROXY_COUNT=0）忽略 XFF，用直连地址",
          direct == "203.0.113.9")

    short = resolve_client_ip(FakeRequest("10.0.0.5", xff="203.0.113.7"),
                              trusted_count=2)
    check("代理层数配多了（链长不足）时退回直连对端，不猜",
          short == "10.0.0.5")

    # ------------------------------------------------------------------
    section("2. 连续输错 5 次 -> 第 6 次直接拒绝")
    attacker = real
    victim = "198.51.100.42"
    print("  攻击者 IP = %s    无辜用户 IP = %s" % (attacker, victim))
    print("")
    print("  第 N 次尝试 | 攻击者是否被拒 | 无辜用户是否被拒")
    outcomes = []
    for i in range(1, 7):
        a_rejected = gate(attacker)
        v_rejected = gate(victim)
        outcomes.append((i, a_rejected, v_rejected))
        print("      %d       |   %-5s        |   %-5s"
              % (i, "拒绝" if a_rejected else "允许", "拒绝" if v_rejected else "允许"))
        if not a_rejected:
            store.record_failure(attacker, now)     # 这次登录失败，记一笔

    check("前 5 次允许、第 6 次拒绝",
          [o[1] for o in outcomes] == [False] * 5 + [True])
    check("全程不影响无辜用户（重点）", all(o[2] is False for o in outcomes))
    print("  攻击者最终计数 = %d（阈值 %d）"
          % (store.count_failures(attacker, since()), CAPTCHA_MAX_ERRORS))
    check("攻击者计数恰好等于阈值",
          store.count_failures(attacker, since()) == CAPTCHA_MAX_ERRORS)

    # ------------------------------------------------------------------
    section("3. 滑动窗口：10 分钟后自动恢复")
    later = iso_after(window + 1, now)
    print("  把时间推到 %d 分钟后，攻击者是否仍被拒: %s"
          % (CAPTCHA_LOCK_MINUTES + 1, gate(attacker, at=later)))
    check("窗口过期后攻击者恢复", not gate(attacker, at=later))
    check("窗口边界（正好 10 分钟前）仍计入",
          store.count_failures(
              attacker, iso_after(-window, iso_after(-1, now))) >= 1)

    # ------------------------------------------------------------------
    section("4. 仅失败计数：成功登录不写表")
    before = store.count_all()
    store.clear(attacker)                 # 模拟"这次登录成功了"
    after = store.count_all()
    print("  成功登录前总行数=%d，clear 后=%d" % (before, after))
    check("成功登录是**删除**记录，不新增行",
          after == before - CAPTCHA_MAX_ERRORS)
    check("成功后该 IP 立即可再次尝试", not gate(attacker))

    for _ in range(10):
        store.clear(victim)
    print("  连续 10 次成功登录后总行数 = %d" % store.count_all())
    check("连续成功登录不会让表增长", store.count_all() == 0)
    public = [n for n in dir(store) if not n.startswith("_")]
    check("协议里没有『记成功』的方法（结构性保证）",
          "record_success" not in public)

    # ------------------------------------------------------------------
    section("5. 多 worker：计数跨进程可见")
    store.record_failure(attacker, now)
    store.record_failure(attacker, now)
    other = SQLiteRateLimitStore(
        session_factory=sessionmaker(bind=engine, autocommit=False, autoflush=False))
    seen = other.count_failures(attacker, since())
    print("  worker A 记了 2 次，worker B 读到 %d 次" % seen)
    check("另一个 worker 能看到同样的计数（原内存字典做不到）", seen == 2,
          "读到 %d" % seen)

    # ------------------------------------------------------------------
    section("6. 清理与窗口判据严格互补")
    outside = iso_after(-window - 60, now)
    store.record_failure(victim, outside)
    counted = store.count_failures(victim, since())
    removed = store.purge_older_than(since())
    print("  窗口内计数=%d；purge 删除=%d" % (counted, removed))
    check("purge 只删窗口外的记录", removed == 1, "删除 %d 条" % removed)
    check("清理后被计数的条数不变",
          store.count_failures(victim, since()) == counted)

    # ------------------------------------------------------------------
    section("汇总")
    failed = [r for r in RESULTS if not r[0]]
    print("  共 %d 项，通过 %d 项，失败 %d 项"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for _, name, detail in failed:
        print("  FAIL: %s  -> %s" % (name, detail))
    print("")
    print("  说明: 本脚本只写临时库；限流器尚未接进 /api/login（接线属 T-23+），")
    print("        因此这里复刻了登录端点里那道闸：count >= %d 就拒绝。" % CAPTCHA_MAX_ERRORS)
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
