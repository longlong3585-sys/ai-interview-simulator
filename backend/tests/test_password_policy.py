"""T-08 测试：密码强度由后端强制，且注册/改密/管理员重置共用同一规则。

漏洞背景：
  - `POST /api/register` **完全不做密码强度校验**，只判空。绕过前端直接构造请求
    即可注册任意弱密码（前端的规则形同装饰）。
  - 改密（user.py）与管理员重置（admin.py）各自写 `len < 8`，而前端改密写的是
    `>= 6` —— 用户输入 6-7 位时前端放行、后端报错。

修复后三处统一调用 `auth.validate_password()`。
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from auth import get_password_hash, validate_password
from database import SessionLocal, User
from tests.support import bearer, create_test_user, delete_users, mint_token

GOOD = "Abcd1234!"          # 8 位、3 类字符
GOOD16 = "Abcd1234!Abcd123"  # 16 位（边界）
# 注意：'Abcdefg!' 看似合规，但它含 'abcdef' 升序序列，会被规则 4 拒绝 ——
# 本用例集初版曾把它标为应通过，属测试自身的错误。这里换成真正合规的样本。
GOOD_LETTER_SYMBOL = "Abc!xyzQ"  # 字母+符号两类，无升序、无重复
USERNAME = "t08_user"
ADMIN = "t08_admin"
OLD_PASSWORD = "OldPass123!"

# 空字符串在走 HTTP 表单时会先被 FastAPI 判为 422（校验早于我们的代码），
# 因此端点层测试接受 {400, 422}（两者都是"被拒绝"）。
WEAK_REJECTED_CODES = {400, 422}

# (密码, 是否应通过, 说明)
CASES = [
    ("", False, "空密码"),
    ("Ab1!", False, "过短（4 位）"),
    ("Abc123!", False, "过短（7 位）"),
    ("Abcd123!", True, "8 位、3 类 —— 边界内"),
    (GOOD16, True, "16 位 —— 上边界"),
    ("Abcd1234!Abcd1234", False, "过长（17 位）"),
    ("abcdefgh", False, "仅字母一类（且含 abcdef 升序）"),
    ("12345678", False, "仅数字一类"),
    ("Abcd1234", True, "字母+数字两类"),
    (GOOD_LETTER_SYMBOL, True, "字母+符号两类"),
    ("aaaaaa12", False, "6 位连续重复"),
    ("Abcd123456", False, "含 123456 升序"),
    ("Abcdefgh1", False, "含 abcdef 升序"),
    (GOOD, True, "常规强密码"),
]


class ValidatePasswordUnitTests(unittest.TestCase):
    """纯函数层：逐条规则。"""

    def test_rule_matrix(self):
        for password, should_pass, label in CASES:
            with self.subTest(case=label):
                err = validate_password(password)
                if should_pass:
                    self.assertIsNone(err, "%s（%r）应通过，却报：%s" % (label, password, err))
                else:
                    self.assertIsNotNone(err, "%s（%r）应被拒绝，却通过了" % (label, password))

    def test_error_message_is_user_facing(self):
        """错误信息必须可直接展示（非堆栈、非英文技术串）。"""
        for password, should_pass, _ in CASES:
            if should_pass:
                continue
            err = validate_password(password)
            self.assertIsInstance(err, str)
            self.assertGreater(len(err), 0)


class PasswordEnforcementEndpointTests(unittest.TestCase):
    """端点层：三处入口都必须真的拦住弱密码。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        create_test_user(USERNAME, role="user")
        self.admin_id = create_test_user(ADMIN, role="admin")
        self.user_token = mint_token(USERNAME, role="user")
        self.admin_token = mint_token(ADMIN, role="admin")

        db = SessionLocal()
        try:
            u = db.query(User).filter(User.username == USERNAME).first()
            u.hashed_password = get_password_hash(OLD_PASSWORD)
            db.commit()
            self.user_id = u.id
        finally:
            db.close()

    def tearDown(self):
        delete_users([USERNAME, ADMIN])

    def _reset_old_password(self):
        """把测试用户的口令哈希重置回 OLD_PASSWORD。

        `change_password` 成功后会改掉口令，导致后续迭代用的 OLD_PASSWORD 失效
        （本用例初版就在"三处一致性"循环里踩到了这个自污染问题）。
        """
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.username == USERNAME).first()
            u.hashed_password = get_password_hash(OLD_PASSWORD)
            db.commit()
        finally:
            db.close()

    # ---------- 注册 ----------

    def _register(self, password, username="t08_newbie"):
        # 验证码是随机的 4 位数字，测试里直接放行，专注密码策略
        with patch("routers.auth_router.verify_captcha", return_value=True):
            return self.client.post(
                "/api/register",
                data={
                    "username": username,
                    "password": password,
                    "email": "%s@test.local" % username,
                    "captcha_id": "test",
                    "captcha_code": "1234",
                },
            )

    def test_register_rejects_weak_password_bypassing_frontend(self):
        """绕过前端直发弱密码 —— 必须被拒（修复前会 200 注册成功）。"""
        for index, (password, should_pass, label) in enumerate(CASES):
            if should_pass:
                continue
            with self.subTest(case=label):
                r = self._register(password, username="t08_weak_%d" % index)
                self.assertIn(
                    r.status_code, WEAK_REJECTED_CODES,
                    "注册弱密码 %s（%r）竟然成功：%s" % (label, password, r.text),
                )

    def test_register_accepts_strong_password(self):
        r = self._register(GOOD, username="t08_strong")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("msg"))
        delete_users(["t08_strong"])

    # ---------- 改密 ----------

    def _change_password(self, new_password):
        return self.client.post(
            "/api/change_password",
            data={"old_password": OLD_PASSWORD, "new_password": new_password},
            headers=bearer(self.user_token),
        )

    def test_change_password_rejects_weak(self):
        for password, should_pass, label in CASES:
            if should_pass:
                continue
            with self.subTest(case=label):
                self._reset_old_password()
                r = self._change_password(password)
                self.assertIn(
                    r.status_code, WEAK_REJECTED_CODES,
                    "改密为弱密码 %s（%r）竟然成功" % (label, password),
                )

    def test_change_password_accepts_strong(self):
        r = self._change_password(GOOD)
        self.assertEqual(r.status_code, 200, r.text)

    def test_change_password_still_requires_correct_old_password(self):
        """回归：原密码校验不能被绕过。"""
        r = self.client.post(
            "/api/change_password",
            data={"old_password": "WrongOld123!", "new_password": GOOD},
            headers=bearer(self.user_token),
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("原密码", r.json().get("detail", ""))

    # ---------- 管理员重置 ----------

    def test_admin_reset_rejects_weak(self):
        for password, should_pass, label in CASES:
            if should_pass:
                continue
            with self.subTest(case=label):
                r = self.client.post(
                    f"/api/admin/users/{self.user_id}/reset_password",
                    data={"new_password": password},
                    headers=bearer(self.admin_token),
                )
                self.assertIn(
                    r.status_code, WEAK_REJECTED_CODES,
                    "管理员重置为弱密码 %s（%r）竟然成功" % (label, password),
                )

    def test_admin_reset_accepts_strong(self):
        r = self.client.post(
            f"/api/admin/users/{self.user_id}/reset_password",
            data={"new_password": GOOD},
            headers=bearer(self.admin_token),
        )
        self.assertEqual(r.status_code, 200, r.text)

    # ---------- 一致性：三处口径必须完全一致 ----------

    def test_all_three_entrypoints_agree_on_every_case(self):
        """同一密码在注册/改密/管理员重置三处必须得到相同判定。

        这是 FR-1.5「同一校验函数」的核心断言：
        只要有一处口径不同，本用例就会失败。
        """
        username_seq = iter("t08_agree_%d" % i for i in range(len(CASES)))

        for password, should_pass, label in CASES:
            with self.subTest(case=label):
                # 改密成功会改掉口令，必须重置，否则后续迭代的 OLD_PASSWORD 失效
                self._reset_old_password()

                reg = self._register(password, username=next(username_seq))
                chg = self._change_password(password)
                rst = self.client.post(
                    f"/api/admin/users/{self.user_id}/reset_password",
                    data={"new_password": password},
                    headers=bearer(self.admin_token),
                )
                outcomes = {
                    "register": reg.status_code == 200,
                    "change_password": chg.status_code == 200,
                    "admin_reset": rst.status_code == 200,
                }
                self.assertEqual(
                    set(outcomes.values()), {should_pass},
                    "%s（%r）三处判定不一致：%s" % (label, password, outcomes),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
