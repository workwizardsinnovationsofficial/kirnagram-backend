from typing import Any, Dict

from bson import ObjectId
from fastapi import HTTPException

from app.database import db
from app.jwt_auth import extract_token_from_header, verify_access_token


def _normalize_user_query(user_id: str) -> Dict[str, Any]:
    """Build a MongoDB query for a user identifier."""
    query_parts = []

    if ObjectId.is_valid(user_id):
        query_parts.append({"_id": ObjectId(user_id)})

    query_parts.append({"firebase_uid": user_id})
    query_parts.append({"public_id": user_id})

    return {"$or": query_parts} if len(query_parts) > 1 else query_parts[0]


async def get_current_user(authorization: str) -> Dict[str, Any]:
    """Verify the bearer token and return the current user document."""
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = extract_token_from_header(authorization)
    payload = verify_access_token(token)
    user_id = payload.get("sub")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    query = _normalize_user_query(user_id)
    user = await db.users.find_one(query)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user


async def get_current_user_id(authorization: str) -> str:
    """Return the current user's MongoDB ID as a string."""
    user = await get_current_user(authorization)
    return str(user["_id"])
