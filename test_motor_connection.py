import asyncio
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, '.env')
load_dotenv(dotenv_path=ENV_PATH, override=True)

print('MONGO_URI=', os.getenv('MONGO_URI'))
print('DB_NAME=', os.getenv('DB_NAME'))

async def main():
    uri = os.getenv('MONGO_URI')
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000, socketTimeoutMS=5000)
    info = await client.server_info()
    print(info)

asyncio.run(main())
