from datetime import datetime, timedelta
import hashlib
import hmac
import secrets
import re

import httpx
from fastapi import HTTPException

from app.config import (
    APP_ENV,
    FAST2SMS_API_KEY,
    FAST2SMS_ROUTE,
    FAST2SMS_SENDER_ID,
    FAST2SMS_ENTITY_ID,
    FAST2SMS_TEMPLATE_ID,
    OTP_DEV_FALLBACK_ENABLED,
    OTP_EXPIRY_MINUTES,
    OTP_HASH_SECRET,
    OTP_MAX_ATTEMPTS,
    OTP_RESEND_COOLDOWN_SECONDS,
    MOBILE_VERIFICATION_WINDOW_MINUTES,
)
from app.database import otp_collection, users_collection


MOBILE_PATTERN = re.compile(r"^[6-9]\d{9}$")


def normalize_mobile(raw_mobile: str) -> str:
    digits = "".join(ch for ch in (raw_mobile or "") if ch.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]

    if not MOBILE_PATTERN.match(digits):
        raise HTTPException(status_code=400, detail="Enter a valid 10-digit Indian mobile number")
    return digits


def _hash_otp(uid: str, mobile: str, otp: str) -> str:
    payload = f"{uid}|{mobile}|{otp}".encode("utf-8")
    secret = OTP_HASH_SECRET.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def _send_fast2sms(mobile: str, otp: str) -> None:
    if not FAST2SMS_API_KEY:
        raise HTTPException(status_code=500, detail="FAST2SMS_API_KEY is not configured")

    message = f"Your Kirnagram OTP is {otp}. It expires in {OTP_EXPIRY_MINUTES} minutes."
    params = {
        "route": FAST2SMS_ROUTE,
        "sender_id": FAST2SMS_SENDER_ID,
        "numbers": mobile,
    }

    if FAST2SMS_ROUTE == "dlt":
        if FAST2SMS_TEMPLATE_ID:
            params["template_id"] = FAST2SMS_TEMPLATE_ID
            params["variables_values"] = otp
            if FAST2SMS_ENTITY_ID:
                params["entity_id"] = FAST2SMS_ENTITY_ID
            else:
                raise HTTPException(
                    status_code=500,
                    detail="FAST2SMS_ENTITY_ID must be configured when using DLT route",
                )
        else:
            # DLT route requires a template, so fallback is not supported.
            raise HTTPException(
                status_code=500,
                detail="FAST2SMS_TEMPLATE_ID must be configured when using DLT route",
            )
    elif FAST2SMS_TEMPLATE_ID:
        params["template_id"] = FAST2SMS_TEMPLATE_ID
        params["variables_values"] = otp
        if FAST2SMS_ENTITY_ID:
            params["entity_id"] = FAST2SMS_ENTITY_ID
    else:
        params["message"] = message

    headers = {
        "Authorization": FAST2SMS_API_KEY,
        "accept": "application/json",
        "cache-control": "no-cache",
    }

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(
                "https://www.fast2sms.com/dev/bulkV2",
                params=params,
                headers=headers,
            )

        body_text = await response.text()
        body = {}
        try:
            body = response.json() if response.content else {}
        except Exception:
            body = {}

        if response.status_code == 401:
            message = body.get("message") if isinstance(body, dict) else None
            raise HTTPException(
                status_code=502,
                detail=message or f"Fast2SMS unauthorized: invalid or inactive API key ({body_text})",
            )

        if response.status_code != 200:
            message = body.get("message") if isinstance(body, dict) else None
            raise HTTPException(
                status_code=502,
                detail=message or f"Failed to send OTP SMS ({response.status_code}) {body_text}",
            )

        if isinstance(body, dict):
            request_status = str(body.get("return") or "").lower()
            if request_status == "false" or body.get("message") == "invalid api key":
                raise HTTPException(
                    status_code=502,
                    detail=f"SMS gateway rejected OTP request: {body.get('message', body_text)}",
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not send OTP right now. {str(exc)}")


async def send_mobile_otp(uid: str, raw_mobile: str) -> dict:
    mobile = normalize_mobile(raw_mobile)
    now = datetime.utcnow()

    latest = await otp_collection.find_one(
        {"firebase_uid": uid, "mobile": mobile},
        sort=[("created_at", -1)],
    )

    if latest and latest.get("created_at"):
        retry_at = latest["created_at"] + timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)
        if now < retry_at:
            seconds_left = int((retry_at - now).total_seconds())
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {seconds_left} seconds before requesting another OTP",
            )

    otp = _generate_otp()
    expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)

    sms_channel = "fast2sms"
    sms_sent = True
    sms_error = None
    try:
        await _send_fast2sms(mobile, otp)
    except Exception as exc:
        sms_sent = False
        if isinstance(exc, HTTPException):
            sms_error = str(exc.detail)
        else:
            sms_error = str(exc)

    is_non_prod = APP_ENV.lower() != "prod"
    if not sms_sent and not (is_non_prod and OTP_DEV_FALLBACK_ENABLED):
        # Never expose OTP fallback in production/deployed environments.
        raise HTTPException(status_code=502, detail=sms_error or "SMS service unavailable")

    await otp_collection.update_many(
        {"firebase_uid": uid, "mobile": mobile, "used": False},
        {"$set": {"used": True, "invalidated_at": now, "updated_at": now}},
    )

    doc = {
        "firebase_uid": uid,
        "mobile": mobile,
        "otp_hash": _hash_otp(uid, mobile, otp),
        "expires_at": expires_at,
        "used": False,
        "verified": False,
        "attempts": 0,
        "max_attempts": OTP_MAX_ATTEMPTS,
        "created_at": now,
        "updated_at": now,
    }
    await otp_collection.insert_one(doc)

    if not sms_sent:
        print(f"[DEV OTP FALLBACK] user={uid} mobile={mobile} otp={otp}")
        return {
            "success": True,
            "dev_mode": True,
            "message": "SMS blocked (IP issue), using a fallback OTP in development mode",
            "mobile": mobile,
            "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
            "channel": "dev-fallback",
            "warning": sms_error,
        }

    result = {
        "message": "OTP sent successfully",
        "mobile": mobile,
        "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
        "channel": sms_channel,
    }
    return result


async def verify_mobile_otp(uid: str, raw_mobile: str, otp: str) -> dict:
    mobile = normalize_mobile(raw_mobile)
    now = datetime.utcnow()

    record = await otp_collection.find_one(
        {"firebase_uid": uid, "mobile": mobile},
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

    submitted_hash = _hash_otp(uid, mobile, otp)
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
                "used": True,
                "verified": True,
                "verified_at": now,
                "updated_at": now,
            }
        },
    )

    user = await users_collection.find_one({"firebase_uid": uid})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    current_mobile = user.get("mobile")
    if current_mobile and current_mobile != mobile:
        mobile_change_verified = user.get("mobile_change_verified") or {}
        verified_mobile = mobile_change_verified.get("mobile")
        verified_at = mobile_change_verified.get("verified_at")

        if verified_mobile != current_mobile:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "MOBILE_CHANGE_OTP_REQUIRED",
                    "message": "Please verify your current mobile number before updating to a new number",
                },
            )

        if not isinstance(verified_at, datetime) or now - verified_at > timedelta(minutes=MOBILE_VERIFICATION_WINDOW_MINUTES):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "MOBILE_CHANGE_OTP_EXPIRED",
                    "message": "Current mobile verification expired. Please verify your current number again.",
                },
            )

    await users_collection.update_one(
        {"firebase_uid": uid},
        {
            "$set": {
                "mobile": mobile,
                "mobile_verified_mobile": mobile,
                "mobile_verified_at": now,
                "mobile_change_verified": {
                    "mobile": mobile,
                    "verified_at": now,
                },
                "updated_at": now,
            }
        },
    )

    return {
        "message": "OTP verified successfully. Mobile number saved.",
        "mobile": mobile,
        "mobile_saved": True,
    }
