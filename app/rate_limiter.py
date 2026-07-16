from datetime import datetime, timedelta
from typing import Dict, Tuple
from app.database import db
from fastapi import HTTPException


class RateLimiter:
    """
    Rate limiter for failed login attempts
    5 failed attempts = 3 hour lockout
    """
    
    MAX_ATTEMPTS = 5
    LOCKOUT_DURATION_HOURS = 3
    ATTEMPT_WINDOW_MINUTES = 60  # Reset attempts after 1 hour of no attempts
    
    @staticmethod
    async def check_rate_limit(identifier: str, identifier_type: str = "email") -> Dict:
        """
        Check if user is rate limited
        identifier_type: "email" or "mobile"
        Returns: {"allowed": bool, "message": str, "locked_until": datetime or None}
        """
        
        rate_limit_key = f"{identifier_type}:{identifier}"
        
        rate_limit_doc = await db.rate_limits.find_one({"_id": rate_limit_key})
        
        if not rate_limit_doc:
            return {"allowed": True, "message": "No prior attempts", "locked_until": None}
        
        now = datetime.utcnow()
        last_attempt = rate_limit_doc.get("last_attempt")
        attempts = rate_limit_doc.get("attempts", 0)
        locked_until = rate_limit_doc.get("locked_until")
        
        # Check if currently locked
        if locked_until and now < locked_until:
            remaining = locked_until - now
            return {
                "allowed": False,
                "message": f"Too many failed attempts. Try again in {remaining.total_seconds() // 60:.0f} minutes",
                "locked_until": locked_until
            }
        
        # Check if attempts window has expired (reset if > 1 hour since last attempt)
        if last_attempt and (now - last_attempt) > timedelta(minutes=RateLimiter.ATTEMPT_WINDOW_MINUTES):
            # Reset attempts
            await db.rate_limits.update_one(
                {"_id": rate_limit_key},
                {"$set": {"attempts": 0, "locked_until": None}}
            )
            return {"allowed": True, "message": "Attempts reset", "locked_until": None}
        
        return {"allowed": True, "message": "Within limits", "locked_until": None}
    
    @staticmethod
    async def record_failed_attempt(identifier: str, identifier_type: str = "email") -> Dict:
        """
        Record a failed login attempt
        If 5 attempts reached, lock the account for 3 hours
        """
        
        rate_limit_key = f"{identifier_type}:{identifier}"
        now = datetime.utcnow()
        
        # Get current document
        rate_limit_doc = await db.rate_limits.find_one({"_id": rate_limit_key})
        
        if not rate_limit_doc:
            # First attempt
            await db.rate_limits.insert_one({
                "_id": rate_limit_key,
                "attempts": 1,
                "last_attempt": now,
                "locked_until": None,
                "created_at": now
            })
            return {
                "success": True,
                "attempts": 1,
                "max_attempts": RateLimiter.MAX_ATTEMPTS,
                "locked": False,
                "message": f"Attempt 1/{RateLimiter.MAX_ATTEMPTS}"
            }
        
        # Check if we should reset attempts (window expired)
        last_attempt = rate_limit_doc.get("last_attempt")
        attempts = rate_limit_doc.get("attempts", 0)
        
        if last_attempt and (now - last_attempt) > timedelta(minutes=RateLimiter.ATTEMPT_WINDOW_MINUTES):
            # Window expired, reset
            attempts = 0
        
        # Increment attempts
        new_attempts = attempts + 1
        locked_until = None
        
        if new_attempts >= RateLimiter.MAX_ATTEMPTS:
            locked_until = now + timedelta(hours=RateLimiter.LOCKOUT_DURATION_HOURS)
        
        await db.rate_limits.update_one(
            {"_id": rate_limit_key},
            {
                "$set": {
                    "attempts": new_attempts,
                    "last_attempt": now,
                    "locked_until": locked_until
                }
            }
        )
        
        return {
            "success": True,
            "attempts": new_attempts,
            "max_attempts": RateLimiter.MAX_ATTEMPTS,
            "locked": locked_until is not None,
            "locked_until": locked_until,
            "message": f"Failed attempt {new_attempts}/{RateLimiter.MAX_ATTEMPTS}. {'Account locked for 3 hours!' if locked_until else ''}"
        }
    
    @staticmethod
    async def clear_rate_limit(identifier: str, identifier_type: str = "email"):
        """Clear rate limit for successful login"""
        rate_limit_key = f"{identifier_type}:{identifier}"
        await db.rate_limits.delete_one({"_id": rate_limit_key})
