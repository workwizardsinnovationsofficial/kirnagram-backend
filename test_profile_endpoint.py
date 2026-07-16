import asyncio
import httpx
from app.jwt_auth import create_session_tokens
from app.database import db
from app.password_utils import hash_password
from bson import ObjectId
from datetime import datetime
import random

async def test_profile_endpoint():
    print("\n=== Testing Profile Endpoint with JWT ===\n")
    
    # Create test user
    print("1. Creating test user...")
    random_id = random.randint(8000, 8999)
    user_doc = {
        "public_id": f"k{random_id}",
        "firebase_uid": None,
        "full_name": "Test User Profile",
        "email": f"testprofile{random_id}@kirnagram.com",
        "mobile": "8888888888",
        "password_hash": hash_password("TestPassword123"),
        "auth_type": "manual",
        "account_type": "public",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True,
        "two_factor_enabled": False,
        "bio": "Test bio",
        "profile_pic": "https://example.com/pic.jpg"
    }
    
    result = await db.users.insert_one(user_doc)
    user_id = str(result.inserted_id)
    await db.users.update_one({"_id": result.inserted_id}, {"$set": {"firebase_uid": user_id}})
    print(f"✅ User created: {user_id}")
    
    # Create token
    print("\n2. Creating JWT token...")
    tokens = create_session_tokens(user_id, email=user_doc["email"])
    token = tokens['access_token']
    print(f"✅ Token created")
    
    # Test profile endpoint
    print("\n3. Testing /profile/me endpoint...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                'http://localhost:8000/profile/me',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10.0
            )
            print(f"   Status: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Profile fetched successfully!")
                print(f"   Email: {data.get('email')}")
                print(f"   Full name: {data.get('full_name')}")
                print(f"   Public ID: {data.get('public_id')}")
            else:
                print(f"❌ Error: {response.json()}")
    except Exception as e:
        print(f"❌ Connection error: {e}")
        print(f"   Make sure backend is running on port 8000")

asyncio.run(test_profile_endpoint())
