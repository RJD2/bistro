import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("TOKEN", "")
DB_URL = os.environ.get("DB", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID") or "0")
