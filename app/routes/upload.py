from fastapi import APIRouter, UploadFile, File, Header, HTTPException
from app.r2 import s3, BUCKET_NAME, PUBLIC_BASE
from app.jwt_auth import verify_access_token, extract_token_from_header, get_user_id_from_authorization_header
from app.firebase import verify_firebase_token
from app.database import db
from bson import ObjectId
import os
import logging
import time
from io import BytesIO

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["Upload"])


# 🧪 TEST ENDPOINT - Check R2 Configuration
@router.get("/test-r2-config")
async def test_r2_config():
    """Test endpoint to verify R2 configuration"""
    return {
        "bucket_name": BUCKET_NAME,
        "public_base": PUBLIC_BASE,
        "endpoint_url": os.getenv("R2_ENDPOINT"),
        "has_access_key": bool(os.getenv("R2_ACCESS_KEY")),
        "has_secret_key": bool(os.getenv("R2_SECRET_KEY"))
    }


@router.get("/debug/check-database")
async def debug_check_database(authorization: str = Header(...)):
    """🔍 DEBUG: Check what's actually in the database for the current user"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]
        
        user = await db.users.find_one({"firebase_uid": uid}, {"_id": 0})
        
        if not user:
            return {
                "status": "error",
                "message": f"User not found in database for firebase_uid={uid}",
                "firebase_uid": uid
            }
        
        return {
            "status": "success",
            "uid": uid,
            "firebase_uid": user.get("firebase_uid"),
            "username": user.get("username"),
            "full_name": user.get("full_name"),
            "image_name": user.get("image_name"),
            "cover_image": user.get("cover_image"),
            "gender": user.get("gender"),
            "image_exists": bool(user.get("image_name")),
            "cover_exists": bool(user.get("cover_image")),
            "message": "Image data from database"
        }
    
    except Exception as e:
        logger.error(f"Debug endpoint error: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": str(e)
        }


@router.post("/profile-image")
async def upload_profile_image(
    file: UploadFile = File(...),
    authorization: str = Header(...)
):
    try:
        logger.info(f"🔹 Uploading profile image: {file.filename}")
        
        # 1️⃣ GET FIREBASE TOKEN
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)

        # 2️⃣ USER ID FROM FIREBASE
        uid = decoded["uid"]
        logger.info(f"✅ User authenticated: {uid}")

        # 3️⃣ FILE EXTENSION (jpg / png / webp)
        if not file.filename or "." not in file.filename:
            raise ValueError("Invalid filename: no extension found")
        
        ext = file.filename.split(".")[-1].lower()
        logger.info(f"📄 File extension: {ext}")

        # 4️⃣ UNIQUE FILE PATH IN R2
        filename = f"profile/{uid}.{ext}"
        logger.info(f"📁 R2 path: {filename}")
        logger.info(f"🪣 Bucket: {BUCKET_NAME}")
        logger.info(f"🌐 Public base: {PUBLIC_BASE}")

        # 5️⃣ UPLOAD FILE TO CLOUDFLARE R2
        file_content = await file.read()
        logger.info(f"📤 File size: {len(file_content)} bytes")
        
        # Upload with timeout and better error handling
        try:
            s3.upload_fileobj(
                BytesIO(file_content),
                BUCKET_NAME,
                filename,
                ExtraArgs={
                    "ContentType": file.content_type or "image/png"
                }
            )
            logger.info(f"✅ File uploaded to R2 successfully")
        except Exception as upload_error:
            logger.error(f"❌ R2 Upload failed: {str(upload_error)}")
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to upload to R2: {str(upload_error)}"
            )

        # 6️⃣ PUBLIC IMAGE URL
        base_url = PUBLIC_BASE.rstrip("/")
        public_url = f"{base_url}/{filename}"
        logger.info(f"🔗 Public URL constructed: {public_url}")
        logger.info(f"   Base URL: {base_url}")
        logger.info(f"   Bucket: {BUCKET_NAME}")
        logger.info(f"   Filename: {filename}")

        # 7️⃣ VERIFY USER EXISTS IN DATABASE BEFORE SAVING
        user_exists = await db.users.find_one({"firebase_uid": uid})
        if not user_exists:
            logger.error(f"❌ User document not found for firebase_uid={uid}")
            raise HTTPException(
                status_code=400, 
                detail=f"User document not found. Please ensure user is registered first."
            )
        logger.info(f"✅ User document found for firebase_uid={uid}")

        # SAVE IMAGE URL IN MONGODB
        result = await db.users.update_one(
            {"firebase_uid": uid},
            {"$set": {"image_name": public_url}}
        )
        logger.info(f"✅ Database updated - Modified count: {result.modified_count}")
        if result.modified_count == 0:
            logger.warning(f"⚠️ No document was modified. This could mean the update failed.")
        logger.info(f"   Saved URL to MongoDB: {public_url}")
        
        # 7️⃣ VERIFY SAVE WAS SUCCESSFUL
        updated_user = await db.users.find_one({"firebase_uid": uid})
        if updated_user and updated_user.get("image_name") == public_url:
            logger.info(f"✅ Verification successful: image_name is saved correctly in database")
        else:
            logger.error(f"❌ Verification failed: image_name NOT found in database after save")
            logger.error(f"   Expected: {public_url}")
            logger.error(f"   Actual: {updated_user.get('image_name') if updated_user else 'User not found'}")
            raise HTTPException(
                status_code=500,
                detail="Failed to save image to database"
            )

        # 8️⃣ RETURN IMAGE URL TO FRONTEND
        return {"image_url": public_url}

    except Exception as e:
        logger.error(f"❌ Error uploading profile image: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))



@router.post("/cover-image")
async def upload_cover_image(
    file: UploadFile = File(...),
    authorization: str = Header(...)
):
    try:
        logger.info(f"🔹 Uploading cover image: {file.filename}")
        
        # 1️⃣ GET FIREBASE TOKEN
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)

        # 2️⃣ USER ID FROM FIREBASE
        uid = decoded["uid"]
        logger.info(f"✅ User authenticated: {uid}")

        # 3️⃣ FILE EXTENSION (jpg / png / webp)
        if not file.filename or "." not in file.filename:
            raise ValueError("Invalid filename: no extension found")
        
        ext = file.filename.split(".")[-1].lower()
        logger.info(f"📄 File extension: {ext}")

        # 4️⃣ UNIQUE FILE PATH IN R2
        filename = f"cover/{uid}.{ext}"
        logger.info(f"📁 R2 path: {filename}")

        # 5️⃣ UPLOAD FILE TO CLOUDFLARE R2
        file_content = await file.read()
        logger.info(f"📤 File size: {len(file_content)} bytes")
        
        s3.upload_fileobj(
            BytesIO(file_content),
            BUCKET_NAME,
            filename,
            ExtraArgs={
                "ContentType": file.content_type
            }
        )
        logger.info(f"✅ File uploaded to R2")

        # 6️⃣ PUBLIC IMAGE URL
        base_url = PUBLIC_BASE.rstrip("/")
        public_url = f"{base_url}/{filename}"
        logger.info(f"🔗 Public URL constructed: {public_url}")
        logger.info(f"   Base URL: {base_url}")
        logger.info(f"   Bucket: {BUCKET_NAME}")
        logger.info(f"   Filename: {filename}")

        # 7️⃣ VERIFY USER EXISTS IN DATABASE BEFORE SAVING
        user_exists = await db.users.find_one({"firebase_uid": uid})
        if not user_exists:
            logger.error(f"❌ User document not found for firebase_uid={uid}")
            raise HTTPException(
                status_code=400, 
                detail=f"User document not found. Please ensure user is registered first."
            )
        logger.info(f"✅ User document found for firebase_uid={uid}")

        # SAVE IMAGE URL IN MONGODB
        result = await db.users.update_one(
            {"firebase_uid": uid},
            {"$set": {"cover_image": public_url}}
        )
        logger.info(f"✅ Database updated - Modified count: {result.modified_count}")
        if result.modified_count == 0:
            logger.warning(f"⚠️ No document was modified. This could mean the update failed.")
        logger.info(f"   Saved URL to MongoDB: {public_url}")
        
        # 8️⃣ VERIFY SAVE WAS SUCCESSFUL
        updated_user = await db.users.find_one({"firebase_uid": uid})
        if updated_user and updated_user.get("cover_image") == public_url:
            logger.info(f"✅ Verification successful: cover_image is saved correctly in database")
        else:
            logger.error(f"❌ Verification failed: cover_image NOT found in database after save")
            logger.error(f"   Expected: {public_url}")
            logger.error(f"   Actual: {updated_user.get('cover_image') if updated_user else 'User not found'}")
            raise HTTPException(
                status_code=500,
                detail="Failed to save cover image to database"
            )

        # 9️⃣ RETURN IMAGE URL TO FRONTEND
        return {"image_url": public_url}

    except Exception as e:
        logger.error(f"❌ Error uploading cover image: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/publisher/govt-id")
async def upload_govt_id(
    file: UploadFile = File(...),
    authorization: str = Header(...)
):
    """Upload government ID document for publisher verification"""
    try:
        logger.info(f"🔹 Uploading government ID: {file.filename}")
        
        # 1️⃣ GET FIREBASE TOKEN
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]
        logger.info(f"✅ User authenticated: {uid}")

        # 2️⃣ FILE EXTENSION
        if not file.filename or "." not in file.filename:
            raise ValueError("Invalid filename: no extension found")
        
        ext = file.filename.split(".")[-1].lower()
        logger.info(f"📄 File extension: {ext}")

        # 3️⃣ UNIQUE FILE PATH IN R2
        filename = f"publisher/govt-id/{uid}.{ext}"
        logger.info(f"📁 R2 path: {filename}")

        # 4️⃣ READ FILE CONTENT
        file_content = await file.read()
        logger.info(f"📤 File size: {len(file_content)} bytes")

        # 5️⃣ UPLOAD TO R2
        s3.upload_fileobj(
            BytesIO(file_content),
            BUCKET_NAME,
            filename,
            ExtraArgs={
                "ContentType": file.content_type or "application/octet-stream"
            }
        )
        logger.info(f"✅ File uploaded to R2 successfully")

        # 6️⃣ CONSTRUCT PUBLIC URL
        base_url = PUBLIC_BASE.rstrip("/")
        public_url = f"{base_url}/{filename}"
        logger.info(f"🔗 Public URL: {public_url}")

        return {"govt_id_url": public_url}

    except Exception as e:
        logger.error(f"❌ Error uploading government ID: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/publisher/ad-logo")
async def upload_ad_logo(
    file: UploadFile = File(...),
    authorization: str = Header(...)
):
    """Upload ad logo/business logo"""
    try:
        logger.info(f"🔹 Uploading ad logo: {file.filename}")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]
        
        if not file.filename or "." not in file.filename:
            raise ValueError("Invalid filename: no extension found")
        
        ext = file.filename.split(".")[-1].lower()
        filename = f"publisher/ads/logo/{uid}-{int(time.time()*1000)}.{ext}"
        
        file_content = await file.read()
        s3.upload_fileobj(
            BytesIO(file_content),
            BUCKET_NAME,
            filename,
            ExtraArgs={"ContentType": file.content_type or "image/png"}
        )
        
        base_url = PUBLIC_BASE.rstrip("/")
        public_url = f"{base_url}/{filename}"
        logger.info(f"✅ Ad logo uploaded: {public_url}")

        return {"logo_url": public_url}

    except Exception as e:
        logger.error(f"❌ Error uploading ad logo: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/publisher/ad-photo")
async def upload_ad_photo(
    file: UploadFile = File(...),
    authorization: str = Header(...)
):
    """Upload ad photo preview"""
    try:
        logger.info(f"🔹 Uploading ad photo: {file.filename}")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]
        
        if not file.filename or "." not in file.filename:
            raise ValueError("Invalid filename: no extension found")
        
        ext = file.filename.split(".")[-1].lower()
        filename = f"publisher/ads/photo/{uid}-{int(time.time()*1000)}.{ext}"
        
        file_content = await file.read()
        s3.upload_fileobj(
            BytesIO(file_content),
            BUCKET_NAME,
            filename,
            ExtraArgs={"ContentType": file.content_type or "image/png"}
        )
        
        base_url = PUBLIC_BASE.rstrip("/")
        public_url = f"{base_url}/{filename}"
        logger.info(f"✅ Ad photo uploaded: {public_url}")

        return {"photo_url": public_url}

    except Exception as e:
        logger.error(f"❌ Error uploading ad photo: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/publisher/ad-video")
async def upload_ad_video(
    file: UploadFile = File(...),
    authorization: str = Header(...)
):
    """Upload ad video (max 2 minutes / 120 seconds)"""
    try:
        logger.info(f"🔹 Uploading ad video: {file.filename}")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]
        
        if not file.filename or "." not in file.filename:
            raise ValueError("Invalid filename: no extension found")
        
        ext = file.filename.split(".")[-1].lower()
        filename = f"publisher/ads/video/{uid}-{int(time.time()*1000)}.{ext}"
        
        file_content = await file.read()
        s3.upload_fileobj(
            BytesIO(file_content),
            BUCKET_NAME,
            filename,
            ExtraArgs={"ContentType": file.content_type or "video/mp4"}
        )
        
        base_url = PUBLIC_BASE.rstrip("/")
        public_url = f"{base_url}/{filename}"
        logger.info(f"✅ Ad video uploaded: {public_url}")

        return {"video_url": public_url}

    except Exception as e:
        logger.error(f"❌ Error uploading ad video: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
