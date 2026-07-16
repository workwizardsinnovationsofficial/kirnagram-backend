import re
import os
from pathlib import Path
from bson import ObjectId

# Files to update
routes_dir = Path('app/routes')
files_to_update = [
    'follow.py', 'story.py', 'upload.py', 'notification.py',
    'otp.py', 'payment_history.py', 'remix.py', 'two_factor.py', 'withdraw.py'
]

for filename in files_to_update:
    filepath = routes_dir / filename
    if not filepath.exists():
        print(f'❌ {filename} - not found')
        continue
    
    with open(filepath, 'r', encoding='utf-8') as f:
        original = f.read()
    
    content = original
    changes = []
    
    # Step 1: Update import
    if 'from app.firebase import verify_firebase_token' in content:
        content = content.replace(
            'from app.firebase import verify_firebase_token',
            'from app.jwt_auth import verify_access_token, extract_token_from_header, get_user_id_from_authorization_header'
        )
        changes.append("Updated Firebase import → JWT")
    
    # Step 2: Replace firebase_uid queries with _id
    # Pattern: {"firebase_uid": ...} → {"_id": ObjectId(...)}
    firebase_uid_pattern = r'{"firebase_uid":\s*(\w+)}'
    if re.search(firebase_uid_pattern, content):
        content = re.sub(firebase_uid_pattern, r'{"_id": ObjectId(\1)}', content)
        changes.append("Updated firebase_uid queries → _id")
    
    # Step 3: Replace decoded["uid"] with user_id from JWT
    # This is a bit tricky, so we'll just flag it
    if 'decoded["uid"]' in content:
        changes.append("⚠️  Manual update needed: decoded['uid'] → user_id from JWT helper")
    
    # Write back if changed
    if content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f'✓ {filename}')
        for change in changes:
            print(f'    {change}')
    else:
        print(f'⊘ {filename} - no changes')

print("\n✅ Bulk import and query updates completed!")
print("⚠️  Some files may need manual fixes for token extraction patterns.")
