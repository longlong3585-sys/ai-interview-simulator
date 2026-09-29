"""T-51 / ADR-003 选 B 测试：`jti` + `token_blacklist` **真登出**。

修复前"退出登录"只是前端删掉 localStorage 里的令牌 —— 令牌本身仍然有效，
被抄走就还能用。T-50 把续期链拉到 **8 小时**之后，这个洞被显著放大。
本文件把"登出之后旧令牌必须立刻失效"钉死，并守住那条最容易复发的约束：

    **黑名单 TTL 必须 ≥ 8 小时（续期绝对上限），否则被吊销令牌会复活**
    （`docs/02-arch-review.md` R-4：初稿写 30 分钟，吊销形同虚设）

三层，缺一层都会留下"看起来实现了"的假象：

  1. **纯逻辑**（`utils/token_revocation.py`，固定时钟）：留多久、算不算够长、缺 jti 怎么办；
  2. **存储**（`SQLiteTokenBlacklistStore`）：幂等写入、过期过滤、只删已过期；
  3. **接线**（`POST /api/logout` + `get_current_user` + 续期中间件）：
     真发请求断言"登出后旧令牌到处都 401""缺 jti 的老令牌登出仍成功"。
"""

import os
import sys
import time
import unittest

from fastapi.testclient import TestClient

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from tests.support import (  # noqa: E402
    bearer,
    create_test_user,
    delete_users,
    mint_renewable_token,
)
from utils.token_renewal import ABSOLUTE_MAX_LIFETIME_SECONDS  # noqa: E402
from utils.token_revocation import (  # noqa: E402
    BLACKLIST_MIN_TTL_SECONDS,
    blacklist_covers_token,
    blacklist_expiry,
    is_revocable,
    iso_after_seconds,
    iso_now,
    revocation_target,
)

USER = "t51_alice"
ADMIN = "t51_admin"
HOUR = 3600


def clear_blacklist():
    """清空黑名单（测试要的是"干净起点"）。

    只删表内容、不走 HTTP —— 与 `test_chat_auth.clear_sessions` 同一手法。
    """
    from sqlalchemy import text
    import database
    with database.engine.begin() as conn:
        conn.execute(text("DELETE FROM token_blacklist"))


class RevocationLogicTests(unittest.TestCase):
    """第 1 层：纯逻辑（固定时钟，不碰网络/数据库）。"""

    NOW = 1_700_000_000

    def test_ttl_matches_the_absolute_lifetime(self):
        """黑名单下限必须**等于** T-50 的续期绝对上限（8h）。

        这条断言是 R-4 的护栏：两者一旦分叉，"被吊销令牌 30 分钟后复活"
        就会以另一种形式复发（比如有人把绝对上限调到 12h、这里忘了跟）。
        """
        self.assertEqual(BLACKLIST_MIN_TTL_SECONDS, 8 * 3600)
        self.assertEqual(BLACKLIST_MIN_TTL_SECONDS, ABSOLUTE_MAX_LIFETIME_SECONDS)

    def test_far_expiry_is_capped_at_the_absolute_limit(self):
        """令牌还能活 7 小时 → 记录**恰好**留到 `now + 8h`（下限兜住）。"""
        payload = {"jti": "j1", "exp": self.NOW + 7 * HOUR}
        self.assertEqual(blacklist_expiry(payload, self.NOW),
                         iso_after_seconds(BLACKLIST_MIN_TTL_SECONDS, self.NOW))

    def test_short_expiry_is_lifted_to_the_floor(self):
        """令牌只剩 5 分钟 → 仍然按 8 小时留痕（这就是 R-4 的修正点）。

        初稿做法（按令牌自己的 exp 留 5 分钟）会让"登出 → 等 5 分钟 → 复用"
        成立；但续期链最长 8 小时，谁也无法保证那枚令牌不会在别处被续期。
        """
        payload = {"jti": "j2", "exp": self.NOW + 5 * 60}
        self.assertEqual(blacklist_expiry(payload, self.NOW),
                         iso_after_seconds(BLACKLIST_MIN_TTL_SECONDS, self.NOW))

    def test_longer_than_floor_expiry_is_not_extended(self):
        """令牌声称还能活 20 小时（异常/伪造）→ 记录不跟着延长。

        T-50 保证真实令牌活不过 8 小时；跟到 20 小时只会留垃圾行。
        """
        payload = {"jti": "j7", "exp": self.NOW + 20 * HOUR}
        self.assertEqual(blacklist_expiry(payload, self.NOW),
                         iso_after_seconds(BLACKLIST_MIN_TTL_SECONDS, self.NOW))

    def test_already_expired_token_still_gets_floor(self):
        """拿着过期令牌来登出 → 也要留痕（否则登出在时钟偏差下变成空操作）。"""
        payload = {"jti": "j3", "exp": self.NOW - HOUR}
        self.assertEqual(blacklist_expiry(payload, self.NOW),
                         iso_after_seconds(BLACKLIST_MIN_TTL_SECONDS, self.NOW))

    def test_missing_exp_is_treated_as_floor(self):
        payload = {"jti": "j4"}
        self.assertEqual(blacklist_expiry(payload, self.NOW),
                         iso_after_seconds(BLACKLIST_MIN_TTL_SECONDS, self.NOW))

    def test_expiry_is_a_naive_utc_iso_string(self):
        """必须是**朴素 UTC ISO 串**（与 `stores/base.utcnow_iso()` 同格式）。

        格式一旦漂移，`expires_at > now` 的**字典序**比较会静默失效 ——
        比较不报错，只是结果悄悄变错（最坏的情况：吊销失效且无人发现）。
        """
        from datetime import datetime
        from services.stores.base import iso_after, utcnow_iso

        payload = {"jti": "j5", "exp": self.NOW + 8 * HOUR}
        value = blacklist_expiry(payload, self.NOW)
        self.assertNotIn("+", value, "带时区偏移的串无法与 utcnow_iso() 比字典序")
        self.assertNotIn("Z", value)
        self.assertNotIn("tzinfo", repr(value))
        # 能被 fromisoformat 解析（与 stores 层同一种解析路径）
        self.assertIsInstance(datetime.fromisoformat(value), datetime)
        self.assertLess(len(value), len("2026-09-29T19:59:20.672310") + 1,
                        "本模块不产出微秒——与 utcnow_iso() 的差异只在精度，不影响字典序")
        # 与存储层的时间工具**逐字一致**（同一种基准、同一种格式）
        # 注意必须传 self.NOW —— 不传就会混入真实 now，断言变成"看运气"
        base = iso_now(self.NOW)
        self.assertEqual(value, iso_after(BLACKLIST_MIN_TTL_SECONDS, base))
        # 令牌还能活很久（20h）→ 同样压到 8h（不产生超上限的垃圾行）
        self.assertEqual(blacklist_expiry({"jti": "j5", "exp": self.NOW + 20 * HOUR}, self.NOW),
                         iso_after(BLACKLIST_MIN_TTL_SECONDS, base))
        # 令牌快到期（2h）→ 下限把它抬到 8h（R-4 的修正点）
        self.assertEqual(blacklist_expiry({"jti": "j5", "exp": self.NOW + 2 * HOUR}, self.NOW),
                         iso_after(BLACKLIST_MIN_TTL_SECONDS, base))
        # 值本身可比大小（字典序 = 时间序）——全部用**固定时钟**，不要混入真实 now
        self.assertGreater(value, base)
        self.assertLess(value, iso_after(9 * HOUR, base))
        # 与真实 now 的格式也一致（同样是 `YYYY-MM-DDTHH:MM:SS.ffffff` 朴素 UTC 串）
        self.assertEqual(len(utcnow_iso()), len("2026-09-29T19:59:20.672310"))
        self.assertNotIn("+", utcnow_iso())
        self.assertEqual(len(iso_now(self.NOW)), len("2026-09-29T19:59:20"))

    def test_covers_token_helper(self):
        payload = {"jti": "j6", "exp": self.NOW + 10 * 60}
        stored = blacklist_expiry(payload, self.NOW)
        self.assertTrue(blacklist_covers_token(stored, payload, self.NOW))
        # 反面：只留 1 分钟的记录**不**覆盖（模拟 R-4 的错误做法）
        too_short = iso_after_seconds(60, self.NOW)
        self.assertFalse(blacklist_covers_token(too_short, payload, self.NOW))
        # 反面：超过 8 小时的记录是垃圾（不产生）
        too_long = iso_after_seconds(BLACKLIST_MIN_TTL_SECONDS + 1, self.NOW)
        self.assertFalse(blacklist_covers_token(too_long, payload, self.NOW))

    def test_revocation_target_requires_nonempty_jti(self):
        self.assertEqual(revocation_target({"jti": "abc"}), "abc")
        self.assertEqual(revocation_target({"jti": "  abc  "}), "abc")
        self.assertIsNone(revocation_target({}))
        self.assertIsNone(revocation_target({"jti": ""}))
        self.assertIsNone(revocation_target({"jti": "   "}))
        self.assertIsNone(revocation_target({"jti": 7}), "非字符串 jti 必须视为不可吊销")
        self.assertIsNone(revocation_target(None))
        self.assertIsNone(revocation_target("x"))
        self.assertFalse(is_revocable({}))
        self.assertTrue(is_revocable({"jti": "abc"}))

    def test_time_helpers_agree_with_store_layer(self):
        """本模块的 ISO 工具与 `stores/base` 的口径必须一致（否则日期比大小会错）。"""
        from services.stores.base import iso_after
        base = iso_now(self.NOW)
        self.assertEqual(iso_now(self.NOW), base)
        # 同一个基准、同样的秒数 → 逐字相同（含小数位都没有）
        self.assertEqual(iso_now(self.NOW), iso_after(0, base))
        self.assertEqual(iso_after_seconds(3600, self.NOW), iso_after(3600, base))
        # 负值用于构造"已过期"的测试数据
        self.assertLess(iso_after_seconds(-60, self.NOW), base)


class BlacklistStoreTests(unittest.TestCase):
    """第 2 层：SQLite 实现（真写库）。"""

    @classmethod
    def setUpClass(cls):
        from services.stores.sqlite_token_blacklist_store import SQLiteTokenBlacklistStore
        cls.store = SQLiteTokenBlacklistStore()

    def setUp(self):
        clear_blacklist()

    def tearDown(self):
        clear_blacklist()

    def test_revoke_then_is_revoked(self):
        self.assertFalse(self.store.is_revoked("jti-a", iso_now()))
        self.store.revoke("jti-a", iso_after_seconds(8 * HOUR))
        self.assertTrue(self.store.is_revoked("jti-a", iso_now()))

    def test_revoke_is_idempotent(self):
        """同一个 jti 登出两次不得抛错（两个标签页、或前端网络重试）。"""
        self.store.revoke("jti-b", iso_after_seconds(8 * HOUR))
        self.store.revoke("jti-b", iso_after_seconds(8 * HOUR))  # 不应抛
        self.assertEqual(self.store.count_all(), 1)

    def test_revoke_requires_jti(self):
        with self.assertRaises(ValueError):
            self.store.revoke("", iso_after_seconds(HOUR))

    def test_expired_record_is_not_treated_as_revoked(self):
        """`expires_at <= now` 视为已失效 —— 与"已被清理"必须行为一致。

        否则清理任务的节奏会变成一个**隐藏开关**：跑得晚，用户就被自己的旧令牌
        永久挡在门外（而且报 401 看不出原因）。
        """
        self.store.revoke("jti-c", iso_after_seconds(-60))  # 已过期
        self.assertFalse(self.store.is_revoked("jti-c", iso_now()))

    def test_boundary_at_exactly_now_is_not_revoked(self):
        self.store.revoke("jti-d", iso_now())
        self.assertFalse(self.store.is_revoked("jti-d", iso_now()))

    def test_purge_expired_only_removes_expired(self):
        self.store.revoke("jti-expired", iso_after_seconds(-1))
        self.store.revoke("jti-live", iso_after_seconds(8 * HOUR))
        removed = self.store.purge_expired(iso_now())
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.count_all(), 1)
        self.assertTrue(self.store.is_revoked("jti-live", iso_now()))

    def test_is_revoked_without_jti_is_false(self):
        self.assertFalse(self.store.is_revoked("", iso_now()))
        self.assertFalse(self.store.is_revoked(None, iso_now()))

    def test_get_expires_at_round_trip(self):
        stamp = iso_after_seconds(8 * HOUR)
        self.store.revoke("jti-e", stamp)
        self.assertEqual(self.store.get_expires_at("jti-e"), stamp)
        self.assertIsNone(self.store.get_expires_at("jti-missing"))

    def test_protocol_conformance(self):
        """装配期自检的那条 `isinstance` —— 这里提前把它跑一遍。"""
        from services.stores.base import TokenBlacklistStore
        self.assertIsInstance(self.store, TokenBlacklistStore)

    def test_factory_assembles_blacklist_store(self):
        """工厂必须能装配黑名单存储（漏了 registry 一行 = 运行时 AttributeError）。"""
        from services.stores.factory import (
            get_token_blacklist_store,
            reset_token_blacklist_store,
        )
        reset_token_blacklist_store()
        try:
            store = get_token_blacklist_store()
            self.assertIsInstance(store, self.store.__class__)
        finally:
            reset_token_blacklist_store()


class LogoutEndpointTests(unittest.TestCase):
    """第 3 层：接线（`POST /api/logout` + `get_current_user` + 续期中间件）。"""

    PROBE_PATHS = ("/api/interview/config", "/api/user/profile", "/api/notifications")

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        clear_blacklist()
        create_test_user(USER, role="user")
        create_test_user(ADMIN, role="admin")

    def tearDown(self):
        clear_blacklist()
        delete_users([USER, ADMIN])

    # ---------- 核心验收标准：登出后旧 token → 401 ----------

    def test_old_token_is_rejected_everywhere_after_logout(self):
        token = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=1)
        # 登出前：到处都能用
        for path in self.PROBE_PATHS:
            self.assertEqual(self.client.get(path, headers=bearer(token)).status_code, 200,
                             "登出前 %s 就不可用，本用例的前提不成立" % path)
        # 登出
        response = self.client.post("/api/logout", headers=bearer(token))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("revoked"), "登出没有真正吊销令牌")
        # 登出后：**每一处**都必须 401（不只某个端点）
        for path in self.PROBE_PATHS:
            self.assertEqual(self.client.get(path, headers=bearer(token)).status_code, 401,
                             "%s 仍接受已登出的令牌（真登出没生效）" % path)

    def test_revoked_token_is_marked_with_a_header(self):
        token = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=1)
        self.client.post("/api/logout", headers=bearer(token))
        response = self.client.get("/api/interview/config", headers=bearer(token))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("X-Token-Revoked"), "revoked")

    def test_revoked_token_is_not_renewed(self):
        """已吊销的令牌**不得**被续期。

        否则"登出"只挡住下一个请求，却顺手发了一枚新的、仍然有效的令牌回去 ——
        吊销被自己的续期逻辑绕过。
        """
        # 剩余 <50%，正常情况下一定会被续期
        token = mint_renewable_token(USER, minutes=30, age_minutes=21, user_id=1)
        self.client.post("/api/logout", headers=bearer(token))
        response = self.client.get("/api/interview/config", headers=bearer(token))
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(response.headers.get("X-Refreshed-Token"))

    def test_revocation_survives_renewal_same_jti(self):
        """续期沿用同一 jti ⇒ 登出一次就把**整条续期链**吊销（ADR-016 第 4 点）。"""
        old = mint_renewable_token(USER, minutes=30, age_minutes=21, user_id=1)
        renewed = self.client.get("/api/interview/config", headers=bearer(old)).headers.get(
            "X-Refreshed-Token")
        self.assertTrue(renewed, "前提不成立：该令牌本应被续期")
        # 用**旧**令牌登出，然后用**续期后**的令牌访问
        self.client.post("/api/logout", headers=bearer(old))
        self.assertEqual(self.client.get("/api/interview/config", headers=bearer(renewed)).status_code, 401,
                         "续期后的令牌逃过了吊销 —— jti 没有沿用")

    def test_other_sessions_are_not_affected(self):
        """吊销是**按 jti**的：另一个会话（另一次登录）不受影响。"""
        doomed = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=1, jti="session-a")
        alive = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=1, jti="session-b")
        self.client.post("/api/logout", headers=bearer(doomed))
        self.assertEqual(self.client.get("/api/interview/config", headers=bearer(doomed)).status_code, 401)
        self.assertEqual(self.client.get("/api/interview/config", headers=bearer(alive)).status_code, 200)

    def test_logout_is_idempotent_over_http(self):
        token = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=1)
        self.assertEqual(self.client.post("/api/logout", headers=bearer(token)).status_code, 200)
        self.assertEqual(self.client.post("/api/logout", headers=bearer(token)).status_code, 200)

    # ---------- 登出 Must succeed（不要求令牌仍然有效） ----------

    def test_logout_works_with_expired_token(self):
        """拿着**已过期**的令牌登出也必须成功（页面挂了很久再点退出）。

        要求令牌有效会让登出 401，而前端只会把它当网络错误 ——
        用户以为登出了，服务端那枚令牌却还在别处有效。
        """
        expired = mint_renewable_token(USER, minutes=30, age_minutes=40, user_id=1)
        response = self.client.post("/api/logout", headers=bearer(expired))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("revoked"))

    def test_logout_without_token_still_succeeds(self):
        response = self.client.post("/api/logout")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("reason"), "no_token")

    def test_logout_with_garbage_token_still_succeeds(self):
        response = self.client.post("/api/logout", headers=bearer("not-a-jwt"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("reason"), "invalid_token")

    def test_logout_with_legacy_token_reports_no_jti(self):
        """T-50 之前签发的老令牌（无 jti）无法被吊销 —— 但要**明确说出来**，
        而不是假装成功（前端据此提示"请重新登录以完成登出"）。"""
        legacy = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=1)
        from jose import jwt
        from config import ALGORITHM, SECRET_KEY
        payload = jwt.decode(legacy, SECRET_KEY, algorithms=[ALGORITHM])
        payload.pop("jti", None)
        payload.pop("auth_time", None)
        legacy_without_jti = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

        response = self.client.post("/api/logout", headers=bearer(legacy_without_jti))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json().get("revoked"))
        self.assertEqual(response.json().get("reason"), "no_jti")

    def test_legacy_token_still_authenticates(self):
        """老令牌鉴权照旧（不能被 T-51 误伤）—— 只是无法被吊销。"""
        from jose import jwt
        from config import ALGORITHM, SECRET_KEY
        legacy = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=1)
        payload = jwt.decode(legacy, SECRET_KEY, algorithms=[ALGORITHM])
        payload.pop("jti", None)
        payload.pop("auth_time", None)
        without_jti = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
        self.assertEqual(self.client.get("/api/interview/config", headers=bearer(without_jti)).status_code, 200)

    # ---------- 管理员与公开端点 ----------

    def test_admin_logout_also_revokes(self):
        token = mint_renewable_token(ADMIN, role="admin", minutes=30, age_minutes=1, user_id=2)
        self.assertEqual(self.client.get("/api/admin/stats", headers=bearer(token)).status_code, 200)
        self.client.post("/api/logout", headers=bearer(token))
        self.assertEqual(self.client.get("/api/admin/stats", headers=bearer(token)).status_code, 401)

    def test_public_endpoints_unaffected_by_blacklist(self):
        """公开端点（登录/注册/验证码/题库）不查黑名单 —— 它们本来就不带令牌。"""
        for path in ("/api/question_bank", "/api/captcha"):
            response = self.client.get(path)
            self.assertLess(response.status_code, 400, "%s 被黑名单逻辑改坏了" % path)

    def test_logout_does_not_blacklist_other_users(self):
        """另一个用户的 jti 不同 —— 吊销绝不能误伤。"""
        mine = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=1, jti="mine")
        theirs = mint_renewable_token(ADMIN, role="admin", minutes=30, age_minutes=1, user_id=2, jti="theirs")
        self.client.post("/api/logout", headers=bearer(mine))
        self.assertEqual(self.client.get("/api/admin/stats", headers=bearer(theirs)).status_code, 200)


if __name__ == "__main__":
    unittest.main()
