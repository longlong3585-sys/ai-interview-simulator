import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("请在 .env 文件中设置 DEEPSEEK_API_KEY")

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

CAPTCHA_TTL = 300
CAPTCHA_MAX_ERRORS = 5
CAPTCHA_LOCK_MINUTES = 10

MAX_FILE_SIZE = 5 * 1024 * 1024

UPLOAD_DIR = "uploads/avatars"
QUESTION_BANK_FILE = os.path.join(os.path.dirname(__file__), "question_bank.json")
