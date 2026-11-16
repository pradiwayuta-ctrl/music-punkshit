import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kaelen_dj")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"].strip()
SESSION_STRING = os.environ["SESSION_STRING"].strip()
OWNER_ID = int(os.environ["OWNER_ID"])

def _parse_allowed_groups() -> set[int]:
    raw = os.environ.get("ALLOWED_GROUPS", "")
    allowed: set[int] = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if token.lstrip("-").isdigit():
            val = int(token)
            allowed.add(val)
            if val > 0:
                allowed.add(int(f"-100{val}"))
    return allowed

def _get_music_vault_id() -> int | str | None:
    raw = os.environ.get("MUSIC_VAULT_ID")
    if not raw:
        return None
    val = str(raw).strip()
    return int(val) if val.lstrip("-").isdigit() else val

ALLOWED_GROUPS: set[int] = _parse_allowed_groups()
MUSIC_VAULT_ID = _get_music_vault_id()
AUTO_DELETE_DELAY = 45
OS_MUTE_SWEEP_INTERVAL = 10
OS_IDLE_TIMEOUT = 300
CACHE_DIR = "/tmp/kaelen_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
