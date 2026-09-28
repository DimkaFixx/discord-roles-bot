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


# ----------------- НАСТРОЙКИ ИЗ ENV -----------------
# Читаем ID через запятую из .env и преобразуем в списки int
ALLOWED_MODERATOR_ROLE_IDS = _parse_int_list(
    os.getenv("ALLOWED_MODERATOR_ROLE_IDS", "")
)

START_ROLE_IDS = _parse_int_list(os.getenv("START_ROLE_IDS", ""))

# ----------------- ПРОВЕРКА ПРАВ -----------------
def is_moderator(user: discord.abc.User) -> bool:
    """Проверяет наличие роли модератора из ALLOWED_MODERATOR_ROLE_IDS."""
    user_role_ids = [role.id for role in getattr(user, "roles", [])]
    return any(role_id in user_role_ids for role_id in ALLOWED_MODERATOR_ROLE_IDS)
