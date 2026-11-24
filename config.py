import os
import pathlib

base_dir = pathlib.Path(__file__).resolve().parent

def _load_dotenv(path: pathlib.Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

_load_dotenv(base_dir / "configs" / ".env")
_load_dotenv(base_dir / ".env")

def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default)).strip()

def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default

def _float_env(name: str, default: float) -> float | int:
    try:
        val = float(_env(name, str(default)))
        return int(val) if val.is_integer() else val
    except ValueError:
        return default

def _env_rev(key: str, default: str = "") -> str:
    val = os.getenv(f"REVOLUTION_{key}")
    if val is not None and val != "":
        return val
    return os.getenv(f"NOREPLY_{key}", default)

def _int_env_rev(key: str, default: int) -> int:
    val = _env_rev(key, "")
    try:
        return int(val) if val else default
    except ValueError:
        return default

def _float_env_rev(key: str, default: float) -> float:
    val = _env_rev(key, "")
    try:
        return float(val) if val else default
    except ValueError:
        return default

bot_token = _env_rev("BOT_TOKEN")
admin_ids = [
    int(part)
    for part in _env_rev("ADMIN_IDS", "9275496970,915316082,6226831200")
    .replace(";", ",")
    .split(",")
    if part.strip().lstrip("-").isdigit()
]

FELIX_STICKERS: dict[str, str] = {
    "welcome": "CAACAgIAAxkBAAIDqWr-o2hCD8oTl1uGpiCCj_ZfDtR7AAKwDAAC1c7YSRgEfwPw-wzfPQQ",
    "menu": "CAACAgIAAxUAAWr-o7OtN0-QoJJK6njYgeg4pE4EAAL1DAACt1zZSR8FnNZCmxcqPQQ",
    "order": "CAACAgIAAxUAAWr-o7PgW7bjYLZeVI8e5A7hgiIDAAIeDwAC3L8RSi_XlinDNYQAAT0E",
    "profile": "CAACAgIAAxUAAWr-o7NVqZae9qA08JTkRinf5eheAAJ1DwACvScRSgNCdFZ_RgthPQQ",
    "history": "CAACAgIAAxUAAWr-o7OoL_jaP0hdiX50eoDQ-NsrAAJKDwACYKbQSd0z7t52NZyrPQQ",
    "protect": "CAACAgIAAxUAAWr-o7N57zBlrvKXOG-BaLGPO0WDAAIGDQACXicZSiFZnpokFU0ZPQQ",
    "vip": "CAACAgIAAxUAAWr-o7NeXtc-IsSBGRl-b2QhG136AAIbDAAC9HEYSnK9-WcXPPVOPQQ",
    "admin": "CAACAgIAAxUAAWr-o7Om7_60zdrcUxQOTUT0B1eIAAJGDQAC3MRpStjhudGyNjNwPQQ",
    "help": "CAACAgIAAxUAAWr-o7MptZndm7L1almTa3mts1cZAAI6DwACbKXZSQ009lztSI28PQQ",
}

sticker_welcome = FELIX_STICKERS["welcome"]
sticker_menu = FELIX_STICKERS["menu"]
sticker_order = FELIX_STICKERS["order"]
sticker_profile = FELIX_STICKERS["profile"]
sticker_history = FELIX_STICKERS["history"]
sticker_protect = FELIX_STICKERS["protect"]
sticker_vip = FELIX_STICKERS["vip"]
sticker_admin = FELIX_STICKERS["admin"]
sticker_help = FELIX_STICKERS["help"]

PHOTO_TO_STICKER: dict[str, str] = {
    "banner_menu.png": FELIX_STICKERS["menu"],
    "menu.jpg": FELIX_STICKERS["menu"],
    "banner_order.png": FELIX_STICKERS["order"],
    "order.jpg": FELIX_STICKERS["order"],
    "banner_profile.png": FELIX_STICKERS["profile"],
    "banner_history.png": FELIX_STICKERS["history"],
    "banner_protect.png": FELIX_STICKERS["protect"],
    "banner_vip.png": FELIX_STICKERS["vip"],
    "banner_admin.png": FELIX_STICKERS["admin"],
    "banner_welcome.png": FELIX_STICKERS["welcome"],
}

photo_menu = base_dir / "pic" / "banner_menu.png"
if not photo_menu.exists():
    photo_menu = base_dir / "pic" / "menu.jpg"

photo_order = base_dir / "pic" / "banner_order.png"
if not photo_order.exists():
    photo_order = base_dir / "pic" / "order.jpg"

photo_welcome = base_dir / "pic" / "banner_welcome.png"
photo_profile = base_dir / "pic" / "banner_profile.png"
photo_history = base_dir / "pic" / "banner_history.png"
photo_protect = base_dir / "pic" / "banner_protect.png"
photo_vip = base_dir / "pic" / "banner_vip.png"
photo_admin = base_dir / "pic" / "banner_admin.png"
logo_avatar = base_dir / "pic" / "logo_avatar.png"

vip_price = _int_env("VIP_PRICE", 100)
xrocket_api_key = _env("XROCKET_API_KEY")
xrocket_enabled = bool(xrocket_api_key)
xrocket_api_url = _env("XROCKET_API_URL", "https://pay.api.xrocket.exchange")
xrocket_vip_price_usdt = _float_env("XROCKET_VIP_PRICE_USDT", 2)
xrocket_vip_price_gram = _float_env("XROCKET_VIP_PRICE_GRAM", 2.2)
xrocket_vip_prices: dict[str, float | int] = {
    "USDT": xrocket_vip_price_usdt,
    "GRAM": xrocket_vip_price_gram,
}
default_max_duration = _int_env("DEFAULT_MAX_DURATION", 40)
default_max_active = _int_env("DEFAULT_MAX_ACTIVE", 1)
default_daily_limit = _int_env("DEFAULT_DAILY_LIMIT", 5)
default_rate_limit = _int_env("DEFAULT_RATE_LIMIT", 5)
free_request_services_limit = _int_env_rev("FREE_REQUEST_SERVICES_LIMIT", 30)

vip_max_duration = _int_env("VIP_MAX_DURATION", 7200)
vip_max_active = _int_env("VIP_MAX_ACTIVE", 10)
vip_daily_limit = _int_env("VIP_DAILY_LIMIT", 100)
vip_rate_limit = _int_env("VIP_RATE_LIMIT", 10)

actual_default_rate_limit = _float_env_rev("ACTUAL_DEFAULT_RATE_LIMIT", 15.0)
actual_vip_rate_limit = _float_env_rev("ACTUAL_VIP_RATE_LIMIT", 35.0)

channel_url = _env("CHANNEL_URL", "https://t.me/DevRevolution")
channel_id = _env("CHANNEL_ID", "@DevRevolution")
force_channel_sub = _env("FORCE_CHANNEL_SUB", "true").lower() in ("1", "true", "yes")
vip_community_chat_id = _int_env_rev("VIP_COMMUNITY_CHAT_ID", -1004422775130)

http_workers = _int_env_rev("HTTP_WORKERS", 2)
http_workers_vip = _int_env_rev("HTTP_WORKERS_VIP", 6)
http_workers_cap = _int_env_rev("WORKERS_CAP", 8)
http_delay_ms = _int_env_rev("DELAY_MS", 50)
request_timeout = _int_env_rev("REQUEST_TIMEOUT", 6)
recaptcha_timeout = _int_env_rev("RECAPTCHA_TIMEOUT", 20)
yandex_captcha_timeout = _int_env_rev("YANDEX_CAPTCHA_TIMEOUT", 15)
yandex_captcha_retries = _int_env_rev("YANDEX_CAPTCHA_RETRIES", 2)
http_connector_limit = _int_env_rev("CONNECTOR_LIMIT", 1536)
http_connector_per_host = _int_env_rev("CONNECTOR_PER_HOST", 48)

emul_workers = _int_env_rev("EMUL_WORKERS", 4)
emul_page_timeout = _float_env_rev("EMUL_PAGE_TIMEOUT", 15.0)
emul_context_rotation = _int_env_rev("EMUL_CONTEXT_ROTATION", 20)

max_attacks = _int_env_rev("MAX_ATTACKS", 30)

configs_dir = base_dir / "configs"
data_dir = base_dir / "data"
logs_dir = base_dir / "logs"

services_file_env = _env_rev("SERVICES_FILE", "")
if services_file_env:
    services_file = pathlib.Path(services_file_env).resolve()
elif (configs_dir / "services.json").exists():
    services_file = configs_dir / "services.json"
else:
    services_file = base_dir / "services.json"

proxy = _env_rev("PROXY", "")
proxies_file_env = _env_rev("PROXIES_FILE", "")
if proxies_file_env:
    proxies_file = pathlib.Path(proxies_file_env).resolve()
elif (configs_dir / "proxies.json").exists():
    proxies_file = configs_dir / "proxies.json"
elif (configs_dir / "proxies.txt").exists():
    proxies_file = configs_dir / "proxies.txt"
elif (base_dir / "proxies.json").exists():
    proxies_file = base_dir / "proxies.json"
else:
    proxies_file = base_dir / "proxies.txt"
proxy_cooldown = 45.0
proxy_fail_threshold = 5

if (data_dir / "users.db").exists():
    db_path = data_dir / "users.db"
else:
    db_path = base_dir / "users.db"

if (logs_dir / "bot.log").exists() or logs_dir.is_dir():
    log_file = logs_dir / "bot.log"
else:
    log_file = base_dir / "bot.log"
log_level = _env_rev("LOG_LEVEL", "INFO")
log_max_bytes = 5 * 1024 * 1024
log_backups = 3

health_host = _env_rev("HEALTH_HOST", "127.0.0.1")
health_port = _int_env_rev("HEALTH_PORT", 8080)

if (data_dir / ".revolution.lock").exists() or data_dir.is_dir():
    lock_file = data_dir / ".revolution.lock"
else:
    lock_file = base_dir / ".revolution.lock"

fsm_cleanup_hours = _int_env_rev("FSM_CLEANUP_HOURS", 24)

broadcast_batch = 25
broadcast_delay = 0.05

pizza_sushi_wok_secret = _env_rev("PIZZA_SUSHI_WOK_SECRET", "super_long_web_secret")
