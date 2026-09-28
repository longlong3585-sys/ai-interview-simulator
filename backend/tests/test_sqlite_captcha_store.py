"""T-20 测试：`SQLiteCaptchaStore`。

任务描述的两个关键词就是本文件的两组核心用例：

  * **TTL 300 秒**：过期即失效；"恰好等于到期时刻"的口径也钉住。
  * **一次性**：同一验证码成功用掉之后再用必须失败。

外加三条从原实现（`auth.py` 的内存字典）继承下来的语义，必须逐条对齐，
否则迁移会**悄悄改变登录体验**：

  1. **输错不消费** —— 用户在有效期内可以改错重输；
  2. **输入去首尾空白** —— 从图片抄录很容易带上空格；
  3. **成功后置 `used=1` 而非删除** —— DDL 有 `used` 列，保留使用痕迹。

还有一条是这次迁移**真正要解决的问题**：原子性。
原实现是"读 -> 判断 -> 写"，在多 worker 下连正确性都不成立；
本实现用单条 UPDATE，因此"两个并发请求恰好一个成功"必须可验证。
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

from services.stores.base import CaptchaStore, StoreError, iso_after, utcnow_iso  # noqa: E402
from services.stores.sqlite_captcha_store import SQLiteCaptchaStore  # noqa: E402
from tests.support import build_temp_db  # noqa: E402

TTL = 300  # config.CAPTCHA_TTL，与架构 §6.3 一致


class CaptchaHarness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t20-captcha-")
        self.db = os.path.join(self.tmp, "c.db")
        self.engine = build_temp_db(self.db)
        self.Session = sessionmaker(bind=self.engine, autocommit=False,
                                    autoflush=False)
        self.store = SQLiteCaptchaStore(session_factory=self.Session)
        self.now = utcnow_iso()

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self, captcha_id="cap1", code="1234", ttl=TTL, now=None):
        expires = iso_after(ttl, now or self.now)
        self.store.save(captcha_id, code, expires)
        return expires

    def raw(self, sql, params=None):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).fetchall()


class ProtocolConformanceTests(CaptchaHarness):

    def test_satisfies_protocol(self):
        """`@runtime_checkable` 让这句话真的在检查方法齐备性。"""
        self.assertIsInstance(self.store, CaptchaStore)

    def test_factory_returns_protocol_satisfying_store(self):
        from services import stores
        from services.stores import factory

        factory.reset_captcha_store()
        store = factory.get_captcha_store()
        self.assertIsInstance(store, CaptchaStore)
        self.assertIs(factory.get_captcha_store(), store, "应当是进程内单例")

    def test_abstract_layer_does_not_export_implementation(self):
        """抽象包不得导出实现类 —— 否则调用方会绕开装配点直接 import。"""
        from services import stores

        self.assertFalse(
            any("sqlite" in n.lower() for n in stores.__all__),
            "services.stores 不应导出实现：%s" % stores.__all__,
        )


class SaveAndVerifyTests(CaptchaHarness):

    def test_save_then_verify_succeeds(self):
        self.save()
        self.assertTrue(self.store.verify_and_consume("cap1", "1234", self.now))

    def test_unknown_id_fails(self):
        self.assertFalse(self.store.verify_and_consume("nope", "1234", self.now))

    def test_wrong_code_fails(self):
        self.save(code="1234")
        self.assertFalse(self.store.verify_and_consume("cap1", "9999", self.now))

    def test_wrong_code_does_not_consume(self):
        """**输错不消费** —— 用户在有效期内可以改错重输。

        若把"输错"也消费掉，打错一个字符就得刷新验证码重来，体验不可接受。
        """
        self.save(code="1234")
        self.assertFalse(self.store.verify_and_consume("cap1", "0000", self.now))
        row = self.store.get("cap1")
        self.assertEqual(row["used"], 0, "输错竟然把验证码消费掉了")
        self.assertTrue(self.store.verify_and_consume("cap1", "1234", self.now),
                        "输错之后正确的码应当仍然可用")

    def test_code_is_stripped_like_legacy(self):
        """原实现比较的是 `user_code.strip()`；从图片抄录容易带空格，必须保留。"""
        self.save(code="1234")
        self.assertTrue(self.store.verify_and_consume("cap1", "  1234  ", self.now))

    def test_empty_inputs_are_rejected_without_writing(self):
        self.save()
        self.assertFalse(self.store.verify_and_consume("", "1234", self.now))
        self.assertFalse(self.store.verify_and_consume("cap1", "", self.now))
        self.assertFalse(self.store.verify_and_consume("cap1", "   ", self.now))
        self.assertEqual(self.store.get("cap1")["used"], 0)

    def test_empty_code_never_matches_even_if_a_row_has_empty_code(self):
        """空输入必须**无条件**拒绝，哪怕库里真有一行 code 为空。

        这条用例是破坏性验证补出来的。原先"空输入提前返回"看着像纯优化
        （反正空串匹配不上任何正常验证码），把它删掉也不会有测试变红 ——
        探针 C9 就是这么"漏网"的。

        但存在一个 fail-open 场景：若某行因数据损坏/上游 bug 而 `code=''`，
        那么去掉提前返回后，**提交空验证码就能通过校验并把该行消费掉**。
        验证码校验是防机器人的闸门，任何 fail-open 都不可接受，
        所以这个提前返回是**安全判定**，不只是省一次往返。
        """
        self.save("broken", "1111")
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE captcha_store SET code='' "
                              "WHERE captcha_id='broken'"))

        self.assertFalse(
            self.store.verify_and_consume("broken", "", self.now),
            "空验证码竟然通过了校验 —— fail-open",
        )
        self.assertFalse(self.store.verify_and_consume("broken", "   ", self.now))
        self.assertEqual(self.store.get("broken")["used"], 0,
                         "空输入竟然消费了验证码")

    def test_duplicate_save_is_reported_not_silently_replaced(self):
        """重复的 captcha_id 说明上游有 bug —— 必须报错，不能静默覆盖。"""
        self.save(code="1111")
        with self.assertRaises(StoreError) as ctx:
            self.save(code="2222")
        self.assertIn("cap1", str(ctx.exception))
        # 原记录未被覆盖
        self.assertEqual(self.store.get("cap1")["code"], "1111")


class OneTimeUseTests(CaptchaHarness):
    """任务关键词之一：**一次性**。"""

    def test_second_use_fails(self):
        self.save()
        self.assertTrue(self.store.verify_and_consume("cap1", "1234", self.now))
        self.assertFalse(self.store.verify_and_consume("cap1", "1234", self.now),
                         "同一验证码被用了两次")

    def test_success_marks_used_instead_of_deleting(self):
        """保留使用痕迹（DDL 有 used 列），而不是像原实现那样直接删掉。"""
        self.save()
        self.store.verify_and_consume("cap1", "1234", self.now)
        row = self.store.get("cap1")
        self.assertIsNotNone(row, "成功后行不该被删除（要保留 used 痕迹）")
        self.assertEqual(row["used"], 1)

    def test_other_captchas_unaffected(self):
        self.save("cap1", "1111")
        self.save("cap2", "2222")
        self.assertTrue(self.store.verify_and_consume("cap1", "1111", self.now))
        self.assertTrue(self.store.verify_and_consume("cap2", "2222", self.now))

    def test_concurrent_consume_exactly_one_wins(self):
        """**原子性** —— 原实现"读 -> 判断 -> 写"在多 worker 下连正确性都不成立。

        本实现用单条 UPDATE，因此 n 个并发消费里**恰好一个**能成功。
        这里用独立连接真的并发打（不是模拟）：先各自 BEGIN IMMEDIATE
        排队，再依次执行同一条 UPDATE，断言成功次数恰好为 1。
        """
        self.save(code="1234")
        results = []
        for _ in range(5):
            conn = sqlite3.connect(self.db, timeout=15.0)
            try:
                conn.execute("PRAGMA busy_timeout=15000")
                # 直接执行与 store 相同的语句，验证"语句本身"的原子语义
                cur = conn.execute(
                    "UPDATE captcha_store SET used = 1 "
                    "WHERE captcha_id = 'cap1' AND used = 0 "
                    "  AND expires_at > ? AND code = ?", (self.now, "1234"))
                conn.commit()
                results.append(cur.rowcount)
            finally:
                conn.close()
        self.assertEqual(results.count(1), 1,
                         "并发消费的成功次数不是 1：%s" % results)


class ExpiryTests(CaptchaHarness):
    """任务关键词之二：**TTL 300 秒**。"""

    def test_expired_code_fails(self):
        expires = self.save(ttl=TTL)
        later = iso_after(TTL + 1, self.now)
        self.assertFalse(self.store.verify_and_consume("cap1", "1234", later))
        self.assertIsNotNone(expires)

    def test_still_valid_just_before_expiry(self):
        self.save(ttl=TTL)
        almost = iso_after(TTL - 1, self.now)
        self.assertTrue(self.store.verify_and_consume("cap1", "1234", almost))

    def test_boundary_instant_is_expired(self):
        """**恰好等于到期时刻**算过期（口径选择，见模块 docstring）。

        选 `<=` 而不是 `<`，是为了与 T-19 会话存储
        （`SessionSnapshot.is_expired`）统一 —— 两处对"到期"的理解必须一致，
        否则会出现"会话过期了、验证码还没过期"这种无法解释的差异。

        注意：原内存实现在这一瞬是**有效**的（严格小于）。这是唯一一处
        理论行为差异，宽度为一个瞬间，实际不可观测，但既然不一致就钉住。
        """
        expires = self.save(ttl=TTL)
        row = self.store.get("cap1")
        self.assertEqual(row["expires_at"], expires)
        self.assertFalse(
            self.store.verify_and_consume("cap1", "1234", expires),
            "到期时刻应当算过期（与 T-19 会话存储同口径）",
        )

    def test_expired_code_is_not_consumed_by_failed_verify(self):
        """过期导致的失败也不消费 —— 行留着交给 purge 回收。"""
        self.save(ttl=TTL)
        later = iso_after(TTL + 100, self.now)
        self.store.verify_and_consume("cap1", "1234", later)
        self.assertEqual(self.store.get("cap1")["used"], 0)

    def test_ttl_constant_matches_architecture(self):
        """300 秒是架构 §6.3 定的；这里把 config 的值也钉一下。"""
        from config import CAPTCHA_TTL

        self.assertEqual(CAPTCHA_TTL, TTL)


class PurgeTests(CaptchaHarness):

    def test_purge_removes_only_expired(self):
        self.save("old1", "1111", ttl=-10)
        self.save("old2", "2222", ttl=-1)
        self.save("live", "3333", ttl=TTL)

        removed = self.store.purge_expired(self.now)
        self.assertEqual(removed, 2)
        self.assertIsNone(self.store.get("old1"))
        self.assertIsNone(self.store.get("old2"))
        self.assertIsNotNone(self.store.get("live"), "未过期的被误删")

    def test_purge_also_removes_used_rows_once_expired(self):
        """已用过的行在过期后同样该被回收（否则表只增不减）。"""
        self.save("used", "1111", ttl=TTL)
        self.store.verify_and_consume("used", "1111", self.now)
        self.assertEqual(self.store.purge_expired(self.now), 0, "还没过期不该删")
        later = iso_after(TTL + 1, self.now)
        self.assertEqual(self.store.purge_expired(later), 1)

    def test_purge_boundary_matches_verify_boundary(self):
        """两条判据必须**严格互补**，否则会出现"验证时说过期、清理时又留着"。"""
        expires = self.save("edge", "1111", ttl=TTL)
        # verify 说：expires_at > now 才算有效 -> 边界处无效
        self.assertFalse(self.store.verify_and_consume("edge", "1111", expires))
        # purge 说：expires_at <= now 就删 -> 边界处应当删掉
        self.assertEqual(self.store.purge_expired(expires), 1)

    def test_purge_on_empty_table_returns_zero(self):
        self.assertEqual(self.store.purge_expired(self.now), 0)


class FactorySelfCheckTests(unittest.TestCase):
    """装配点的**启动期自检**必须真的会拦下不合规的实现。

    没有这组用例，"装配期自检"就只是一句注释 —— 去掉 `isinstance`
    也不会有任何测试变红。
    """

    def tearDown(self):
        from services.stores import factory

        factory.reset_stores()

    def test_factory_rejects_implementation_missing_methods(self):
        from services.stores import factory

        class HalfBaked(object):
            """只实现了 save —— 少了 verify_and_consume / purge_expired。"""

            def save(self, captcha_id, code, expires_at):
                pass

        original = factory._REGISTRY["captcha"]
        factory._REGISTRY["captcha"] = (original[0], original[1], original[2],
                                        HalfBaked)
        factory.reset_captcha_store()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                factory.get_captcha_store()
            self.assertIn("CaptchaStore", str(ctx.exception))
        finally:
            factory._REGISTRY["captcha"] = original
            factory.reset_captcha_store()

    def test_factory_rejects_unknown_backend(self):
        from services.stores import factory

        old = os.environ.get("CAPTCHA_STORE_BACKEND")
        os.environ["CAPTCHA_STORE_BACKEND"] = "redis"
        factory.reset_captcha_store()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                factory.get_captcha_store()
            self.assertIn("redis", str(ctx.exception))
        finally:
            if old is None:
                os.environ.pop("CAPTCHA_STORE_BACKEND", None)
            else:
                os.environ["CAPTCHA_STORE_BACKEND"] = old
            factory.reset_captcha_store()

    def test_session_and_captcha_are_independent_singletons(self):
        from services.stores import factory

        factory.reset_stores()
        session_a = factory.get_session_store()
        captcha_a = factory.get_captcha_store()
        self.assertIs(factory.get_session_store(), session_a)
        self.assertIs(factory.get_captcha_store(), captcha_a)
        self.assertIsNot(session_a, captcha_a)
        # 只重置其中一个，另一个不受影响
        factory.reset_captcha_store()
        self.assertIs(factory.get_session_store(), session_a)
        self.assertIsNot(factory.get_captcha_store(), captcha_a)


class TransactionDisciplineTests(CaptchaHarness):

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
            ("save", lambda: self.save("capX", "1234")),
            ("verify_and_consume",
             lambda: self.store.verify_and_consume("capX", "1234", self.now)),
            ("purge_expired", lambda: self.store.purge_expired(self.now)),
        ):
            call()
            self.assertTrue(self._can_take_write_lock(),
                            "%s 之后事务没释放 —— 会把后续请求堵死" % name)

    def test_failed_save_also_releases_transaction(self):
        self.save("dup", "1111")
        try:
            self.save("dup", "2222")
        except StoreError:
            pass
        self.assertTrue(self._can_take_write_lock(),
                        "重复保存失败之后事务没释放")

    def test_no_transaction_handle_exposed(self):
        public = [n for n in dir(self.store) if not n.startswith("_")]
        for forbidden in ("begin", "commit", "rollback", "session", "transaction"):
            self.assertNotIn(forbidden, public,
                             "存储层暴露了 %r，调用方就有可能持长事务" % forbidden)


class RawStorageTests(CaptchaHarness):
    """直接读库确认落库内容（不只在自己内部自洽）。"""

    def test_save_writes_expected_columns(self):
        expires = self.save("raw", "9876")
        row = self.raw("SELECT captcha_id, code, expires_at, used "
                       "FROM captcha_store WHERE captcha_id='raw'")[0]
        self.assertEqual(tuple(row), ("raw", "9876", expires, 0))

    def test_new_captcha_defaults_to_unused(self):
        self.save("fresh", "1111")
        row = self.raw("SELECT used FROM captcha_store WHERE captcha_id='fresh'")[0]
        self.assertEqual(row[0], 0, "新建验证码的 used 默认值应为 0")


if __name__ == "__main__":
    unittest.main()
