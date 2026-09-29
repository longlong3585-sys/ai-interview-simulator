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

# T-28 / FR-4.12：**面试时长上限（秒）**。
#
# 为什么要有这个服务端常量：修复前"15 分钟归零"只活在前端的一个
# `setInterval` 里（`App.tsx: setTimeLeft(15 * 60)`）。前端一旦被绕过
# （手工发请求、脚本刷接口、标签页被挂起），面试就能无限期继续下去 ——
# FR-4.12 的业务规则形同虚设。超时时刻必须由**服务端自己掌握**：
# 死线 = 会话行里的 `created_at`（服务端写入，客户端无法伪造）+ 本常量。
#
# 为什么**不**复用 `expires_at`（2h TTL）：那是 ADR-022 的**锁卫生**上限，
# 与"面试能答多久"是两个语义。把两者压成一个字段会二选一地制造事故：
# 要么允许用户答 2 小时，要么让一次刷新/断网就把 15 分钟的面试作废。
#
# 之所以做成环境变量：验收脚本（`scripts/verify_t28_manual.py`）需要把
# 15 分钟压成几秒，否则"人工验收"就得真的等一刻钟。
_INTERVIEW_DURATION_RAW = os.getenv("INTERVIEW_DURATION_SECONDS")
try:
    INTERVIEW_DURATION_SECONDS = (int(_INTERVIEW_DURATION_RAW)
                                 if _INTERVIEW_DURATION_RAW
                                 else 15 * 60)
except (TypeError, ValueError):
    raise ValueError(
        "INTERVIEW_DURATION_SECONDS 必须是整数秒，当前为 %r" % _INTERVIEW_DURATION_RAW
    )
if INTERVIEW_DURATION_SECONDS <= 0:
    raise ValueError(
        "INTERVIEW_DURATION_SECONDS 必须为正数（否则每场面试一开始就是超时）：%d"
        % INTERVIEW_DURATION_SECONDS
    )

MAX_FILE_SIZE = 5 * 1024 * 1024

# T-09 / FR-10.4：头像后端大小上限，与前端 canvas 裁剪后提交的 2MB 限制保持一致
MAX_AVATAR_SIZE = 2 * 1024 * 1024

UPLOAD_DIR = "uploads/avatars"
QUESTION_BANK_FILE = os.path.join(os.path.dirname(__file__), "question_bank.json")
