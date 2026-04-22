import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN не задан. Создайте .env из .env.example и впишите токен от @BotFather."
    )

# Белый список Telegram ID (через запятую: 123,456). Пусто = доступ всем (отладка).
_whitelist_raw = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = set()
if _whitelist_raw:
    for uid in _whitelist_raw.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USER_IDS.add(int(uid))

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/history.db")
FILES_DIR = os.getenv("FILES_DIR", "data/files")

# Задержка между запросами к WB (сек)
WB_REQUEST_DELAY = float(os.getenv("WB_REQUEST_DELAY", "0.3"))

# Макс. общий таймаут одного сбора (сек) — защита от зависших задач
COLLECTION_TIMEOUT = int(os.getenv("COLLECTION_TIMEOUT", "1800"))  # 30 минут

# Минимальный интервал между редактированием прогресс-сообщения (сек)
PROGRESS_EDIT_INTERVAL = float(os.getenv("PROGRESS_EDIT_INTERVAL", "3.0"))
