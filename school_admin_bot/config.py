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

# Period-to-Time mapping for CTSS (Period number -> Start time)
# Used for scheduling relief reminders
PERIOD_TIMES = {
    "0": "07:35",
    "1": "08:00",
    "2": "08:20",
    "3": "08:40",
    "4": "09:00",
    "5": "09:20",
    "6": "09:40",
    "7": "10:00",
    "8": "10:20",
    "9": "10:40",
    "10": "11:00",
    "11": "11:20",
    "12": "11:40",
    "13": "12:00",
    "14": "12:20",
    "15": "12:40",
    "16": "13:00",
    "17": "13:20",
    "18": "13:40",
    "19": "14:00",
    "20": "14:20",
    "21": "14:40",
    "22": "15:00",
    "23": "15:20",
    "24": "15:40",
    "25": "16:00",
}

# Minutes before lesson to send reminder
REMINDER_MINUTES_BEFORE = 5

# Validate required environment variables
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required")

if not CLAUDE_API_KEY:
    raise ValueError("CLAUDE_API_KEY environment variable is required")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

if not SUPER_ADMIN_IDS:
    raise ValueError("SUPER_ADMIN_IDS environment variable is required")
