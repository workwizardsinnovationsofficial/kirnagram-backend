#!/usr/bin/env python3
"""
Diagnostic script to test Cloudflare R2 credentials.
Run this to verify if your access key and secret are correct.
"""
import boto3
import os
from dotenv import load_dotenv
from botocore.config import Config

load_dotenv()

ENDPOINT = os.getenv("R2_ENDPOINT")
ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
SECRET_KEY = os.getenv("R2_SECRET_KEY")
BUCKET = os.getenv("R2_BUCKET_NAME")

print("=" * 60)
print("🔍 Cloudflare R2 Credentials Diagnostic")
print("=" * 60)
print(f"\n📋 Configuration:")
print(f"  Endpoint:    {ENDPOINT}")
print(f"  Bucket:      {BUCKET}")
print(f"  Access Key:  {ACCESS_KEY[:10]}...{ACCESS_KEY[-10:]}")
print(f"  Secret Key:  {SECRET_KEY[:10]}...{SECRET_KEY[-10:]}")

try:
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
    )
    
    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="auto",
        config=config,
    )
    
    print("\n\n🧪 Test 1: HEAD_BUCKET (Check bucket access)")
    try:
        s3.head_bucket(Bucket=BUCKET)
        print("  ✅ PASSED - Bucket exists and is accessible")
    except Exception as e:
        print(f"  ❌ FAILED - {str(e)}")
    
    print("\n🧪 Test 2: PUT_OBJECT (Write a tiny test file)")
    try:
        s3.put_object(
            Bucket=BUCKET,
            Key="test/diagnostic-check.txt",
            Body=b"R2 Diagnostic Test",
            ContentType="text/plain"
        )
        print("  ✅ PASSED - Successfully wrote to R2!")
        print(f"  📍 Test file path: test/diagnostic-check.txt")
    except Exception as e:
        print(f"  ❌ FAILED - {str(e)}")
        print("\n⚠️  LIKELY CAUSE:")
        print("  Your R2_ACCESS_KEY and R2_SECRET_KEY are NOT valid S3 API credentials.")
        print("  You need to create a new S3 API Token in Cloudflare R2 dashboard.")
    
    print("\n🧪 Test 3: GET_OBJECT (Read the test file back)")
    try:
        response = s3.get_object(Bucket=BUCKET, Key="test/diagnostic-check.txt")
        content = response['Body'].read()
        print(f"  ✅ PASSED - Read {len(content)} bytes: {content.decode()}")
    except Exception as e:
        print(f"  ❌ FAILED - {str(e)}")

except Exception as e:
    print(f"\n❌ Fatal Error: {str(e)}")

print("\n" + "=" * 60)
print("💡 How to fix (if tests failed):")
print("=" * 60)
print("""
1. Go to: https://dash.cloudflare.com/
2. Select your account → R2
3. Click "S3 API Tokens" in the left sidebar
4. Click "Create S3 API Token"
5. Choose "Object Read & Write" + "Bucket Read & Write" permissions
6. Apply to bucket: kirnagram-media (or leave as "All buckets")
7. Click "Create S3 API Token"
8. Copy the "Access Key ID" and "Secret Access Key"
9. Update your backend/.env:
   R2_ACCESS_KEY=<Access Key ID>
   R2_SECRET_KEY=<Secret Access Key>
10. Restart the backend and try again!
""")
print("=" * 60)
