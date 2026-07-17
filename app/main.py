from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from app.routes.auth_new import router as auth_router
from app.routes.profile import router as profile_router
from app.routes.upload import router as upload_router
from app.routes.notification import router as notification_router
from app.routes.follow import router as follow_router
from app.routes.story import router as story_router
from app.routes.story import posts_router
from app.routes.admin.ai_creator import admin_router, user_router
from app.routes.admin.credits import admin_router as credits_admin_router
from app.routes.credits import router as credits_router
from app.routes.payment_history import router as payment_history_router
from app.routes.remix import router as remix_router
from app.routes import post
from app.routes.two_factor import router as two_factor_router
from app.routes.admin.dashboard import router as admin_dashboard_router
from app.routes.withdraw import router as withdraw_router
from app.routes.ads import user_router as ads_user_router, admin_router as ads_admin_router
from app.routes.otp import router as otp_router
from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header
from bson.objectid import ObjectId


import os
from dotenv import load_dotenv
load_dotenv()
app = FastAPI(title="kirnagram Backend")

# ✅ SECURITY HEADERS MIDDLEWARE
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Allow cross-origin window operations for Google OAuth
        # Use "same-origin-allow-popups" to allow popup windows from same origin
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        # Don't set COEP to avoid breaking OAuth flow
        return response


class UserActivityTrackingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if request.method == "OPTIONS":
            return response

        path = request.url.path or ""
        if path.startswith("/admin") or path in {"/", "/docs", "/openapi.json", "/redoc"}:
            return response

        authorization = request.headers.get("authorization")
        if not authorization or " " not in authorization:
            return response

        try:
            token = extract_token_from_header(authorization)
            payload = verify_access_token(token)
            user_id = payload.get("sub")
            
            if not user_id:
                return response

            user = await db.users.find_one(
                {"_id": ObjectId(user_id)},
                {
                    "_id": 1,
                    "full_name": 1,
                    "username": 1,
                    "email": 1,
                    "mobile": 1,
                    "account_type": 1,
                },
            )
            if not user:
                return response

            from datetime import datetime

            now = datetime.utcnow()
            day_start = datetime(now.year, now.month, now.day)
            day_key = day_start.strftime("%Y-%m-%d")

            await db.user_daily_activity.update_one(
                {"user_id": user_id, "date_key": day_key},
                {
                    "$setOnInsert": {
                        "user_id": user_id,
                        "date": day_start,
                        "date_key": day_key,
                        "first_seen_at": now,
                        "created_at": now,
                        "full_name": user.get("full_name") or "",
                        "username": user.get("username") or "",
                        "email": user.get("email") or "",
                        "mobile": user.get("mobile") or "",
                        "account_type": user.get("account_type") or "public",
                    },
                    "$set": {
                        "last_seen_at": now,
                        "updated_at": now,
                        "last_path": path,
                        "full_name": user.get("full_name") or "",
                        "username": user.get("username") or "",
                        "email": user.get("email") or "",
                        "mobile": user.get("mobile") or "",
                        "account_type": user.get("account_type") or "public",
                    },
                    "$inc": {"hit_count": 1},
                },
                upsert=True,
            )
        except Exception:
            # Activity tracking should never break API responses.
            pass

        return response

# Add security headers middleware
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(UserActivityTrackingMiddleware)

# ✅ CORS CONFIGURATION (VERY IMPORTANT)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.kirnagram.com",
        "http://localhost:8080",
        "http://localhost:3000",
        "http://localhost:3006",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3006",
        "http://10.181.211.98:3000"
        "",

        
    ],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],   # allows OPTIONS, POST, GET, etc.
    allow_headers=["*"],
)

# Routes
app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(otp_router)
app.include_router(upload_router)
app.include_router(notification_router)
app.include_router(follow_router)

from app.routes.admin.withdraw import admin_router as withdraw_admin_router

app.include_router(story_router)
app.include_router(admin_router)
app.include_router(user_router)
app.include_router(credits_admin_router)
app.include_router(credits_router)
app.include_router(payment_history_router)
app.include_router(remix_router)
app.include_router(two_factor_router)
app.include_router(post.router)
app.include_router(withdraw_admin_router)
app.include_router(withdraw_router)
app.include_router(admin_dashboard_router)
app.include_router(posts_router)
app.include_router(ads_user_router)
app.include_router(ads_admin_router)


@app.on_event("startup")
async def ensure_indexes():
    # Optimized lookup for latest OTP record by user and mobile.
    await db.otp_verifications.create_index([
        ("firebase_uid", 1),
        ("mobile", 1),
        ("created_at", -1),
    ])
    # Ensure public_id uniqueness for users (k0001 style IDs)
    await db.users.create_index("public_id", unique=True, sparse=True)
    await db.otp_verifications.create_index([
        ("email", 1),
        ("channel", 1),
        ("created_at", -1),
    ])
    # Auto-clean expired OTP documents.
    await db.otp_verifications.create_index("expires_at", expireAfterSeconds=0)



@app.get("/")
def root():
    return {"status": "Backend running"}