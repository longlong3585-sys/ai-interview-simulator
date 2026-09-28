"""T-06 测试：账号被禁用后，既有令牌必须立即失效（FR-2.6 / NFR-1）。

漏洞背景：
  `get_current_user` 此前**不检查 `is_active`** —— 管理员在后台"禁用"某用户后，
  该用户**此前签发的令牌在有效期内（默认 30 分钟）仍然可以读写全部用户接口**。
  修复点在鉴权链最底层，因此 `get_current_user` / `require_user` /
  `get_current_admin_user` 三条链路应同时生效 —— 本文件逐条验证。
"""

import unittest

from fastapi.testclient import TestClient

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from auth import authenticate_user, get_password_hash
from database import SessionLocal, User
from tests.support import (
    bearer,
    create_test_user,
    delete_users,
    make_docx_bytes,
    mint_token,
    set_user_active,
)

USER = "t06_user"
ADMIN = "t06_admin"
DISABLED_ADMIN = "t06_disabled_admin"

PROTECTED_ENDPOINTS = [
    # (方法, 路径, 说明, 依赖链)
    ("GET", "/api/user/profile", "个人资料", "get_current_user"),
    ("GET", "/api/user/stats", "个人统计", "get_current_user"),
    ("GET", "/api/history", "面试历史", "require_user"),
    ("GET", "/api/notifications", "通知列表", "require_user"),
]


class DisabledUserTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        create_test_user(USER, role="user", is_active=True)
        create_test_user(ADMIN, role="admin", is_active=True)
        self.token = mint_token(USER, role="user")
        self.admin_token = mint_token(ADMIN, role="admin")

    def tearDown(self):
        delete_users([USER, ADMIN, DISABLED_ADMIN])

    def _call(self, method, path, token):
        return self.client.request(method, path, headers=bearer(token))

    # ---------- 1. 基线：启用状态下必须可用 ----------

    def test_active_user_can_access_protected_endpoints(self):
        for method, path, label, _ in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=path):
                r = self._call(method, path, self.token)
                self.assertEqual(
                    r.status_code, 200,
                    "启用状态下 %s(%s) 应可用，实际 %s" % (label, path, r.status_code),
                )

    # ---------- 2. 核心：禁用后立刻失效 ----------

    def test_disabled_user_token_is_rejected_on_all_protected_endpoints(self):
        """管理员禁用后，旧令牌在所有受保护接口上必须立即被拒。"""
        self.assertTrue(set_user_active(USER, False))

        for method, path, label, chain in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=path):
                r = self._call(method, path, self.token)
                self.assertEqual(
                    r.status_code, 401,
                    "%s(%s) 在账号被禁用后仍返回 %s —— 链路 %s 未生效"
                    % (label, path, r.status_code, chain),
                )
                self.assertIn("禁用", r.json().get("detail", ""))

    def test_disabled_user_token_rejected_on_chat(self):
        """POST /api/chat（require_user）同样必须被拒。"""
        set_user_active(USER, False)
        r = self.client.post(
            "/api/chat", json={"message": "hi"}, headers=bearer(self.token)
        )
        self.assertEqual(r.status_code, 401)

    def test_disabled_user_token_rejected_on_resume_upload(self):
        """POST /api/resume/upload（get_current_user）同样必须被拒。"""
        set_user_active(USER, False)
        r = self.client.post(
            "/api/resume/upload",
            files={"file": ("r.docx", make_docx_bytes(["x"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            headers=bearer(self.token),
        )
        self.assertEqual(r.status_code, 401, "禁用用户竟能上传：%s" % r.status_code)

    # ---------- 3. 真实场景：先登录拿令牌，再被禁用 ----------

    def test_token_issued_before_disable_stops_working_immediately(self):
        """模拟真实时序：拿令牌 → 能用 → 管理员禁用 → 立刻不能用。"""
        r_before = self._call("GET", "/api/user/profile", self.token)
        self.assertEqual(r_before.status_code, 200, "前置条件失败：令牌本应可用")

        set_user_active(USER, False)

        r_after = self._call("GET", "/api/user/profile", self.token)
        self.assertEqual(r_after.status_code, 401, "禁用后旧令牌仍可用 —— 漏洞未修复")

    def test_admin_can_disable_user_via_api_and_effect_is_immediate(self):
        """走真实管理端接口禁用（而非直接改库），验证端到端生效。"""
        from database import SessionLocal, User

        db = SessionLocal()
        try:
            target_id = db.query(User).filter(User.username == USER).first().id
        finally:
            db.close()

        r = self.client.patch(
            f"/api/admin/users/{target_id}/toggle_active",
            headers=bearer(self.admin_token),
        )
        self.assertEqual(r.status_code, 200, r.text)

        r_after = self._call("GET", "/api/user/profile", self.token)
        self.assertEqual(r_after.status_code, 401, "经管理端禁用后旧令牌仍可用")

    # ---------- 4. 恢复与幂等 ----------

    def test_reenabling_restores_access(self):
        set_user_active(USER, False)
        self.assertEqual(self._call("GET", "/api/user/profile", self.token).status_code, 401)

        set_user_active(USER, True)
        self.assertEqual(
            self._call("GET", "/api/user/profile", self.token).status_code, 200,
            "重新启用后旧令牌应恢复可用",
        )

    # ---------- 5. 管理员链路 ----------

    def test_disabled_admin_is_also_blocked_on_admin_endpoints(self):
        """被禁用的 admin 在管理端接口（get_current_admin_user）也必须被拒。"""
        create_test_user(DISABLED_ADMIN, role="admin", is_active=False)
        token = mint_token(DISABLED_ADMIN, role="admin")

        r = self.client.get("/api/admin/users", headers=bearer(token))
        self.assertEqual(r.status_code, 401, "被禁用的 admin 仍能访问管理端：%s" % r.status_code)

    # ---------- 6. 回归：登录侧既有行为不变 ----------

    def test_login_rejects_disabled_user(self):
        """禁用用户即使拿着正确密码也不能登录（与 401 语义保持一致）。"""
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.username == USER).first()
            u.hashed_password = get_password_hash("CorrectHorse123!")
            u.is_active = False
            db.commit()
        finally:
            db.close()

        db = SessionLocal()
        try:
            result = authenticate_user(db, USER, "CorrectHorse123!")
            self.assertFalse(result, "禁用用户竟然登录成功")
        finally:
            db.close()

    def test_enabled_user_can_login(self):
        """同一密码、仅 is_active 不同 —— 确保上一条失败是因为禁用，而非密码错。"""
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.username == USER).first()
            u.hashed_password = get_password_hash("CorrectHorse123!")
            u.is_active = True
            db.commit()
        finally:
            db.close()

        db = SessionLocal()
        try:
            self.assertIsNotNone(authenticate_user(db, USER, "CorrectHorse123!"))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
