from pydantic import BaseModel, validator
import re


class SendOtpRequest(BaseModel):
    mobile: str
    force_send: bool = False

    @validator("mobile")
    def validate_mobile(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Mobile number is required")
        return cleaned


class VerifyOtpRequest(BaseModel):
    mobile: str
    otp: str

    @validator("mobile")
    def validate_mobile(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Mobile number is required")
        return cleaned

    @validator("otp")
    def validate_otp(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) != 6 or not cleaned.isdigit():
            raise ValueError("OTP must be a 6-digit number")
        return cleaned


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SendEmailOtpRequest(BaseModel):
    email: str

    @validator("email")
    def validate_email(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if not cleaned:
            raise ValueError("Email is required")
        if not EMAIL_PATTERN.match(cleaned):
            raise ValueError("Enter a valid email address")
        return cleaned


class VerifyEmailOtpRequest(BaseModel):
    email: str
    otp: str

    @validator("email")
    def validate_email(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if not cleaned:
            raise ValueError("Email is required")
        if not EMAIL_PATTERN.match(cleaned):
            raise ValueError("Enter a valid email address")
        return cleaned

    @validator("otp")
    def validate_otp(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) != 6 or not cleaned.isdigit():
            raise ValueError("OTP must be a 6-digit number")
        return cleaned
