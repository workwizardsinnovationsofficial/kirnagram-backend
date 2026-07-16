import asyncio
from app.database import db
from app.jwt_auth import create_session_tokens, verify_access_token
from app.password_utils import hash_password
from bson import ObjectId
from datetime import datetime
import random

async def test_auth_flow():
    print("\n=== Testing Complete Auth Flow ===\n")
    
    # Step 1: Create a test user in DB
    print("1. Creating test user in database...")
    random_id = random.randint(9000, 9999)
    user_doc = {
        "public_id": f"k{random_id}",
        "firebase_uid": None,
        "full_name": "Test User",
        "email": f"test{random_id}@kirnagram.com",
        "mobile": "9999999999",
        "password_hash": hash_password("TestPassword123"),
        "auth_type": "manual",
        "account_type": "public",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True,
        "two_factor_enabled": False
    }
    
    # Delete if exists
    await db.users.delete_one({"email": user_doc["email"]})
    
    result = await db.users.insert_one(user_doc)
    user_id = str(result.inserted_id)
    print(f"✅ User created with _id: {user_id}")
    
    # Update firebase_uid
    await db.users.update_one(
        {"_id": result.inserted_id},
        {"$set": {"firebase_uid": user_id}}
    )
    
    # Step 2: Create JWT tokens
    print("\n2. Creating JWT tokens...")
    tokens = create_session_tokens(user_id, email=f"test{random_id}@kirnagram.com")
    access_token = tokens['access_token']
    print(f"✅ Token created: {access_token[:50]}...")
    
    # Step 3: Verify token
    print("\n3. Verifying JWT token...")
    payload = verify_access_token(access_token)
    extracted_user_id = payload.get("sub")
    print(f"✅ Token verified, extracted user_id: {extracted_user_id}")
    print(f"   Matches original: {extracted_user_id == user_id}")
    
    # Step 4: Query user by extracted ID
    print("\n4. Querying user by extracted _id...")
    user_from_db = await db.users.find_one({"_id": ObjectId(extracted_user_id)})
    if user_from_db:
        print(f"✅ User found in DB")
        print(f"   Email: {user_from_db.get('email')}")
        print(f"   Full name: {user_from_db.get('full_name')}")
    else:
        print(f"❌ User NOT found in DB")
    
    print("\n=== Test Complete ===\n")
    print(f"Frontend should store in localStorage:")
    print(f"  access_token: {access_token[:30]}...")
    print(f"  user_id: {user_id}")

asyncio.run(test_auth_flow())
