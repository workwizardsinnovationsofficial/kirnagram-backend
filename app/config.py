import os
from dotenv import load_dotenv
import razorpay

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
# Force-load backend .env and override stale shell vars.
load_dotenv(dotenv_path=ENV_PATH, override=True)


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "kirnagram")
APP_ENV = os.getenv("APP_ENV", "dev")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
GEMINI_FALLBACK_MODE = os.getenv("GEMINI_FALLBACK_MODE")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("CHATGPT_API_KEY")
CHATGPT_API_KEY = OPENAI_API_KEY
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")

# OTP + SMS configuration

FAST2SMS_API_KEY = (os.getenv("FAST2SMS_API_KEY") or "").strip()

FAST2SMS_ROUTE = (os.getenv("FAST2SMS_ROUTE", "dlt") or "dlt").strip()

FAST2SMS_SENDER_ID = (os.getenv("FAST2SMS_SENDER_ID") or "").strip()

FAST2SMS_ENTITY_ID = (os.getenv("FAST2SMS_ENTITY_ID") or "").strip()

FAST2SMS_ENTITY_NAME = (os.getenv("FAST2SMS_ENTITY_NAME") or "").strip()

FAST2SMS_TEMPLATE_ID = (os.getenv("FAST2SMS_TEMPLATE_ID") or "").strip()

OTP_HASH_SECRET = (os.getenv("OTP_HASH_SECRET", "change-me-in-env") or "change-me-in-env").strip()

OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "5"))

OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "3"))

OTP_RESEND_COOLDOWN_SECONDS = int(os.getenv("OTP_RESEND_COOLDOWN_SECONDS", "30"))

MOBILE_VERIFICATION_WINDOW_MINUTES = int(
    os.getenv("MOBILE_VERIFICATION_WINDOW_MINUTES", "15")
)

EMAIL_CHANGE_VERIFICATION_WINDOW_MINUTES = int(
    os.getenv("EMAIL_CHANGE_VERIFICATION_WINDOW_MINUTES", "15")
)

OTP_DEV_FALLBACK_ENABLED = _as_bool(
    os.getenv("OTP_DEV_FALLBACK_ENABLED"),
    default=True,
)

# Email configuration
SMTP_SERVER = (os.getenv("SMTP_SERVER") or "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_EMAIL = (os.getenv("SMTP_EMAIL") or os.getenv("SENDER_EMAIL") or "").strip()
SMTP_PASSWORD = (os.getenv("SMTP_PASSWORD") or os.getenv("SENDER_PASSWORD") or "").strip()
EMAIL_OTP_EXPIRY_MINUTES = int(os.getenv("EMAIL_OTP_EXPIRY_MINUTES", "5"))
EMAIL_OTP_RESEND_COOLDOWN_SECONDS = int(os.getenv("EMAIL_OTP_RESEND_COOLDOWN_SECONDS", "30"))

# ✅ Razorpay
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

razorpay_client = razorpay.Client(auth=(
    RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET
))

print("=" * 50)
print("CONFIG ENTITY :", FAST2SMS_ENTITY_ID)
print("CONFIG SENDER :", FAST2SMS_SENDER_ID)
print("CONFIG TEMPLATE :", FAST2SMS_TEMPLATE_ID)
print("=" * 50)