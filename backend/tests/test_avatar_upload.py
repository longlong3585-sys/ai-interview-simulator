"""T-09 测试：头像上传的服务端校验（FR-10.4）。

漏洞背景（修复前的 upload_avatar）:
  - 只检查 `file.content_type.startswith("image/")` —— content_type 是**客户端
    可任意伪造**的请求头，等于没校验
  - **完全没有大小校验** → 传一个 100MB 文件即可写满磁盘
  - 按客户端提供的扩展名原样拼接文件名写入静态目录 → 可落地 .html 等可执行内容

修复后：扩展名白名单 → 大小上限（边读边限）→ 文件头魔数 → 统一重命名。
"""

import io
import os
import unittest
import uuid

from fastapi.testclient import TestClient

import main  # noqa: E402  —— 环境隔离由 tests/__init__.py 保证
from config import MAX_AVATAR_SIZE, UPLOAD_DIR
from tests.support import bearer, create_test_user, delete_users, mint_token
from utils.upload_validation import (
    detect_image_type,
    extension_is_allowed,
    is_managed_avatar_url,
    safe_avatar_filename,
)

USERNAME = "t09_user"
URL = "/api/user/avatar"

# 真实文件头样本（内容尾部随意，魔数才是判定依据）
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64


class DetectImageTypeTests(unittest.TestCase):
    """纯函数层：魔数识别与命名。"""

    def test_recognizes_real_formats(self):
        self.assertEqual(detect_image_type(PNG), ("image/png", ".png"))
        self.assertEqual(detect_image_type(JPEG), ("image/jpeg", ".jpg"))
        self.assertEqual(detect_image_type(GIF), ("image/gif", ".gif"))
        self.assertEqual(detect_image_type(WEBP), ("image/webp", ".webp"))

    def test_rejects_non_images(self):
        for label, data in [
            ("空", b""),
            ("纯文本", b"this is not an image"),
            ("HTML", b"<html><body>hi</body></html>"),
            ("SVG（刻意不支持：可内嵌脚本）", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
            ("PDF", b"%PDF-1.4"),
            ("只截断 PNG 前 4 字节", b"\x89PNG"),
        ]:
            with self.subTest(case=label):
                self.assertIsNone(detect_image_type(data), "%s 不应被识别为图片" % label)

    def test_extension_whitelist(self):
        """严格白名单：未知或缺失扩展名一律拒绝（真实前端固定发 .jpg）。"""
        for name, ok in [
            ("a.png", True), ("a.JPG", True), ("a.jpeg", True),
            ("a.gif", True), ("a.webp", True),
            ("a.html", False), ("a.svg", False), ("a.php", False),
            ("a.exe", False), ("noext", False), ("a.tar.gz", False),
        ]:
            with self.subTest(name=name):
                self.assertEqual(extension_is_allowed(name), ok, name)

    def test_missing_filename_is_not_blocked_here(self):
        """文件名缺失时不在此处拦截，交由魔数判定（二者职责分离）。"""
        self.assertTrue(extension_is_allowed(None))
        self.assertTrue(extension_is_allowed(""))

    def test_safe_filename_ignores_client_input(self):
        """扩展名必须来自真实格式，与客户端文件名无关。"""
        name = safe_avatar_filename(7, ".png", "deadbeef")
        self.assertEqual(name, "user_7_deadbeef.png")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)

    def test_is_managed_avatar_url_blocks_path_traversal(self):
        for url, expected in [
            ("/uploads/avatars/user_1_abc.png", True),
            ("/uploads/avatars/../config.py", False),
            ("/uploads/avatars/..%2f..%2fetc", False),
            ("/uploads/avatars/other.png", False),
            ("/etc/passwd", False),
            (None, False),
            ("", False),
        ]:
            with self.subTest(url=url):
                self.assertEqual(is_managed_avatar_url(url, UPLOAD_DIR), expected)


class AvatarUploadEndpointTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.client.__enter__()
        cls.created_files = []

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        create_test_user(USERNAME, role="user")
        self.token = mint_token(USERNAME, role="user")

    def tearDown(self):
        delete_users([USERNAME])
        # 清理本次用例写出的头像文件，避免污染 uploads/
        for path in self.created_files:
            if os.path.isfile(path):
                os.remove(path)
        self.created_files.clear()

    def _upload(self, content, filename="a.png", content_type="image/png", token=None):
        r = self.client.post(
            URL,
            files={"file": (filename, content, content_type)},
            headers=bearer(self.token if token is None else token),
        )
        if r.status_code == 200:
            name = r.json()["avatar_url"].rsplit("/", 1)[-1]
            self.created_files.append(os.path.join(UPLOAD_DIR, name))
        return r

    # ---------- 鉴权 ----------

    def test_upload_requires_auth(self):
        self.assertEqual(self._upload(PNG, token="").status_code, 401)

    # ---------- 正常路径 ----------

    def test_valid_png_accepted(self):
        r = self._upload(PNG)
        self.assertEqual(r.status_code, 200, r.text)
        url = r.json()["avatar_url"]
        self.assertTrue(url.startswith("/uploads/avatars/user_"))
        self.assertTrue(url.endswith(".png"))
        self.assertTrue(os.path.isfile(os.path.join(UPLOAD_DIR, url.rsplit("/", 1)[-1])))

    # ---------- 核心：伪造 content_type 必须无效 ----------

    def test_fake_content_type_is_rejected_by_magic_number(self):
        """把文本/HTML 伪装成 image/png —— 必须被文件头校验拦下。

        这是修复前最典型的绕过方式：content_type 是客户端可控的请求头。
        """
        for label, payload in [
            ("纯文本", b"just some text"),
            ("HTML", b"<html><script>alert(1)</script></html>"),
            ("PDF", b"%PDF-1.4 fake"),
        ]:
            with self.subTest(case=label):
                r = self._upload(payload, filename="evil.png", content_type="image/png")
                self.assertEqual(
                    r.status_code, 400,
                    "%s 伪装成 image/png 竟然被接受：%s" % (label, r.text),
                )

    def test_html_extension_rejected(self):
        r = self._upload(b"<html></html>", filename="x.html", content_type="image/png")
        self.assertEqual(r.status_code, 400)

    def test_svg_rejected(self):
        """SVG 可在静态目录下导致存储型 XSS，刻意不支持。"""
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        r = self._upload(svg, filename="x.svg", content_type="image/svg+xml")
        self.assertEqual(r.status_code, 400)

    # ---------- 大小上限 ----------

    def test_oversized_avatar_rejected(self):
        r = self._upload(PNG + b"\x00" * MAX_AVATAR_SIZE, filename="big.png")
        self.assertEqual(r.status_code, 400)
        self.assertIn("MB", r.json().get("detail", ""))

    def test_size_boundary(self):
        """恰好等于上限应通过；超过 1 字节应被拒。"""
        exact = PNG + b"\x00" * (MAX_AVATAR_SIZE - len(PNG))
        self.assertEqual(len(exact), MAX_AVATAR_SIZE)
        self.assertEqual(self._upload(exact, filename="exact.png").status_code, 200)

        over = PNG + b"\x00" * (MAX_AVATAR_SIZE - len(PNG) + 1)
        self.assertEqual(self._upload(over, filename="over.png").status_code, 400)

    # ---------- 重命名：不采用客户端扩展名 ----------

    def test_stored_filename_uses_detected_extension(self):
        """客户端谎报 .jpg 但内容其实是 PNG —— 落盘扩展名应为 .png。"""
        r = self._upload(PNG, filename="lie.jpg", content_type="image/jpeg")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["avatar_url"].endswith(".png"))

    def test_replacing_avatar_removes_old_file(self):
        """替换头像后旧文件应被清理，避免孤儿文件累积。"""
        first = self._upload(PNG).json()["avatar_url"]
        first_path = os.path.join(UPLOAD_DIR, first.rsplit("/", 1)[-1])
        self.assertTrue(os.path.isfile(first_path))

        second = self._upload(JPEG, filename="b.jpg").json()["avatar_url"]
        second_path = os.path.join(UPLOAD_DIR, second.rsplit("/", 1)[-1])

        self.assertFalse(os.path.isfile(first_path), "旧头像文件未被清理")
        self.assertTrue(os.path.isfile(second_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
