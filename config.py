import os

import discord

# ----------------- КОНФИГУРАЦИЯ ИЗ ENV -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))
API_SECRET_KEY = os.getenv("API_SECRET_KEY")


def _parse_int_list(raw: str) -> list[int]:
    # Некорректные ID молча отбрасываются, а не роняют старт процесса
    return [
        int(value.strip())
        for value in raw.split(",")
        if value.strip().isdigit()
    ]


def _parse_str_list(raw: str) -> list[str]:
    # Значения через запятую, пустые отбрасываются
    return [value.strip() for value in raw.split(",") if value.strip()]


# ----------------- НАСТРОЙКИ ИЗ ENV -----------------
# Читаем ID через запятую из .env и преобразуем в списки int
ALLOWED_MODERATOR_ROLE_IDS = _parse_int_list(
    os.getenv("ALLOWED_MODERATOR_ROLE_IDS", "")
)

START_ROLE_IDS = _parse_int_list(os.getenv("START_ROLE_IDS", ""))


def _parse_int_env(name: str, default: int = 0) -> int:
    raw = (os.getenv(name, "") or "").strip()
    return int(raw) if raw.isdigit() else default


# ----------------- АНКЕТА: GOOGLE SHEETS -----------------
# ID таблицы и имена листов
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SHEET_REFERENCE = os.getenv("SHEET_REFERENCE", "Справочник")
SHEET_RESPONSES = os.getenv("SHEET_RESPONSES", "Форма +Бойцы")

# Имя файла ключа сервисного аккаунта (может быть относительным — резолвится от корня проекта)
SERVICE_ACCOUNT_FOR_SPREADSHEET_FILENAME = os.getenv(
    "SERVICE_ACCOUNT_FOR_SPREADSHEET_FILENAME", ""
)

# ----------------- АНКЕТА: DISCORD -----------------
APPLICATION_CHANNEL_ID = _parse_int_env("APPLICATION_CHANNEL_ID")

# Роли офицеров и гостевые роли
OFFICER_ROLE_IDS = _parse_int_list(os.getenv("OFFICER_ROLE_IDS", ""))
GUEST_ROLE_IDS = _parse_int_list(os.getenv("GUEST_ROLE_IDS", ""))
CLOSE_GUEST_ROLE_IDS = _parse_int_list(os.getenv("CLOSE_GUEST_ROLE_IDS", ""))

# Роль, которую пингуем при заявке на дополнительную роль (если не задана — пингуем модераторов)
ONLY_MODER_DS_ID = _parse_int_env("ONLY_MODER_DS_ID")

# Варианты часового пояса для меню анкеты (по умолчанию MCK-12 … MCK+12)
TIMEZONE_OPTIONS = _parse_str_list(os.getenv("TIMEZONE_OPTIONS", "")) or [
    f"MCK+{offset}" if offset >= 0 else f"MCK{offset}"
    for offset in range(-12, 13)
]

# ----------------- ПРОВЕРКА ПРАВ -----------------
def is_moderator(user: discord.abc.User) -> bool:
    """Проверяет наличие роли модератора из ALLOWED_MODERATOR_ROLE_IDS."""
    user_role_ids = [role.id for role in getattr(user, "roles", [])]
    return any(role_id in user_role_ids for role_id in ALLOWED_MODERATOR_ROLE_IDS)


def is_officer(user: discord.abc.User) -> bool:
    """Офицер — модератор ИЛИ обладатель роли из OFFICER_ROLE_IDS."""
    user_role_ids = [role.id for role in getattr(user, "roles", [])]
    allowed = set(ALLOWED_MODERATOR_ROLE_IDS) | set(OFFICER_ROLE_IDS)
    return any(role_id in allowed for role_id in user_role_ids)
