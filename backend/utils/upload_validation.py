"""T-09 / FR-10.4：上传图片的**服务端**校验工具。

修复前 `upload_avatar` 只检查 `file.content_type.startswith("image/")` ——
而 `content_type` 是**客户端可任意伪造**的请求头；同时**完全没有大小校验**，
且直接把客户端提供的扩展名拼进文件名写入静态目录。后果：
  - 传一个 100MB 的文件即可写满磁盘（无界磁盘写）
  - 传 "x.html" 且 content_type 写成 image/png，即可在 /uploads 下落地可执行内容

本模块提供两项与框架无关的纯函数，便于单元测试：
  - detect_image_type(): 按**文件头（魔数）**判定真实格式
  - safe_avatar_filename(): 统一重命名（扩展名由真实格式决定，不用客户端输入）
"""

from typing import Optional, Tuple

# 允许的图片格式：魔数 → (规范 MIME, 规范扩展名)
#
# 说明：这里刻意**不接受 SVG** —— SVG 是 XML，可内嵌脚本，
# 在静态目录下与 HTML 同源可导致存储型 XSS；前端头像也不需要矢量图。
_SIGNATURES: Tuple[Tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)


def detect_image_type(data: bytes) -> Optional[Tuple[str, str]]:
    """按文件头判定真实图片类型。

    返回 (mime, 扩展名)；无法识别返回 None。
    注意：**不信任**客户端提供的 filename 与 content_type。
    """
    if not data:
        return None
    for signature, mime, ext in _SIGNATURES:
        if data.startswith(signature):
            return mime, ext
    # WebP: 'RIFF' + 4 字节长度 + 'WEBP'
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


# 允许的扩展名白名单（仅用于校验，不参与命名）
ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def extension_is_allowed(filename: Optional[str]) -> bool:
    """客户端文件名后缀是否在白名单内。

    这是**便宜的预筛**，不是安全边界（真正的安全判定是 `detect_image_type`）。
    策略为**严格白名单**：无扩展名（如 `noext`）或未知扩展名一律拒绝 ——
    真实前端固定提交 `avatar.jpg`，不存在需要放行的合法无扩展名场景。
    文件名缺失（None/空）时不作判断，交由魔数决定。
    """
    import os

    if not filename:
        return True
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def safe_avatar_filename(user_id: int, ext: str, token: str) -> str:
    """生成服务端控制的文件名：`user_<id>_<随机>.<规范扩展名>`。

    扩展名来自 `detect_image_type` 的结果，**绝不使用客户端输入**，
    从根本上消除 "../"、".html"、双扩展名等路径/类型注入。
    """
    return "user_%s_%s%s" % (user_id, token, ext)


def is_managed_avatar_url(avatar_url: Optional[str], upload_dir: str) -> bool:
    """判断 avatar_url 是否指向由本服务管理的头像文件。

    用于"替换头像时删除旧文件"前的安全检查：只允许删除
    <upload_dir> 下的 `user_*` 文件，避免删到任意路径。
    """
    import os

    if not avatar_url:
        return False
    prefix = "/uploads/avatars/"
    if not avatar_url.startswith(prefix):
        return False
    name = avatar_url[len(prefix):]
    if "/" in name or "\\" in name or name.startswith("."):
        return False
    if not name.startswith("user_"):
        return False
    target = os.path.normpath(os.path.join(upload_dir, name))
    base = os.path.normpath(upload_dir)
    return os.path.dirname(target) == base
