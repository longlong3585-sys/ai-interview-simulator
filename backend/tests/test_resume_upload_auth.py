"""T-05 测试：/api/resume/upload 的鉴权（FR-2.4 / Bug 1）。

覆盖的核心风险：
  1. 无令牌 / 令牌无效 → 必须 401（修复前该接口完全裸奔，未登录即可触发
     PDF/DOCX 解析，构成资源消耗攻击面）
  2. **鉴权通过后才做格式/大小校验**：带令牌 + 错误格式应得 400 而非 401，
     以此反证鉴权确实放行了
  3. **依赖选型验证**：该接口挂的是 `get_current_user` 而非 `require_user`，
     因此 **admin 应能上传**（若误挂 require_user 会得到 403）
  4. 正常 DOCX 走通全链路：解析 → 清洗 → 生成问题

上传接口不写磁盘、不写数据库，故无需清理副作用。
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from config import MAX_FILE_SIZE
from tests.support import (
    DOCX_MIME,
    PDF_MIME,
    bearer,
    create_test_user,
    delete_users,
    make_docx_bytes,
    mint_token,
)

USERNAME = "t05_alice"
ADMIN_NAME = "t05_admin"
URL = "/api/resume/upload"


def _fake_questions(*args, **kwargs):
    """替身问题生成：不发网络请求。"""
    return ["Q1", "Q2", "Q3"]


class ResumeUploadAuthTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        create_test_user(USERNAME, role="user")
        create_test_user(ADMIN_NAME, role="admin")
        self.token = mint_token(USERNAME, role="user")
        self.admin_token = mint_token(ADMIN_NAME, role="admin")

    def tearDown(self):
        delete_users([USERNAME, ADMIN_NAME])

    def _upload(self, token, filename="resume.docx", content=b"x", mime=DOCX_MIME):
        return self.client.post(
            URL,
            files={"file": (filename, content, mime)},
            headers=bearer(token),
        )

    # ---------- 1. 鉴权 ----------

    def test_upload_without_token_returns_401(self):
        """无令牌必须 401（修复前为 200/400，属严重漏洞）。"""
        r = self._upload(None)
        self.assertEqual(r.status_code, 401, "无令牌竟然被放行：%s" % r.text)

    def test_upload_with_malformed_token_returns_401(self):
        r = self._upload("not-a-real-jwt")
        self.assertEqual(r.status_code, 401)

    def test_upload_with_expired_token_returns_401(self):
        expired = mint_token(USERNAME, role="user", minutes=-5)
        r = self._upload(expired)
        self.assertEqual(r.status_code, 401)

    # ---------- 2. 鉴权放行后才做业务校验 ----------

    def test_authenticated_but_wrong_mime_gets_400_not_401(self):
        """带令牌 + .txt：应为 400（格式错），而不是 401（未鉴权）。

        这是"鉴权确实放行了"的反证：若鉴权没过，绝不会走到格式校验。
        """
        r = self._upload(self.token, filename="notes.txt", content=b"hello", mime="text/plain")
        self.assertEqual(
            r.status_code, 400,
            "期望 400（格式不支持），实际 %s：%s" % (r.status_code, r.text),
        )
        self.assertIn("仅支持 PDF 或 DOCX", r.json().get("detail", ""))

    def test_authenticated_oversized_file_gets_400(self):
        """带令牌 + 超过 5MB：应为 400（大小超限）。"""
        big = b"\0" * (MAX_FILE_SIZE + 1)
        r = self._upload(self.token, filename="big.docx", content=big)
        self.assertEqual(r.status_code, 400)
        self.assertIn("5MB", r.json().get("detail", ""))

    # ---------- 3. 依赖选型：admin 不应被 403 ----------

    def test_admin_can_upload(self):
        """admin 必须能上传（证明挂的是 get_current_user，不是 require_user）。"""
        content = make_docx_bytes(["张三", "技能：Python"])
        with patch("utils.ai_helpers.generate_questions", side_effect=_fake_questions), \
             patch("routers.interview.generate_questions", side_effect=_fake_questions):
            r = self._upload(self.admin_token, content=content)
        self.assertEqual(
            r.status_code, 200,
            "admin 上传被拒（%s）—— 依赖可能误挂成了 require_user" % r.status_code,
        )
        self.assertTrue(r.json().get("success"))

    # ---------- 4. 正常链路 ----------

    def test_valid_docx_upload_succeeds(self):
        content = make_docx_bytes(
            ["候选人：张三", "技能：Python、Kubernetes、FastAPI", "项目：智能面试系统"]
        )
        with patch("routers.interview.generate_questions", side_effect=_fake_questions):
            r = self._upload(self.token, content=content)

        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["filename"], "resume.docx")
        self.assertEqual(data["questions"], ["Q1", "Q2", "Q3"])
        # 文本确实被解析出来了（不是空壳）
        self.assertIn("Kubernetes", data["full_text"])
        self.assertTrue(data["preview"])

    def test_docx_without_extractable_text_gets_400(self):
        """空文档：解析不出文本 → 400，且给的是可读错误而非 500。"""
        content = make_docx_bytes([])
        r = self._upload(self.token, content=content)
        self.assertEqual(r.status_code, 400)
        self.assertIn("无法提取文本内容", r.json().get("detail", ""))

    def test_corrupt_docx_gets_400_not_500(self):
        """损坏的 DOCX：必须给可读错误，不能 500。"""
        r = self._upload(self.token, content=b"this is not a real docx file")
        self.assertEqual(
            r.status_code, 400,
            "损坏文件应返回 400，实际 %s" % r.status_code,
        )

    def test_pdf_mime_is_accepted_by_validation(self):
        """PDF 分支：MIME 通过校验后才会走解析（此处内容非法 → 400 解析失败）。"""
        r = self._upload(self.token, filename="r.pdf", content=b"%PDF-1.4 broken", mime=PDF_MIME)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("仅支持 PDF 或 DOCX", r.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
