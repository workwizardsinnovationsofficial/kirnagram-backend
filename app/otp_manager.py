import secrets
import string
from datetime import datetime, timedelta
from typing import Dict
from app.database import db


class OTPManager:
    """Manages OTP generation and verification"""
    
    OTP_LENGTH = 6
    OTP_VALIDITY_MINUTES = 10
    
    @staticmethod
    def generate_otp() -> str:
        """Generate a 6-digit OTP"""
        return ''.join(secrets.choice(string.digits) for _ in range(OTPManager.OTP_LENGTH))
    
    @staticmethod
    async def create_otp(identifier: str, identifier_type: str = "email") -> Dict:
        """
        Create and store OTP
        identifier_type: "email" or "mobile"
        """
        otp = OTPManager.generate_otp()
        now = datetime.utcnow()
        expires_at = now + timedelta(minutes=OTPManager.OTP_VALIDITY_MINUTES)
        
        otp_key = f"{identifier_type}:{identifier}"
        
        await db.otps.update_one(
            {"_id": otp_key},
            {
                "$set": {
                    "otp": otp,
                    "created_at": now,
                    "expires_at": expires_at,
                    "verified": False,
                    "attempts": 0
                }
            },
            upsert=True
        )
        
        return {
            "otp": otp,
            "expires_at": expires_at,
            "validity_minutes": OTPManager.OTP_VALIDITY_MINUTES
        }
    
    @staticmethod
    async def verify_otp(identifier: str, otp: str, identifier_type: str = "email") -> bool:
        """Verify OTP"""
        otp_key = f"{identifier_type}:{identifier}"
        
        otp_doc = await db.otps.find_one({"_id": otp_key})
        
        if not otp_doc:
            return False
        
        now = datetime.utcnow()
        
        # Check if OTP has expired
        if now > otp_doc.get("expires_at", now):
            await db.otps.delete_one({"_id": otp_key})
            return False
        
        # Check if already verified
        if otp_doc.get("verified"):
            return False
        
        # Check OTP value
        if otp_doc.get("otp") != otp:
            # Increment attempts
            attempts = otp_doc.get("attempts", 0) + 1
            if attempts >= 3:
                # Delete after 3 failed attempts
                await db.otps.delete_one({"_id": otp_key})
            else:
                await db.otps.update_one(
                    {"_id": otp_key},
                    {"$set": {"attempts": attempts}}
                )
            return False
        
        # Mark as verified
        await db.otps.update_one(
            {"_id": otp_key},
            {"$set": {"verified": True}}
        )
        
        return True
    
    @staticmethod
    async def mark_otp_as_used(identifier: str, identifier_type: str = "email"):
        """Mark OTP as used and clean up"""
        otp_key = f"{identifier_type}:{identifier}"
        await db.otps.delete_one({"_id": otp_key})
    
    @staticmethod
    async def get_otp_status(identifier: str, identifier_type: str = "email") -> Dict:
        """Get OTP status"""
        otp_key = f"{identifier_type}:{identifier}"
        
        otp_doc = await db.otps.find_one({"_id": otp_key})
        
        if not otp_doc:
            return {"exists": False}
        
        now = datetime.utcnow()
        expires_at = otp_doc.get("expires_at", now)
        
        return {
            "exists": True,
            "verified": otp_doc.get("verified", False),
            "attempts": otp_doc.get("attempts", 0),
            "expires_at": expires_at,
            "expired": now > expires_at
        }
