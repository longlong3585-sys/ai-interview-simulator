"""T-21 测试：`SQLiteRateLimitStore`。

任务描述的两个关键词：
  * **仅失败计数** —— 成功路径**不写**这张表（ADR-014）；
  * **滑动窗口 10min / 5 次** —— 第 5 次失败后第 6 次被拒，窗口过期后恢复。

其中"多 worker 下真的生效"是本任务存在的理由：原实现用模块级字典，
`uvicorn --workers 2` 时每个进程一份 → 限流上限被 worker 数**放大**。
所以这里有一条用例专门断言"数据在**另一个连接/另一个 store 实例**里也看得见"。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from services.stores.base import RateLimitStore, iso_after, utcnow_iso  # noqa: E402
from services.stores.sqlite_rate_limit_store import SQLiteRateLimitStore  # noqa: E402
from tests.support import build_temp_db  # noqa: E402

WINDOW_MINUTES = 10
MAX_ERRORS = 5


class RateLimitHarness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t21-rate-")
        self.db = os.path.join(self.tmp, "r.db")
        self.engine = build_temp_db(self.db)
        self.Session = sessionmaker(bind=self.engine, autocommit=False,
                                    autoflush=False)
        self.store = SQLiteRateLimitStore(session_factory=self.Session)
        self.now = utcnow_iso()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def window_start(self, now=None):
        return iso_after(-WINDOW_MINUTES * 60, now or self.now)

    def record(self, ip, at=None):
        self.store.record_failure(ip, at or self.now)

    def count(self, ip, now=None):
        return self.store.count_failures(ip, self.window_start(now))

    def raw(self, sql, params=None):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).fetchall()


class ProtocolConformanceTests(RateLimitHarness):

    def test_satisfies_protocol(self):
        self.assertIsInstance(self.store, RateLimitStore)

    def test_factory_registers_it_without_touching_assembly_logic(self):
        """表驱动装配：新增存储只加一行 `_REGISTRY`。"""
        from services.stores import factory

        factory.reset_stores()
        store = factory.get_rate_limit_store()
        self.assertIsInstance(store, RateLimitStore)
        self.assertIs(factory.get_rate_limit_store(), store, "应当是单例")
        self.assertIsNot(store, factory.get_session_store())
        self.assertIsNot(store, factory.get_captcha_store())

    def test_registry_row_is_what_makes_it_available(self):
        """把 _REGISTRY 的那一行去掉，装配点就认不出这个 kind —— 证明它是表驱动。"""
        from services.stores import factory

        saved = factory._REGISTRY.pop("rate_limit")
        factory.reset_rate_limit_store()
        try:
            with self.assertRaises(KeyError):
                factory.get_rate_limit_store()
        finally:
            factory._REGISTRY["rate_limit"] = saved
            factory.reset_rate_limit_store()

    def test_factory_rejects_implementation_missing_methods(self):
        from services.stores import factory

        class HalfBaked(object):
            def count_failures(self, ip, since):
                return 0

        saved = factory._REGISTRY["rate_limit"]
        factory._REGISTRY["rate_limit"] = (saved[0], saved[1], saved[2], HalfBaked)
        factory.reset_rate_limit_store()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                factory.get_rate_limit_store()
            self.assertIn("RateLimitStore", str(ctx.exception))
        finally:
            factory._REGISTRY["rate_limit"] = saved
            factory.reset_rate_limit_store()


class CountingTests(RateLimitHarness):

    def test_no_failures_initially(self):
        self.assertEqual(self.count("1.1.1.1"), 0)

    def test_records_are_counted(self):
        for _ in range(3):
            self.record("1.1.1.1")
        self.assertEqual(self.count("1.1.1.1"), 3)

    def test_ips_are_isolated(self):
        """**关键**：一个 IP 的失败不能影响另一个 IP。"""
        for _ in range(5):
            self.record("1.1.1.1")
        self.assertEqual(self.count("1.1.1.1"), 5)
        self.assertEqual(self.count("2.2.2.2"), 0, "别的 IP 被连累了")

    def test_window_excludes_older_records(self):
        """滑动窗口：窗口外的记录不计入。"""
        old = iso_after(-WINDOW_MINUTES * 60 - 60, self.now)   # 11 分钟前
        self.record("1.1.1.1", at=old)
        self.record("1.1.1.1", at=self.now)                    # 刚刚
        self.assertEqual(self.count("1.1.1.1"), 1, "窗口外的记录被算进来了")

    def test_window_boundary_is_inclusive(self):
        """`since`（含）—— 与 `purge_older_than` 的 `< before` 严格互补。"""
        boundary = self.window_start()
        self.record("1.1.1.1", at=boundary)
        self.assertEqual(self.count("1.1.1.1"), 1,
                         "边界记录应当被计入（否则与 purge 判据不互补）")

    def test_window_recovers_after_expiry(self):
        """10 分钟窗口过期后恢复。"""
        for _ in range(5):
            self.record("1.1.1.1", at=iso_after(-WINDOW_MINUTES * 60 - 1, self.now))
        self.assertEqual(self.count("1.1.1.1"), 0, "过期记录仍被计数 —— 用户被永久锁死")
        self.assertEqual(self.count("1.1.1.1", now=iso_after(1, self.now)), 0)

    def test_count_is_per_ip_not_global(self):
        for _ in range(3):
            self.record("1.1.1.1")
        for _ in range(2):
            self.record("2.2.2.2")
        self.assertEqual(self.count("1.1.1.1"), 3)
        self.assertEqual(self.count("2.2.2.2"), 2)


class ThresholdTests(RateLimitHarness):
    """验收要求：第 5 次失败后，第 6 次被拒。"""

    def gate(self, ip, now=None):
        """复刻登录端点里那道闸：`count >= MAX_ERRORS` 就拒绝。"""
        return self.count(ip, now=now) >= MAX_ERRORS

    def test_sixth_attempt_is_rejected(self):
        ip = "203.0.113.7"
        outcomes = []
        for i in range(6):
            rejected = self.gate(ip)
            outcomes.append(rejected)
            if not rejected:
                self.record(ip)          # 模拟"这次登录失败了"
        self.assertEqual(
            outcomes, [False, False, False, False, False, True],
            "第 6 次应当被拒绝（前 5 次允许并记失败）",
        )
        self.assertEqual(self.count(ip), MAX_ERRORS)

    def test_rejection_does_not_spread_to_other_ips(self):
        """一人连错锁死**自己**，不影响别人 —— 这正是 T-21 的验收点。"""
        bad = "203.0.113.7"
        for _ in range(5):
            self.record(bad)
        self.assertTrue(self.gate(bad), "连错 5 次的 IP 应被拒")
        for other in ("198.51.100.1", "198.51.100.2", "203.0.113.8"):
            self.assertFalse(self.gate(other), "IP %s 被无辜连累" % other)

    def test_recovery_after_window(self):
        ip = "203.0.113.7"
        for _ in range(5):
            self.record(ip)
        self.assertTrue(self.gate(ip))
        later = iso_after(WINDOW_MINUTES * 60 + 1, self.now)
        self.assertFalse(self.gate(ip, now=later), "窗口过期后应当恢复")

    def test_config_values_match_architecture(self):
        from config import CAPTCHA_LOCK_MINUTES, CAPTCHA_MAX_ERRORS

        self.assertEqual(CAPTCHA_MAX_ERRORS, MAX_ERRORS)
        self.assertEqual(CAPTCHA_LOCK_MINUTES, WINDOW_MINUTES)


class OnlyFailuresAreWrittenTests(RateLimitHarness):
    """验收要求：**成功登录不写该表**。"""

    def test_protocol_has_no_record_success(self):
        """结构性保证：协议里根本没有"记成功"的方法。"""
        public = [n for n in dir(self.store) if not n.startswith("_")]
        self.assertNotIn("record_success", public)
        self.assertNotIn("record_login", public)
        for name in ("count_failures", "record_failure", "clear", "purge_older_than"):
            self.assertIn(name, public)

    def test_successful_login_clears_and_writes_nothing(self):
        """成功后调用 `clear()`：删掉失败记录，**不新增行**。"""
        ip = "203.0.113.7"
        for _ in range(3):
            self.record(ip)
        before = self.store.count_all()
        self.store.clear(ip)
        after = self.store.count_all()
        self.assertEqual(after, before - 3, "clear 应当删掉该 IP 的记录")
        self.assertEqual(after, 0)
        self.assertEqual(self.store.count_all(ip), 0)

    def test_repeated_successful_logins_do_not_grow_the_table(self):
        """连续成功登录不应让表增长 —— 否则高频路径变成写热点（ADR-014）。"""
        ip = "203.0.113.7"
        for _ in range(10):
            self.store.clear(ip)      # 每次成功都 clear
        self.assertEqual(self.store.count_all(), 0, "成功路径写入了数据")

    def test_clear_only_affects_that_ip(self):
        self.record("1.1.1.1")
        self.record("2.2.2.2")
        self.store.clear("1.1.1.1")
        self.assertEqual(self.store.count_all("1.1.1.1"), 0)
        self.assertEqual(self.store.count_all("2.2.2.2"), 1, "误删了别的 IP")


class MultiWorkerTests(RateLimitHarness):
    """本任务存在的理由：内存字典在多 worker 下会被放大。"""

    def test_counts_are_visible_across_store_instances(self):
        """另一个 store 实例（等价于另一个 worker 进程）必须看到同样的计数。

        原实现用模块级字典：worker A 记 3 次、worker B 记 3 次，
        两边都不到 5 → 攻击者实际可以试 6 次以上。落表后才是真的。
        """
        for _ in range(3):
            self.record("203.0.113.7")

        other_session = sessionmaker(bind=self.engine, autocommit=False,
                                     autoflush=False)
        other_worker = SQLiteRateLimitStore(session_factory=other_session)
        self.assertEqual(
            other_worker.count_failures("203.0.113.7", self.window_start()), 3,
            "另一个 worker 看不到已记录的失败 —— 限流会被 worker 数放大",
        )
        other_worker.record_failure("203.0.113.7", self.now)
        self.assertEqual(self.count("203.0.113.7"), 4, "本 worker 看不到另一个写的")

    def test_counts_visible_from_raw_connection(self):
        self.record("203.0.113.7")
        rows = self.raw("SELECT ip, attempted_at FROM auth_attempts")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "203.0.113.7")


class PurgeTests(RateLimitHarness):

    def test_purge_removes_only_older(self):
        self.record("1.1.1.1", at=iso_after(-WINDOW_MINUTES * 60 - 60, self.now))
        self.record("2.2.2.2", at=self.now)
        removed = self.store.purge_older_than(self.window_start())
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.count_all("1.1.1.1"), 0)
        self.assertEqual(self.store.count_all("2.2.2.2"), 1, "误删了窗口内的记录")

    def test_purge_is_complementary_to_the_window(self):
        """`purge_older_than(since)` 恰好删掉 `count_failures(since)` 不数的那些。"""
        since = self.window_start()
        self.record("1.1.1.1", at=iso_after(-1, since))   # 刚好在窗口外
        self.record("1.1.1.1", at=since)                  # 边界，窗口内
        self.record("1.1.1.1", at=self.now)

        self.assertEqual(self.count("1.1.1.1"), 2, "窗口内应当是 2 条")
        self.assertEqual(self.store.purge_older_than(since), 1, "窗口外应当是 1 条")
        self.assertEqual(self.count("1.1.1.1"), 2, "清理后被计数的条数不该变")

    def test_purge_on_empty_returns_zero(self):
        self.assertEqual(self.store.purge_older_than(self.now), 0)


class TransactionDisciplineTests(RateLimitHarness):

    def _can_take_write_lock(self):
        conn = sqlite3.connect(self.db, timeout=0.5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    def test_every_method_releases_its_transaction(self):
        for name, call in (
            ("count_failures", lambda: self.count("1.1.1.1")),
            ("record_failure", lambda: self.record("1.1.1.1")),
            ("clear", lambda: self.store.clear("1.1.1.1")),
            ("purge_older_than", lambda: self.store.purge_older_than(self.now)),
        ):
            call()
            self.assertTrue(self._can_take_write_lock(),
                            "%s 之后事务没释放 —— 会把后续请求堵死" % name)

    def test_no_transaction_handle_exposed(self):
        public = [n for n in dir(self.store) if not n.startswith("_")]
        for forbidden in ("begin", "commit", "rollback", "session", "transaction"):
            self.assertNotIn(forbidden, public)


if __name__ == "__main__":
    unittest.main()
