"""T-50 / ADR-016 测试：JWT 滑动续期 + 8 小时绝对上限。

分两层，缺一层都会留下"看起来实现了"的假象：

  1. **纯逻辑**（`utils/token_renewal.py`）：用**固定时钟**断言
     "剩余 <50% 才续期""超 8h 拒绝续期""缺声明不续期""续期沿用同一 jti"。
     "8 小时"这种规则靠手点页面是验不出来的 —— 必须能把时钟拨过去。
  2. **接线**（中间件 + `main.app`）：真发请求，断言**响应头**真的有
     `X-Refreshed-Token`、超龄请求真的拿 401 + `X-Token-Expired: absolute`，
     并且公开端点（验证码/登录/题库）不被这条链打扰。

刻意不做的两件事（边界，写清楚以免被当成遗漏）：
  * **不驱动真实浏览器** —— 前端侧的落库与 401 分支由
    `frontend/tests/api-convergence.test.mjs` 的源码断言 + T-36/T-40 的契约测试守着；
  * **不测"过期令牌能否复活"** —— 本实现刻意**不**续期已过期令牌
    （否则 `exp` 形同虚设），这里只断言它确实不续期。
"""

import os
import sys
import time
import unittest

from fastapi.testclient import TestClient
from jose import jwt

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from config import ALGORITHM, SECRET_KEY  # noqa: E402
from tests.support import (  # noqa: E402
    bearer,
    create_test_user,
    delete_users,
    mint_renewable_token,
    mint_token,
)
from utils.token_renewal import (  # noqa: E402
    ABSOLUTE_MAX_LIFETIME_SECONDS,
    REFRESHED_TOKEN_HEADER,
    REASON_ABSOLUTE_LIMIT,
    REASON_EXPIRED,
    REASON_NO_CLAIMS,
    REASON_NOT_NEEDED,
    REASON_RENEWED,
    TOKEN_EXPIRED_ABSOLUTE,
    TOKEN_EXPIRED_HEADER,
    evaluate_renewal,
    exceeds_absolute_lifetime,
    has_renewal_claims,
    origin_auth_claims,
    renewed_claims,
    should_renew,
)

USER = "t50_alice"
OTHER = "t50_bob"

MINUTE = 60
HOUR = 3600


class RenewalLogicTests(unittest.TestCase):
    """第 1 层：纯逻辑（固定时钟，不碰网络/数据库）。"""

    NOW = 1_700_000_000

    def _payload(self, *, iat_offset=-HOUR, exp_offset=HOUR, auth_offset=None, jti="jti-1"):
        if auth_offset is None:
            auth_offset = iat_offset
        return {
            "sub": USER,
            "user_id": 7,
            "role": "user",
            "jti": jti,
            "auth_time": self.NOW + auth_offset,
            "iat": self.NOW + iat_offset,
            "exp": self.NOW + exp_offset,
        }

    def _aged(self, *, age, lifetime, auth_age=None, jti="jti-1"):
        """按"已经活了 `age` 秒、总寿命 `lifetime` 秒"造 payload。

        这样写才不容易错：`_payload` 的两个 offset 都是**相对 NOW** 的，
        而"剩余比例"取决于 `exp - iat`（寿命）与 `exp - now`（剩余）之差 ——
        直接手写两个 offset 极易把寿命写错（本文件第一版就在此翻了 9 条用例）。
        """
        return self._payload(
            iat_offset=-age,
            exp_offset=lifetime - age,
            auth_offset=-(age if auth_age is None else auth_age),
            jti=jti,
        )

    # ---------- 触发条件：剩余 < 50% ----------

    def test_renews_when_less_than_half_remaining(self):
        """30 分钟寿命走了 20 分钟（剩 10 < 15）→ 必须续期。"""
        payload = self._aged(age=20 * MINUTE, lifetime=30 * MINUTE)
        self.assertTrue(should_renew(payload, self.NOW))

    def test_does_not_renew_at_exactly_half(self):
        """恰好一半（剩 15 分钟）→ **不**续期（阈值是严格小于）。
        边界取严可以避免"每次请求都续期"的写放大。"""
        payload = self._aged(age=15 * MINUTE, lifetime=30 * MINUTE)
        self.assertFalse(should_renew(payload, self.NOW))

    def test_does_not_renew_just_past_half(self):
        """刚过半衰点一秒（剩 14 分 59 秒）→ 续期（证明阈值不是"永远不续期"）。"""
        payload = self._aged(age=15 * MINUTE + 1, lifetime=30 * MINUTE)
        self.assertTrue(should_renew(payload, self.NOW))

    def test_does_not_renew_when_plenty_remaining(self):
        """刚签发（剩 29/30）→ 不续期。"""
        payload = self._aged(age=1 * MINUTE, lifetime=30 * MINUTE)
        self.assertFalse(should_renew(payload, self.NOW))

    def test_expired_token_is_not_renewed(self):
        """已过期 → 不续期（否则过期令牌可无限复活，exp 形同虚设）。"""
        payload = self._aged(age=40 * MINUTE, lifetime=30 * MINUTE)
        self.assertFalse(should_renew(payload, self.NOW))
        should_issue, claims, reason = evaluate_renewal(payload, 30 * MINUTE, self.NOW)
        self.assertFalse(should_issue)
        self.assertIsNone(claims)
        self.assertEqual(reason, REASON_EXPIRED)

    # ---------- 绝对上限 8 小时 ----------

    def test_absolute_limit_boundary(self):
        """恰好 8 小时**算超限**（`>=`），差一秒不算。"""
        fresh = self._payload(auth_offset=-ABSOLUTE_MAX_LIFETIME_SECONDS + 1)
        hit = self._payload(auth_offset=-ABSOLUTE_MAX_LIFETIME_SECONDS)
        self.assertFalse(exceeds_absolute_lifetime(fresh, self.NOW))
        self.assertTrue(exceeds_absolute_lifetime(hit, self.NOW))

    def test_rejects_renewal_beyond_absolute_limit(self):
        """仍然有效（剩 10 分钟）但 auth_time 已 9 小时 → 拒绝续期，原因是 absolute_limit。"""
        payload = self._aged(age=20 * MINUTE, lifetime=30 * MINUTE, auth_age=9 * HOUR)
        should_issue, claims, reason = evaluate_renewal(payload, 30 * MINUTE, self.NOW)
        self.assertFalse(should_issue)
        self.assertIsNone(claims)
        self.assertEqual(reason, REASON_ABSOLUTE_LIMIT)

    def test_missing_auth_time_fails_closed(self):
        """缺 auth_time → 视为超限（fail-closed）：绝不签出一条"没有绝对上限"的链。"""
        payload = self._aged(age=20 * MINUTE, lifetime=30 * MINUTE)
        del payload["auth_time"]
        self.assertTrue(exceeds_absolute_lifetime(payload, self.NOW))
        self.assertFalse(has_renewal_claims(payload))
        _, _, reason = evaluate_renewal(payload, 30 * MINUTE, self.NOW)
        self.assertEqual(reason, REASON_NO_CLAIMS)

    def test_garbage_auth_time_is_treated_as_missing(self):
        for bad in ("abc", "", None, True, [], {}):
            payload = self._aged(age=20 * MINUTE, lifetime=30 * MINUTE)
            payload["auth_time"] = bad
            self.assertTrue(
                exceeds_absolute_lifetime(payload, self.NOW),
                "auth_time=%r 竟然被判为『在绝对上限内』" % (bad,),
            )

    # ---------- 缺声明 / 畸形 payload ----------

    def test_legacy_token_without_claims_is_never_renewed(self):
        """老令牌（T-50 之前签发：无 jti / auth_time）不续期，但**不影响鉴权**。"""
        legacy = {"sub": USER, "role": "user", "iat": self.NOW - 20 * MINUTE, "exp": self.NOW + 10 * MINUTE}
        self.assertFalse(has_renewal_claims(legacy))
        self.assertFalse(should_renew(legacy, self.NOW))
        self.assertFalse(evaluate_renewal(legacy, 30 * MINUTE, self.NOW)[0])

    def test_malformed_payloads_are_handled(self):
        for bad in (None, {}, "x", 7, []):
            should_issue, claims, reason = evaluate_renewal(bad, 30 * MINUTE, self.NOW)
            self.assertFalse(should_issue)
            self.assertIsNone(claims)
            self.assertIn(reason, (REASON_NO_CLAIMS, REASON_EXPIRED, REASON_ABSOLUTE_LIMIT))

    def test_iat_after_exp_is_refused(self):
        payload = self._payload(iat_offset=+10 * MINUTE, exp_offset=+5 * MINUTE)
        self.assertFalse(should_renew(payload, self.NOW))

    # ---------- 续期结果本身 ----------

    def test_renewed_claims_keep_identity_and_jti_and_auth_time(self):
        payload = self._aged(age=20 * MINUTE, lifetime=30 * MINUTE)
        claims, new_exp = renewed_claims(payload, payload["auth_time"], 30 * MINUTE, self.NOW)
        self.assertEqual(claims["sub"], USER)
        self.assertEqual(claims["user_id"], 7)
        self.assertEqual(claims["role"], "user")
        # ADR-016 第 4 点：沿用同一 jti（T-51 的黑名单据此吊销整条链）
        self.assertEqual(claims["jti"], payload["jti"])
        # 绝对上限**不因续期而重置**
        self.assertEqual(claims["auth_time"], payload["auth_time"])
        self.assertEqual(claims["exp"], self.NOW + 30 * MINUTE)
        self.assertEqual(claims["iat"], self.NOW)
        self.assertEqual(new_exp, self.NOW + 30 * MINUTE)
        # 续期后仍然可续期（还剩满额），但不会立刻再续（剩 30/30 > 50%）
        self.assertFalse(should_renew(claims, self.NOW))

    def test_origin_claims_are_fresh_each_time(self):
        first = origin_auth_claims(self.NOW)
        second = origin_auth_claims(self.NOW)
        self.assertEqual(first["auth_time"], self.NOW)
        self.assertTrue(first["jti"] and len(first["jti"]) >= 16)
        self.assertNotEqual(first["jti"], second["jti"], "两次签发的 jti 相同 —— 吊销会误伤")

    def test_renew_evaluation_reports_renewed(self):
        payload = self._aged(age=20 * MINUTE, lifetime=30 * MINUTE)
        should_issue, claims, reason = evaluate_renewal(payload, 30 * MINUTE, self.NOW)
        self.assertTrue(should_issue)
        self.assertEqual(reason, REASON_RENEWED)
        self.assertEqual(claims["jti"], payload["jti"])

    def test_not_needed_reports_reason(self):
        payload = self._payload(iat_offset=-1 * MINUTE, exp_offset=29 * MINUTE, auth_offset=-1 * MINUTE)
        should_issue, claims, reason = evaluate_renewal(payload, 30 * MINUTE, self.NOW)
        self.assertFalse(should_issue)
        self.assertIsNone(claims)
        self.assertEqual(reason, REASON_NOT_NEEDED)


class RenewalEndpointTests(unittest.TestCase):
    """第 2 层：真发请求（中间件 + 路由），只看响应头与状态码。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()  # 触发 startup（与生产一致）

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        create_test_user(USER, role="user")
        create_test_user(OTHER, role="user")

    def tearDown(self):
        delete_users([USER, OTHER])

    # 用一个只读、鉴权直白的受保护端点观察响应头。
    PROBE_PATH = "/api/interview/config"

    def _get_config(self, token):
        return self.client.get(self.PROBE_PATH, headers=bearer(token))

    def _decode(self, token):
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

    def test_low_remaining_lifetime_gets_refreshed_token_header(self):
        """剩余 <50%（21/30 已过）→ 响应头回写新令牌，且身份/jti/auth_time 与旧令牌一致。"""
        token = mint_renewable_token(USER, minutes=30, age_minutes=21, user_id=7)
        old = self._decode(token)
        response = self._get_config(token)
        self.assertEqual(response.status_code, 200)
        new_token = response.headers.get(REFRESHED_TOKEN_HEADER)
        self.assertTrue(new_token, "响应里没有 %s —— 滑动续期没有生效" % REFRESHED_TOKEN_HEADER)
        new = self._decode(new_token)
        self.assertEqual(new["sub"], old["sub"])
        self.assertEqual(new.get("user_id"), old.get("user_id"))
        self.assertEqual(new["role"], old["role"])
        self.assertEqual(new["jti"], old["jti"], "续期没有沿用同一 jti（ADR-016 第 4 点）")
        self.assertEqual(new["auth_time"], old["auth_time"], "续期把绝对上限重置了（安全洞）")
        self.assertGreater(new["exp"], old["exp"], "新令牌的有效期没有往后延")

    def test_refreshed_token_itself_works(self):
        """续期令牌必须**真的能用**（不能只是头里好看）。"""
        token = mint_renewable_token(USER, minutes=30, age_minutes=21, user_id=7)
        new_token = self._get_config(token).headers.get(REFRESHED_TOKEN_HEADER)
        response = self._get_config(new_token)
        self.assertEqual(response.status_code, 200)
        # 刚续期（剩满额）→ 不再续期，避免每次请求都写新令牌。
        self.assertIsNone(
            response.headers.get(REFRESHED_TOKEN_HEADER),
            "刚续期的令牌又被续了一次 —— 阈值判定失效（写放大）",
        )

    def test_fresh_token_gets_no_refreshed_header(self):
        token = mint_renewable_token(USER, minutes=30, age_minutes=1, user_id=7)
        response = self._get_config(token)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get(REFRESHED_TOKEN_HEADER))

    def test_legacy_token_still_authenticates_without_renewal(self):
        """老令牌（无 jti/auth_time）鉴权照旧，只是不续期 —— 不能把用户挡在门外。"""
        token = mint_token(USER, role="user")
        response = self._get_config(token)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get(REFRESHED_TOKEN_HEADER))

    def test_absolute_limit_returns_401_with_marker(self):
        """超 8 小时 → 401 + X-Token-Expired: absolute（且**没有**续期头）。

        注意令牌本身**必须仍然有效**（`exp` 还没到），否则测的是"过期"而不是"绝对上限"：
        因此这里用"寿命 10 小时、已活 9 小时、还剩 1 小时"的令牌。
        """
        token = mint_renewable_token(USER, minutes=600, age_minutes=9 * 60, user_id=7)
        response = self._get_config(token)
        self.assertEqual(response.status_code, 401, "超过 8 小时绝对上限的请求竟然被放行")
        self.assertEqual(response.headers.get(TOKEN_EXPIRED_HEADER), TOKEN_EXPIRED_ABSOLUTE)
        self.assertIsNone(response.headers.get(REFRESHED_TOKEN_HEADER))
        # 服务端会话仍在：重新登录即可恢复（这里只断言错误体可读，便于前端展示文案）
        self.assertIn("重新登录", response.json().get("detail", ""))

    def test_absolute_limit_blocks_state_changing_endpoints_too(self):
        """不只是读接口：写接口同样被硬上限挡住（否则等于没有上限）。"""
        token = mint_renewable_token(USER, minutes=600, age_minutes=9 * 60, user_id=7)
        response = self.client.get("/api/user/profile", headers=bearer(token))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get(TOKEN_EXPIRED_HEADER), TOKEN_EXPIRED_ABSOLUTE)

    def test_expired_token_is_a_plain_401_without_absolute_marker(self):
        """过期令牌 = 普通 401（**不带** absolute 标记）→ 前端走统一登出。"""
        token = mint_renewable_token(USER, minutes=30, age_minutes=40)
        response = self._get_config(token)
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(response.headers.get(TOKEN_EXPIRED_HEADER))

    def test_invalid_token_is_not_touched_by_renewal(self):
        response = self._get_config("not-a-jwt")
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(response.headers.get(TOKEN_EXPIRED_HEADER))
        self.assertIsNone(response.headers.get(REFRESHED_TOKEN_HEADER))

    def test_public_endpoints_are_untouched(self):
        """公开端点不该被续期链打扰（它们本来就不带令牌）。"""
        for path in ("/api/question_bank", "/api/captcha"):
            response = self.client.get(path)
            self.assertLess(response.status_code, 400, "%s 被中间件改坏了" % path)
            self.assertIsNone(response.headers.get(REFRESHED_TOKEN_HEADER))
            self.assertIsNone(response.headers.get(TOKEN_EXPIRED_HEADER))

    def test_wrong_user_token_still_401_without_marker(self):
        """另一个人的令牌（合法签名但用户不存在）→ 普通 401，不误报绝对超时。"""
        ghost = mint_renewable_token("t50_ghost", minutes=30, age_minutes=21)
        response = self._get_config(ghost)
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(response.headers.get(TOKEN_EXPIRED_HEADER))


class RenewalConfigTests(unittest.TestCase):
    """常量与单一来源：8 小时上限、50% 阈值、以及"令牌寿命只有一个来源"。"""

    def test_constants_match_adr_016(self):
        self.assertEqual(ABSOLUTE_MAX_LIFETIME_SECONDS, 8 * 3600)
        import utils.token_renewal as mod
        self.assertEqual(mod.RENEWAL_THRESHOLD_RATIO, 0.5)
        self.assertEqual(REFRESHED_TOKEN_HEADER, "X-Refreshed-Token")
        self.assertEqual(TOKEN_EXPIRED_HEADER, "X-Token-Expired")

    def test_cors_exposes_both_headers(self):
        """跨域下浏览器只交出 expose_headers 里列的头 —— 漏一个就等于没实现。"""
        import main as app_main
        cors = [m for m in app_main.app.user_middleware if m.cls.__name__ == "CORSMiddleware"]
        self.assertTrue(cors, "main.py 里没有 CORS 中间件")
        exposed = cors[0].kwargs.get("expose_headers") or []
        self.assertIn("X-Refreshed-Token", exposed)
        self.assertIn("X-Token-Expired", exposed)
        # ADR-016 第 2 点：来源不得为通配（credentials 模式下浏览器不允许读通配来源的头）
        origins = cors[0].kwargs.get("allow_origins") or []
        self.assertNotIn("*", origins, "CORS 来源配成了通配 —— 前端读不到续期头")

    def test_token_ttl_has_single_source(self):
        """`create_access_token` 的默认寿命必须来自 config，而不是函数内写死的 15 分钟。"""
        import inspect
        from auth import create_access_token
        from config import ACCESS_TOKEN_EXPIRE_MINUTES
        source = inspect.getsource(create_access_token)
        self.assertIn("ACCESS_TOKEN_EXPIRE_MINUTES", source)
        self.assertNotIn("timedelta(minutes=15)", source)
        self.assertEqual(ACCESS_TOKEN_EXPIRE_MINUTES, 30)

    def test_login_token_carries_renewal_claims(self):
        """登录签发的令牌必须带上 jti/auth_time/user_id（否则这条链只有测试里的令牌能续期）。"""
        from auth import create_access_token
        token = create_access_token({"sub": USER, "user_id": 7, "role": "user"})
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        self.assertTrue(has_renewal_claims(payload), "登录令牌缺少续期声明")
        self.assertEqual(payload["user_id"], 7)


if __name__ == "__main__":
    unittest.main()
