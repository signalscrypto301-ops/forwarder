import os
import yaml

set_yaml = {}
config_path = os.path.join(os.path.dirname(__file__), "config.yml")
if os.path.exists(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            set_yaml = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Warning: Could not read config.yml: {e}")

settings = set_yaml.get("settings", {})

bot_token = os.getenv("BOT_TOKEN") or settings.get("bot_token", "")

# Support admin_ids from env (comma-separated: "123,456") or list from config.yml
admin_ids_raw = os.getenv("ADMIN_IDS")
if admin_ids_raw:
    admin_ids = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()]
else:
    admin_ids = settings.get("admin_ids", [])

whatsapp_service = os.getenv("WHATSAPP_SERVICE") or settings.get(
    "whatsapp_service", "http://whatsapp-bot:5426"
)
API_ID = int(os.getenv("API_ID") or settings.get("API_ID", 0))
API_HASH = os.getenv("API_HASH") or settings.get("API_HASH", "")

api_secret = os.getenv("API_SECRET") or settings.get(
    "api_secret", "forwarder_internal_secret_key_8f3a92b"
)
telethon_session_string = os.getenv("TELETHON_SESSION_STRING") or settings.get(
    "telethon_session_string", ""
)

# Anti-Ban Rate Limiting & Deliverability Protection (Optimization 2.A)
rate_limits = set_yaml.get("rate_limits", {})
MAX_MSGS_PER_HOUR = int(
    os.getenv("MAX_MSGS_PER_HOUR") or rate_limits.get("max_per_hour", 120)
)
MAX_MSGS_PER_DAY = int(
    os.getenv("MAX_MSGS_PER_DAY") or rate_limits.get("max_per_day", 1000)
)
ALERT_THRESHOLD_PERCENT = float(
    os.getenv("ALERT_THRESHOLD_PERCENT")
    or rate_limits.get("alert_threshold_percent", 80.0)
)
PER_RECIPIENT_RATE = float(
    os.getenv("PER_RECIPIENT_RATE") or rate_limits.get("per_recipient_rate", 0.2)
)
PER_RECIPIENT_BURST = int(
    os.getenv("PER_RECIPIENT_BURST") or rate_limits.get("per_recipient_burst", 3)
)
