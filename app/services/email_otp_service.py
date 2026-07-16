import hashlib
import hmac
import re
import secrets
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import HTTPException

from app.config import (
    EMAIL_OTP_EXPIRY_MINUTES,
    EMAIL_OTP_RESEND_COOLDOWN_SECONDS,
    OTP_HASH_SECRET,
    OTP_MAX_ATTEMPTS,
    SMTP_SERVER,
    SMTP_PORT,
    SMTP_EMAIL,
    SMTP_PASSWORD,
)
from app.database import otp_collection


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(raw_email: str) -> str:
    email = (raw_email or "").strip().lower()
    if not email or not EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    return email


def _hash_otp(email: str, otp: str) -> str:
    payload = f"{email}|{otp}".encode("utf-8")
    secret = OTP_HASH_SECRET.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _send_email_otp(to_email: str, otp: str) -> None:
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        raise HTTPException(status_code=500, detail="SMTP credentials are not configured")

    subject = "Kirnagram Email Verification OTP"
    body = (
        f"Your Kirnagram OTP is {otp}. "
        f"It expires in {EMAIL_OTP_EXPIRY_MINUTES} minutes.\n\n"
        "If you did not request this OTP, please ignore this email."
    )

    message = MIMEMultipart()
    message["From"] = SMTP_EMAIL
    message["To"] = to_email
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
                server.login(SMTP_EMAIL, SMTP_PASSWORD)
                server.sendmail(SMTP_EMAIL, to_email, message.as_string())
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_EMAIL, SMTP_PASSWORD)
                server.sendmail(SMTP_EMAIL, to_email, message.as_string())
    except Exception as exc:
        print(f"Email OTP error: {exc}")
        raise HTTPException(status_code=502, detail="Could not send OTP email. Please retry.")


async def send_email_otp(email: str, channel: str = "email-signup") -> dict:
    normalized_email = normalize_email(email)
    now = datetime.utcnow()

    latest = await otp_collection.find_one(
        {"email": normalized_email, "channel": channel},
        sort=[("created_at", -1)],
    )

    if latest and latest.get("created_at"):
        retry_at = latest["created_at"] + timedelta(seconds=EMAIL_OTP_RESEND_COOLDOWN_SECONDS)
        if now < retry_at:
            seconds_left = int((retry_at - now).total_seconds())
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {seconds_left} seconds before requesting another OTP",
            )

    otp = _generate_otp()
    expires_at = now + timedelta(minutes=EMAIL_OTP_EXPIRY_MINUTES)

    _send_email_otp(normalized_email, otp)

    await otp_collection.update_many(
        {"email": normalized_email, "channel": "email-signup", "used": False},
        {"$set": {"used": True, "invalidated_at": now, "updated_at": now}},
    )

    await otp_collection.insert_one(
        {
            "email": normalized_email,
            "channel": channel,
            "otp_hash": _hash_otp(normalized_email, otp),
            "expires_at": expires_at,
            "used": False,
            "verified": False,
            "attempts": 0,
            "max_attempts": OTP_MAX_ATTEMPTS,
            "created_at": now,
            "updated_at": now,
        }
    )

    return {
        "success": True,
        "message": "OTP sent to your email",
        "email": normalized_email,
        "expires_in_seconds": EMAIL_OTP_EXPIRY_MINUTES * 60,
    }


async def verify_email_otp(email: str, otp: str, channel: str = "email-signup") -> dict:
    normalized_email = normalize_email(email)
    now = datetime.utcnow()

    record = await otp_collection.find_one(
        {"email": normalized_email, "channel": channel},
        sort=[("created_at", -1)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="OTP not found. Please request a new OTP")

    if record.get("used"):
        raise HTTPException(status_code=400, detail="OTP already used. Request a new OTP")

    expires_at = record.get("expires_at")
    if not expires_at or now > expires_at:
        await otp_collection.update_one(
            {"_id": record["_id"]},
            {"$set": {"used": True, "updated_at": now}},
        )
        raise HTTPException(status_code=400, detail="OTP expired. Please request a new OTP")

    attempts = int(record.get("attempts", 0))
    max_attempts = int(record.get("max_attempts", OTP_MAX_ATTEMPTS))
    if attempts >= max_attempts:
        await otp_collection.update_one(
            {"_id": record["_id"]},
            {"$set": {"used": True, "updated_at": now}},
        )
        raise HTTPException(status_code=400, detail="Maximum OTP attempts exceeded")

    submitted_hash = _hash_otp(normalized_email, otp)
    if submitted_hash != record.get("otp_hash"):
        attempts += 1
        updates = {"attempts": attempts, "updated_at": now}
        if attempts >= max_attempts:
            updates["used"] = True

        await otp_collection.update_one({"_id": record["_id"]}, {"$set": updates})

        remaining = max(max_attempts - attempts, 0)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_OTP",
                "message": "Invalid OTP",
                "remaining_attempts": remaining,
            },
        )

    await otp_collection.update_one(
        {"_id": record["_id"]},
        {
            "$set": {
                "verified": True,
                "verified_at": now,
                "updated_at": now,
            }
        },
    )

    return {
        "success": True,
        "message": "Email verified successfully",
        "email": normalized_email,
    }


async def send_profile_email_otp(email: str) -> dict:
    return await send_email_otp(email, channel="email-change")


async def verify_profile_email_otp(email: str, otp: str) -> dict:
    return await verify_email_otp(email, otp, channel="email-change")


async def consume_verified_email_for_signup(email: str) -> None:
    normalized_email = normalize_email(email)
    now = datetime.utcnow()

    record = await otp_collection.find_one(
        {
            "email": normalized_email,
            "channel": "email-signup",
            "verified": True,
            "used": False,
        },
        sort=[("created_at", -1)],
    )

    if not record:
        raise HTTPException(status_code=400, detail="Please verify your email OTP before creating account")

    expires_at = record.get("expires_at")
    if not expires_at or now > expires_at:
        await otp_collection.update_one(
            {"_id": record["_id"]},
            {"$set": {"used": True, "updated_at": now}},
        )
        raise HTTPException(status_code=400, detail="Email OTP expired. Please verify again")

    await otp_collection.update_one(
        {"_id": record["_id"]},
        {
            "$set": {
                "used": True,
                "consumed_for_signup": True,
                "consumed_at": now,
                "updated_at": now,
            }
        },
    )
