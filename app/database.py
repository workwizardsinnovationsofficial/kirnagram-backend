from motor.motor_asyncio import AsyncIOMotorClient
from app.config import MONGO_URI, DB_NAME

client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]

# 🔥 Explicit Collections (IMPORTANT)
users_collection = db["users"]
posts_collection = db["posts"]
follows_collection = db["follows"]
notifications_collection = db["notifications"]

withdraw_requests_collection = db["withdraw_requests"]
settings_collection = db["settings"]
publisher_applications_collection = db["publisher_applications"]
otp_collection = db["otp_verifications"]
