import os
from dotenv import load_dotenv

# T-07 补强：允许用 APP_ENV_FILE 指定 .env 路径。
#
# 动机：原来的写法把路径写死，导致"真实 .env 文件里漏配 SECRET_KEY"这一
# **生产最可能发生的场景**无法被测试覆盖（单元测试只能 mock 掉 load_dotenv，
# 而那恰好绕过了真实文件路径 —— 也正因如此，我第一次给出的手工验证指令是错的：
# PowerShell 的 $env:SECRET_KEY="" 实际是【删除】变量，load_dotenv 随后又从
# .env 把值补了回来，根本没进入"缺失"分支）。
#
# 不设置该变量时行为与之前完全一致。
_ENV_FILE = os.getenv("APP_ENV_FILE") or os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_ENV_FILE)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("请在 .env 文件中设置 DEEPSEEK_API_KEY")

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

# T-07 / NFR-1(c)：SECRET_KEY 缺失时**启动即失败**。
#
# 修复前：SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
#   —— 漏配 .env 时会**静默**使用这个公开可知的默认值签发/校验 JWT，
#      任何人都能自行伪造任意用户的令牌（含 admin），且没有任何告警。
#   对照：同文件对 DEEPSEEK_API_KEY 缺失就是直接 raise，此处保持一致。
#
# 额外拒绝两类弱值：
#   1. 文档/示例里的占位符（照抄 .env.example 或 README 的写法）
#   2. 过短的密钥（HS256 建议 ≥256 bit，即 32 字符）
_INSECURE_SECRET_KEY_PLACEHOLDERS = {
    "dev-secret-change-in-production",
    "change-this-to-a-random-string-at-least-64-chars",
    "your-random-secret-key-at-least-64-chars",
}
_MIN_SECRET_KEY_LENGTH = 32

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise ValueError(
        "请在 .env 文件中设置 SECRET_KEY。"
        "该值不得缺失或使用默认值 —— 否则 JWT 可被任意伪造。"
    )
if SECRET_KEY in _INSECURE_SECRET_KEY_PLACEHOLDERS:
    raise ValueError(
        "SECRET_KEY 仍是文档中的占位符，必须替换为随机值"
        "（可用 `python -c \"import secrets;print(secrets.token_urlsafe(64))\"` 生成）。"
    )
if len(SECRET_KEY) < _MIN_SECRET_KEY_LENGTH:
    raise ValueError(
        "SECRET_KEY 过短（当前 %d 字符，至少需要 %d 字符）。"
        "HS256 建议使用 ≥256 bit 的随机密钥。" % (len(SECRET_KEY), _MIN_SECRET_KEY_LENGTH)
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

CAPTCHA_TTL = 300
CAPTCHA_MAX_ERRORS = 5
CAPTCHA_LOCK_MINUTES = 10

MAX_FILE_SIZE = 5 * 1024 * 1024

# T-09 / FR-10.4：头像后端大小上限，与前端 canvas 裁剪后提交的 2MB 限制保持一致
MAX_AVATAR_SIZE = 2 * 1024 * 1024

UPLOAD_DIR = "uploads/avatars"
QUESTION_BANK_FILE = os.path.join(os.path.dirname(__file__), "question_bank.json")
