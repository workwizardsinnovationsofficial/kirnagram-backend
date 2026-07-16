from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException

from app.jwt_auth import verify_access_token
from app.models.otp import SendOtpRequest, VerifyOtpRequest
from app.services.otp_service import send_mobile_otp, verify_mobile_otp, normalize_mobile
from app.database import users_collection


router = APIRouter(tags=["OTP"])


def _extract_uid(authorization: str) -> str:
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = parts[1]
    payload = verify_access_token(token)
    uid = payload.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return uid


@router.post("/send-otp")
async def send_otp(payload: SendOtpRequest, authorization: str = Header(...)):
    uid = _extract_uid(authorization)
    mobile = normalize_mobile(payload.mobile)
    force_send = bool(payload.force_send)

    user = await users_collection.find_one({"_id": ObjectId(uid)}, {"mobile": 1, "_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    current_mobile = user.get("mobile")
    if current_mobile and not force_send:
        try:
            if normalize_mobile(current_mobile) == mobile:
                return {
                    "message": "Mobile number is unchanged. OTP verification not required.",
                    "already_verified": True,
                    "mobile": mobile,
                }
        except HTTPException:
            pass

    return await send_mobile_otp(uid, mobile)


@router.post("/verify-otp")
async def verify_otp(payload: VerifyOtpRequest, authorization: str = Header(...)):
    uid = _extract_uid(authorization)
    return await verify_mobile_otp(uid, payload.mobile, payload.otp)
