import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Telegram Bot Token
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Claude API Key
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

# Database URL (PostgreSQL)
DATABASE_URL = os.getenv("DATABASE_URL")

# File storage path
STORAGE_PATH = os.getenv("STORAGE_PATH", "./data/uploads")

# Super admin Telegram IDs (comma-separated in env)
SUPER_ADMIN_IDS_STR = os.getenv("SUPER_ADMIN_IDS", "")
SUPER_ADMIN_IDS = [int(id.strip()) for id in SUPER_ADMIN_IDS_STR.split(",") if id.strip()]

# Daily code settings
DAILY_CODE_LENGTH = 4  # Number of digits in code

# Predefined tags for uploads
TAGS = [
    "RELIEF",
    "ABSENT",
    "EVENT",
    "VENUE_CHANGE",
    "DUTY_ROSTER",
    "GENERAL",
]

# Validate required environment variables
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required")

if not CLAUDE_API_KEY:
    raise ValueError("CLAUDE_API_KEY environment variable is required")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

if not SUPER_ADMIN_IDS:
    raise ValueError("SUPER_ADMIN_IDS environment variable is required")
