from fastapi import APIRouter, Header, HTTPException
from datetime import datetime, timedelta
from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header, get_user_id_from_authorization_header
from app.firebase import verify_firebase_token
from app.config import SMTP_SERVER, SMTP_PORT, SMTP_EMAIL, SMTP_PASSWORD
import random
import smtplib
from email.mime.text import MIMEText

router = APIRouter(prefix="/2fa", tags=["Two Factor"])


# 🔐 Generate OTP
def generate_otp():
    return str(random.randint(100000, 999999))


# 📧 Send Email OTP
def send_email_otp(to_email: str, otp: str):
    msg = MIMEText(f"Your Kiranagram OTP is: {otp}")
    msg["Subject"] = "Kiranagram Two Factor Verification"
    msg["From"] = SMTP_EMAIL
    msg["To"] = to_email

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())


# 🚀 Send OTP (used for enable & disable)
@router.post("/request")
async def request_otp(authorization: str = Header(...)):
    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)

    uid = decoded["uid"]
    email = decoded["email"]

    otp = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=5)

    await db.two_factor_codes.delete_many({"user_id": uid})

    await db.two_factor_codes.insert_one({
        "user_id": uid,
        "otp": otp,
        "expires_at": expires_at
    })

    send_email_otp(email, otp)

    return {"success": True}

@router.post("/verify")
async def verify_otp(payload: dict, authorization: str = Header(...)):
    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)

    uid = decoded["uid"]
    otp_input = payload.get("otp")

    record = await db.two_factor_codes.find_one({
        "user_id": uid,
        "otp": otp_input
    })

    if not record:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    if record["expires_at"] < datetime.utcnow():
        raise HTTPException(status_code=400, detail="OTP expired")

    # 🔥 THIS IS IMPORTANT
    result = await db.users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"two_factor_enabled": True}}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="User not found in users collection")

    return {"success": True}


# 🔴 Disable 2FA
@router.post("/disable")
async def disable_2fa(payload: dict, authorization: str = Header(...)):
    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)

    uid = decoded["uid"]
    otp_input = payload.get("otp")

    record = await db.two_factor_codes.find_one({
        "user_id": uid,
        "otp": otp_input
    })

    if not record:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    if record["expires_at"] < datetime.utcnow():
        raise HTTPException(status_code=400, detail="OTP expired")

    await db.users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"two_factor_enabled": False}}
    )

    return {"success": True, "message": "2FA Disabled"}

@router.post("/login-request")
async def login_request_otp(authorization: str = Header(...)):
    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)

    uid = decoded["uid"]
    email = decoded["email"]

    # 🔍 Check if user exists
    user = await db.users.find_one({"_id": ObjectId(uid)})

    # 🆕 Auto create user if not exists
    if not user:
        user_data = {
            "firebase_uid": uid,
            "email": email,
            "two_factor_enabled": False,
            "created_at": datetime.utcnow()
        }
        await db.users.insert_one(user_data)
        return {"two_factor_required": False}

    # ✅ If 2FA NOT enabled → allow login
    if not user.get("two_factor_enabled", False):
        return {"two_factor_required": False}

    # 🔥 If 2FA enabled → Generate OTP
    otp = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=5)

    await db.two_factor_codes.delete_many({"user_id": uid})

    await db.two_factor_codes.insert_one({
        "user_id": uid,
        "otp": otp,
        "expires_at": expires_at
    })

    send_email_otp(email, otp)

    return {
        "two_factor_required": True,
        "message": "OTP sent"
    }

@router.post("/login-verify")
async def login_verify_otp(payload: dict, authorization: str = Header(...)):
    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)

    uid = decoded["uid"]
    otp_input = payload.get("otp")

    if not otp_input:
        raise HTTPException(status_code=400, detail="OTP required")

    record = await db.two_factor_codes.find_one({
        "user_id": uid,
        "otp": otp_input
    })

    if not record:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    if record["expires_at"] < datetime.utcnow():
        raise HTTPException(status_code=400, detail="OTP expired")

    # 🧹 Delete OTP after success
    await db.two_factor_codes.delete_many({"user_id": uid})

    return {"success": True}
@router.post("/sync-user")
async def sync_user(authorization: str = Header(...)):
    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)

    uid = decoded["uid"]
    email = decoded.get("email")

    user = await db.users.find_one({"_id": ObjectId(uid)})

    if not user:
        await db.users.insert_one({
            "firebase_uid": uid,
            "email": email,
            "two_factor_enabled": False,
            "created_at": datetime.utcnow()
        })

    return {"success": True}
