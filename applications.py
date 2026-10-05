import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import start_roles
from config import (
    APPLICATION_CHANNEL_ID,
    CLOSE_GUEST_ROLE_IDS,
    GUEST_ROLE_IDS,
    SERVICE_ACCOUNT_FOR_SPREADSHEET_FILENAME,
    SHEET_REFERENCE,
    SHEET_RESPONSES,
    SPREADSHEET_ID,
    TIMEZONE_OPTIONS,
    is_officer,
)

VERSION = "2026-10-05-r1-service-account"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OPTIONS_CACHE_PATH = os.getenv(
    "ANKETA_CACHE_PATH", os.path.join("data", "anketa_options.json")
)
APPLICATIONS_PATH = os.getenv(
    "ANKETA_DATA_PATH", os.path.join("data", "applications.json")
)
STATE_PATH = os.getenv(
    "ANKETA_STATE_PATH", os.path.join("data", "anketa_state.json")
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# custom_id постоянных компонентов (persistent views)
CUSTOM_START = "anketa:start"
CUSTOM_GUEST = "anketa:guest"
CUSTOM_CLOSE_GUEST = "anketa:close_guest"

TYPE_TITLES = {
    "member": "Заявка на вступление",
    "guest": "Заявка: Гость",
    "close_guest": "Заявка: Близкий гость",
}
TYPE_COLORS = {
    "member": discord.Color.blurple(),
    "guest": discord.Color.greyple(),
    "close_guest": discord.Color.dark_teal(),
}
STATUS_LABELS = {
    "pending": "⏳ На рассмотрении",
    "approved": "✅ Одобрено",
    "rejected": "❌ Отклонено",
}

# Кэш вариантов справочника в памяти (группа -> список {label, roles})
_OPTIONS: dict = {}


# ============================================================================
# GOOGLE SHEETS
# ============================================================================
def _service_account_path() -> str:
    name = (SERVICE_ACCOUNT_FOR_SPREADSHEET_FILENAME or "").strip()
    if not name:
        return ""
    return name if os.path.isabs(name) else os.path.join(BASE_DIR, name)


def _open_spreadsheet_sync():
    """Синхронное открытие таблицы. Вызывать только через asyncio.to_thread."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        raise RuntimeError(
            "Не установлены gspread/google-auth. Пересоберите образ (pip install -r requirements.txt)."
        ) from e

    path = _service_account_path()
    if not path:
        raise RuntimeError("Не задан SERVICE_ACCOUNT_FOR_SPREADSHEET_FILENAME")
    if not os.path.exists(path):
        raise RuntimeError(f"Файл ключа не найден: {path}")

    if not SPREADSHEET_ID:
        raise RuntimeError("Не задан SPREADSHEET_ID")

    creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


_NAMES_RE = re.compile(r"^(.+?)names$", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@&(\d{15,20})>")
_PLAIN_RE = re.compile(r"\b(\d{15,20})\b")


def _cell(row, index) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index] if row[index] is not None else "")


def _extract_role_ids(text: str) -> list[str]:
    """Достает ID ролей из ячейки: упоминания <@&...> или голые ID."""
    if not text:
        return []
    ids = _MENTION_RE.findall(text)
    if not ids:
        ids = _PLAIN_RE.findall(text)
    result: list[str] = []
    for rid in ids:
        if rid not in result:
            result.append(rid)
    return result


def _find_role_column(lower_headers: list[str], group: str, name_idx: int) -> int:
    exact = [f"{group}namesids", f"{group}rolesids", f"{group}roleids", f"{group}ids"]
    for k, header in enumerate(lower_headers):
        if k != name_idx and header in exact:
            return k
    for k in range(name_idx + 1, len(lower_headers)):
        if "id" in lower_headers[k]:
            return k
    return -1


def _find_header_row(values: list[list[str]]) -> int:
    for r in range(min(len(values), 10)):
        if any(_NAMES_RE.match(str(c).strip().lower()) for c in values[r]):
            return r
    return 0


def parse_options(values: list[list[str]]) -> dict:
    """Разбирает справочник: пары '<base>Names' + '<base>...IDs' -> группы rank/spec/att."""
    if not values:
        return {}
    header_row = _find_header_row(values)
    header = [str(h).strip() for h in values[header_row]]
    lower = [h.lower() for h in header]

    result: dict[str, list[dict]] = {}
    for c, header_lower in enumerate(lower):
        m = _NAMES_RE.match(header_lower)
        if not m:
            continue
        group = m.group(1)
        if not group:
            continue
        role_idx = _find_role_column(lower, group, c)
        for r in range(header_row + 1, len(values)):
            label = _cell(values[r], c).strip()
            if not label:
                continue
            result.setdefault(group, []).append(
                {"label": label, "roles": _extract_role_ids(_cell(values[r], role_idx))}
            )
    return result


def _fetch_reference_values_sync() -> list[list[str]]:
    sh = _open_spreadsheet_sync()
    ws = sh.worksheet(SHEET_REFERENCE)
    return ws.get_all_values()


def _atomic_write_json(path: str, data) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


# ---------- запись заявок в лист ----------
RESPONSE_HEADER_MAP = {
    "callsign": "позывн",
    "number": "номер",
    "rank": "звание",
    "spec": "специализ",
    "att": "припис",
    "timezone": "часовой",
    "discord_tag": "discord-тег",
    "discord_id": "discord id",
    "status": "статус",
    "decided_by": "решение",
}

DEFAULT_RESPONSE_HEADERS = [
    "Отметка времени",
    "Позывной",
    "Номер",
    "Звание, на котором вступаете в батальон",
    "Ваш Discord-тег (в вашем профиле)",
    "Ваш Discord ID (включите в настройках)",
    "Ваш часовой пояс",
    "Специализация, на которой вступаете в батальон (если есть)",
    "(Не) являюсь приписником",
    "Статус",
    "Решение принял",
]


def _ensure_status_columns(ws, headers: list[str]) -> list[str]:
    lower = [h.lower() for h in headers]
    if not any("статус" in h for h in lower):
        col = len(headers) + 1
        ws.update_cell(1, col, "Статус")
        headers.append("Статус")
    if not any("решени" in h for h in lower):
        col = len(headers) + 1
        ws.update_cell(1, col, "Решение принял")
        headers.append("Решение принял")
    return headers


def _value_for_header(header: str, record: dict, status: str, decided_by: str) -> str:
    hl = header.lower()
    answers = record.get("answers", {}) or {}
    if "отметка" in hl or "врем" in hl:
        return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S")

    fields = {
        "callsign": answers.get("callsign", ""),
        "number": answers.get("number", ""),
        "rank": answers.get("rank", ""),
        "spec": answers.get("spec", "") or "",
        "att": answers.get("att", "") or "",
        "timezone": answers.get("tz", "") or "",
        "discord_tag": record.get("discord_tag", ""),
        "discord_id": str(record.get("user_id", "")),
        "status": "Одобрено" if status == "approved" else "Отклонено",
        "decided_by": str(decided_by),
    }
    for field, needle in RESPONSE_HEADER_MAP.items():
        if needle in hl:
            return fields.get(field, "")
    return ""


def _append_application_sync(record: dict, status: str, decided_by: str) -> None:
    sh = _open_spreadsheet_sync()
    ws = sh.worksheet(SHEET_RESPONSES)
    values = ws.get_all_values()
    if not values:
        headers = list(DEFAULT_RESPONSE_HEADERS)
        ws.append_row(headers, value_input_option="RAW")
    else:
        headers = [str(h).strip() for h in values[0]]
        headers = _ensure_status_columns(ws, headers)
    row = [_value_for_header(h, record, status, decided_by) for h in headers]
    ws.append_row(row, value_input_option="USER_ENTERED")


# ============================================================================
# ХРАНИЛИЩА
# ============================================================================
def load_options_cache() -> dict:
    try:
        with open(OPTIONS_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_options() -> dict:
    return _OPTIONS or load_options_cache()


def load_applications() -> dict:
    try:
        with open(APPLICATIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"pending": {}}
    if not isinstance(data, dict):
        return {"pending": {}}
    data.setdefault("pending", {})
    data.setdefault("history", {})
    return data


def save_applications(data: dict) -> None:
    _atomic_write_json(APPLICATIONS_PATH, data)


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    _atomic_write_json(STATE_PATH, state)


# ============================================================================
# СПРАВОЧНИК
# ============================================================================
def _summarize(options: dict) -> str:
    return (
        f"звания: {len(options.get('rank', []))}, "
        f"специализации: {len(options.get('spec', []))}, "
        f"приписки: {len(options.get('att', []))}"
    )


async def refresh_options() -> tuple[bool, str]:
    global _OPTIONS
    try:
        values = await asyncio.to_thread(_fetch_reference_values_sync)
    except Exception as e:
        return False, f"ошибка чтения таблицы: {e}"

    options = parse_options(values)
    if not options:
        return False, "в справочнике не найдено ни одной пары <base>Names/<base>IDs"

    _OPTIONS = options
    _atomic_write_json(OPTIONS_CACHE_PATH, options)
    return True, _summarize(options)


async def warmup() -> None:
    global _OPTIONS
    cached = load_options_cache()
    if cached:
        _OPTIONS = cached
        print(f"[ANKETA] Загружен кэш справочника: {_summarize(cached)}")

    if not SPREADSHEET_ID or not _service_account_path():
        print("[ANKETA] Google Sheets не настроен — пропускаю загрузку справочника")
        return

    ok, msg = await refresh_options()
    print(f"[ANKETA] Обновление справочника из таблицы: {'ok' if ok else 'ошибка'} — {msg}")


def protected_role_ids() -> set[int]:
    """Роли, которые нельзя снимать панелью реакций (старт + анкета + гости)."""
    ids: set[int] = set(start_roles.load_roles())
    ids |= set(GUEST_ROLE_IDS) | set(CLOSE_GUEST_ROLE_IDS)
    for items in get_options().values():
        for item in items:
            for rid in item.get("roles", []):
                if str(rid).isdigit():
                    ids.add(int(rid))
    return ids


# ============================================================================
# РОЛИ
# ============================================================================
def _resolve_role_ids(type_: str, answers: dict) -> list[int]:
    if type_ == "guest":
        return list(GUEST_ROLE_IDS)
    if type_ == "close_guest":
        return list(CLOSE_GUEST_ROLE_IDS)

    ids: list[int] = list(start_roles.load_roles())
    options = get_options()
    for group, key in (("rank", "rank"), ("spec", "spec"), ("att", "att")):
        label = answers.get(key)
        if not label:
            continue
        for item in options.get(group, []):
            if item.get("label") == label:
                ids.extend(int(r) for r in item.get("roles", []) if str(r).isdigit())
                break

    result: list[int] = []
    for rid in ids:
        if rid not in result:
            result.append(rid)
    return result


async def _grant_roles(
    guild: discord.Guild, member: discord.Member, role_ids: list[int]
) -> tuple[list[discord.Role], list[str]]:
    me = guild.me
    to_add: list[discord.Role] = []
    skipped: list[str] = []
    seen: set[int] = set()

    for rid in role_ids:
        if rid in seen:
            continue
        seen.add(rid)
        role = guild.get_role(rid)
        if role is None:
            skipped.append(f"`{rid}` (не найдена)")
            continue
        if me is not None and me.top_role.position <= role.position:
            skipped.append(f"{role.name} (выше роли бота)")
            continue
        if role not in member.roles:
            to_add.append(role)

    if not to_add:
        return [], skipped
    try:
        await member.add_roles(*to_add, reason="Анкета: заявка одобрена")
    except discord.HTTPException as e:
        skipped.append(f"ошибка выдачи: {e}")
        return [], skipped
    return to_add, skipped


# ============================================================================
# UI: ПАНЕЛЬ
# ============================================================================
def build_panel_embed() -> discord.Embed:
    options = get_options()
    embed = discord.Embed(
        title="Анкета батальона",
        color=discord.Color.dark_teal(),
        description=(
            "Нажмите **«Заполнить анкету»**, чтобы указать звание, специализацию, "
            "приписку и контакты.\n"
            "Кнопки **«Гость»** и **«Близкий гость»** создают заявку без формы."
        ),
    )
    embed.set_footer(
        text=(
            f"Звания: {len(options.get('rank', []))} · "
            f"Специализации: {len(options.get('spec', []))} · "
            f"Приписки: {len(options.get('att', []))}"
        )
    )
    return embed


class PanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Заполнить анкету",
        style=discord.ButtonStyle.primary,
        custom_id=CUSTOM_START,
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not get_options().get("rank"):
            await interaction.response.send_message(
                "⚠️ Анкета временно недоступна: справочник не загружен.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Выберите параметры и нажмите «Далее»:",
            view=AnketaSelectView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Гость",
        style=discord.ButtonStyle.secondary,
        custom_id=CUSTOM_GUEST,
    )
    async def guest(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _create_application(interaction, "guest", {})

    @discord.ui.button(
        label="Близкий гость",
        style=discord.ButtonStyle.secondary,
        custom_id=CUSTOM_CLOSE_GUEST,
    )
    async def close_guest(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await _create_application(interaction, "close_guest", {})


class AnketaSelectView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=600)
        self.user_id = user_id
        self.answers: dict = {}

        options = get_options()
        self._add_select(
            "rank", "Звание", options.get("rank", []), required=True
        )
        self._add_select(
            "spec", "Специализация (необязательно)", options.get("spec", []), required=False
        )
        self._add_select(
            "att", "Приписка", options.get("att", []), required=False
        )
        self._add_select(
            "tz",
            "Часовой пояс",
            [{"label": t, "roles": []} for t in TIMEZONE_OPTIONS],
            required=True,
        )

    def _add_select(self, key, placeholder, items, required) -> None:
        if not items:
            return
        select_options = [
            discord.SelectOption(
                label=(str(item.get("label", "")) or "—")[:100],
                value=str(item.get("label", ""))[:100],
            )
            for item in items
        ][:25]

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "Это не ваша анкета.", ephemeral=True
                )
                return
            self.answers[key] = select.values[0] if select.values else None
            await interaction.response.defer()

        select = discord.ui.Select(
            placeholder=placeholder,
            options=select_options,
            min_values=1 if required else 0,
            max_values=1,
            custom_id=f"anketa:sel:{key}",
        )
        select.callback = callback
        self.add_item(select)

    @discord.ui.button(
        label="Далее",
        style=discord.ButtonStyle.success,
        custom_id="anketa:next",
    )
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваша анкета.", ephemeral=True)
            return
        if not self.answers.get("rank"):
            await interaction.response.send_message(
                "Сначала выберите звание.", ephemeral=True
            )
            return
        await interaction.response.send_modal(AnketaModal(self.answers))


class AnketaModal(discord.ui.Modal, title="Анкета — контакты"):
    callsign = discord.ui.TextInput(
        label="Позывной", required=True, max_length=100
    )
    number = discord.ui.TextInput(label="Номер", required=True, max_length=32)

    def __init__(self, answers: dict) -> None:
        super().__init__()
        self.answers = answers

    async def on_submit(self, interaction: discord.Interaction):
        self.answers["callsign"] = str(self.callsign.value).strip()
        self.answers["number"] = str(self.number.value).strip()
        await _create_application(interaction, "member", self.answers)


# ============================================================================
# UI: ЗАЯВКА И РЕШЕНИЕ
# ============================================================================
def build_application_embed(
    record: dict,
    status: str = "pending",
    decided_by: discord.abc.User | None = None,
    added: list[discord.Role] | None = None,
    skipped: list[str] | None = None,
    reason: str = "",
) -> discord.Embed:
    type_ = record.get("type", "member")
    answers = record.get("answers", {}) or {}
    user_id = record.get("user_id")

    embed = discord.Embed(
        title=TYPE_TITLES.get(type_, "Заявка"),
        color=TYPE_COLORS.get(type_, discord.Color.blurple()),
    )
    embed.add_field(
        name="Пользователь",
        value=f"<@{user_id}>\n`{record.get('discord_tag', '')}`\nID: `{user_id}`",
        inline=False,
    )

    if type_ == "member":
        embed.add_field(name="Позывной", value=answers.get("callsign") or "—", inline=True)
        embed.add_field(name="Номер", value=answers.get("number") or "—", inline=True)
        embed.add_field(name="Звание", value=answers.get("rank") or "—", inline=True)
        embed.add_field(
            name="Специализация", value=answers.get("spec") or "—", inline=True
        )
        embed.add_field(name="Приписка", value=answers.get("att") or "—", inline=True)
        embed.add_field(name="Часовой пояс", value=answers.get("tz") or "—", inline=True)

    embed.add_field(
        name="Статус", value=STATUS_LABELS.get(status, status), inline=False
    )
    if status == "approved" and added:
        embed.add_field(
            name="Выданы роли",
            value=" ".join(r.mention for r in added)[:1024],
            inline=False,
        )
    if status == "approved" and skipped:
        embed.add_field(name="Пропущено", value=", ".join(skipped)[:1024], inline=False)
    if reason:
        embed.add_field(name="Причина", value=reason[:1024], inline=False)
    if decided_by is not None:
        embed.set_footer(text=f"Решение: {decided_by} · ID заявки: {record.get('app_id')}")
        embed.timestamp = datetime.now(timezone.utc)
    else:
        embed.set_footer(text=f"ID заявки: {record.get('app_id')}")
    return embed


class RejectModal(discord.ui.Modal, title="Отклонить заявку"):
    reason = discord.ui.TextInput(
        label="Причина (необязательно)",
        required=False,
        max_length=400,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, app_id: str) -> None:
        super().__init__()
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await process_decision(
            interaction, self.app_id, "rejected", str(self.reason.value).strip()
        )


class ApplicationView(discord.ui.View):
    def __init__(self, app_id: str) -> None:
        super().__init__(timeout=None)
        self.app_id = app_id

        approve = discord.ui.Button(
            label="Одобрить",
            style=discord.ButtonStyle.success,
            custom_id=f"anketa:approve:{app_id}",
        )
        reject = discord.ui.Button(
            label="Отклонить",
            style=discord.ButtonStyle.danger,
            custom_id=f"anketa:reject:{app_id}",
        )
        approve.callback = self._approve
        reject.callback = self._reject
        self.add_item(approve)
        self.add_item(reject)

    async def _approve(self, interaction: discord.Interaction):
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                "❌ У вас нет прав для этой заявки.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await process_decision(interaction, self.app_id, "approved")

    async def _reject(self, interaction: discord.Interaction):
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                "❌ У вас нет прав для этой заявки.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RejectModal(self.app_id))


async def _create_application(interaction: discord.Interaction, type_: str, answers: dict):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "❌ Действие доступно только на сервере.", ephemeral=True
        )
        return

    channel = guild.get_channel(APPLICATION_CHANNEL_ID) if APPLICATION_CHANNEL_ID else None
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ Канал заявок не настроен или недоступен.", ephemeral=True
        )
        return

    data = load_applications()
    for record in data["pending"].values():
        if record.get("user_id") == interaction.user.id:
            await interaction.response.send_message(
                "⚠️ У вас уже есть активная заявка.", ephemeral=True
            )
            return

    app_id = uuid.uuid4().hex[:10]
    record = {
        "app_id": app_id,
        "type": type_,
        "user_id": interaction.user.id,
        "discord_tag": str(interaction.user),
        "answers": answers,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

    try:
        message = await channel.send(
            embed=build_application_embed(record, "pending"),
            view=ApplicationView(app_id),
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"❌ Не удалось отправить заявку: {e}", ephemeral=True
        )
        return

    record["message_id"] = message.id
    record["channel_id"] = channel.id
    data["pending"][app_id] = record
    save_applications(data)

    await interaction.response.send_message(
        "✅ Заявка отправлена офицерам на рассмотрение.", ephemeral=True
    )


async def _fetch_message(guild: discord.Guild, channel_id: int, message_id: int):
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return None
    try:
        return await channel.fetch_message(message_id)
    except discord.HTTPException:
        return None


async def _notify_user(guild: discord.Guild, user_id: int, status: str, reason: str = ""):
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return
    if status == "approved":
        text = "✅ Ваша заявка одобрена."
    else:
        text = "❌ Ваша заявка отклонена."
    if reason:
        text += f"\nПричина: {reason}"
    try:
        await member.send(text)
    except discord.Forbidden:
        pass
    except discord.HTTPException as e:
        print(f"[ANKETA] Не удалось отправить DM {user_id}: {e}")


async def process_decision(
    interaction: discord.Interaction, app_id: str, status: str, reason: str = ""
):
    data = load_applications()
    record = data["pending"].get(app_id)
    if record is None:
        await interaction.followup.send(
            "⚠️ Заявка уже обработана или не найдена.", ephemeral=True
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ Только на сервере.", ephemeral=True)
        return

    type_ = record.get("type", "member")
    answers = record.get("answers", {}) or {}
    user_id = record["user_id"]

    added: list[discord.Role] = []
    skipped: list[str] = []
    notes: list[str] = []

    if status == "approved":
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None

        if member is None:
            notes.append("⚠️ Участник не найден на сервере — роли не выданы.")
        else:
            role_ids = _resolve_role_ids(type_, answers)
            added, skipped = await _grant_roles(guild, member, role_ids)

        if type_ == "member":
            try:
                await asyncio.to_thread(
                    _append_application_sync, record, status, str(interaction.user)
                )
                notes.append("Строка добавлена в таблицу.")
            except Exception as e:
                notes.append(f"⚠️ Не удалось записать в таблицу: {e}")
                print(f"[ANKETA] Ошибка записи в таблицу: {e}")

    record["reason"] = reason

    message = await _fetch_message(guild, record.get("channel_id"), record.get("message_id"))
    if message is not None:
        try:
            await message.edit(
                embed=build_application_embed(
                    record,
                    status,
                    decided_by=interaction.user,
                    added=added,
                    skipped=skipped,
                    reason=reason,
                ),
                view=None,
            )
        except discord.HTTPException as e:
            print(f"[ANKETA] Не удалось обновить сообщение заявки: {e}")

    await _notify_user(guild, user_id, status, reason)

    record["status"] = status
    record["decided_by"] = interaction.user.id
    record["decided_at"] = datetime.now(timezone.utc).isoformat()
    data["pending"].pop(app_id, None)
    data["history"][app_id] = record
    save_applications(data)

    summary = "✅ Заявка одобрена." if status == "approved" else "❌ Заявка отклонена."
    if added:
        summary += f" Выдано ролей: {len(added)}."
    if skipped:
        summary += f" Пропущено: {len(skipped)}."
    for note in notes:
        summary += f"\n{note}"
    await interaction.followup.send(summary, ephemeral=True)


# ============================================================================
# КОМАНДЫ
# ============================================================================
async def _check_officer(interaction: discord.Interaction) -> bool:
    if is_officer(interaction.user):
        return True
    await interaction.response.send_message(
        "❌ У вас нет прав для использования этой команды.", ephemeral=True
    )
    return False


def _resolve_channel(
    guild: discord.Guild, interaction: discord.Interaction, channel_id: str | None
) -> discord.TextChannel | None:
    if not channel_id or not str(channel_id).strip().isdigit():
        channel = interaction.channel
    else:
        channel = guild.get_channel(int(str(channel_id).strip()))
    return channel if isinstance(channel, discord.TextChannel) else None


async def _fetch_panel(guild: discord.Guild, state: dict):
    message_id = state.get("panel_message_id")
    channel_id = state.get("panel_channel_id")
    if not message_id or not channel_id:
        return None
    return await _fetch_message(guild, int(channel_id), int(message_id))


def _diagnose_sync() -> list[str]:
    lines: list[str] = []
    path = _service_account_path()
    if path and os.path.exists(path):
        lines.append(f"✅ Ключ сервисного аккаунта: `{path}`")
    else:
        lines.append(f"❌ Ключ сервисного аккаунта не найден: `{path or 'не задан'}`")
        return lines

    if not SPREADSHEET_ID:
        lines.append("❌ SPREADSHEET_ID не задан")
        return lines

    sh = _open_spreadsheet_sync()
    lines.append(f"✅ Таблица открыта: **{sh.title}**")
    lines.append("Листы: " + ", ".join(f"`{ws.title}`" for ws in sh.worksheets()))

    values = sh.worksheet(SHEET_REFERENCE).get_all_values()
    options = parse_options(values)
    for group in ("rank", "spec", "att"):
        items = options.get(group, [])
        with_roles = sum(1 for item in items if item.get("roles"))
        lines.append(f"• `{group}`: вариантов {len(items)}, с ролями {with_roles}")
    return lines


def setup(bot: commands.Bot) -> None:
    print(f"[ANKETA] Версия модуля анкеты: {VERSION}")

    global _OPTIONS
    cached = load_options_cache()
    if cached:
        _OPTIONS = cached

    # Постоянные компоненты, переживают рестарт
    bot.add_view(PanelView())
    for app_id in load_applications()["pending"]:
        try:
            bot.add_view(ApplicationView(app_id))
        except Exception as e:
            print(f"[ANKETA] Не удалось восстановить заявку {app_id}: {e}")

    # ----------------- /ANKETA_POST -----------------
    @bot.tree.command(
        name="anketa_post", description="Опубликовать или обновить панель анкеты"
    )
    @app_commands.describe(
        channel_id="Канал для панели (по умолчанию — текущий)",
        confirm="Подтвердить перенос панели в другой канал",
    )
    async def anketa_post(
        interaction: discord.Interaction,
        channel_id: str | None = None,
        confirm: bool = False,
    ) -> None:
        if not await _check_officer(interaction):
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        channel = _resolve_channel(guild, interaction, channel_id)
        if channel is None:
            await interaction.response.send_message(
                "❌ Не удалось определить канал для панели.", ephemeral=True
            )
            return
        if not channel.permissions_for(guild.me).send_messages:
            await interaction.response.send_message(
                f"❌ У бота нет права `Send Messages` в канале {channel.mention}.",
                ephemeral=True,
            )
            return

        state = load_state()
        existing = await _fetch_panel(guild, state)

        if existing is not None and existing.channel.id == channel.id:
            await existing.edit(embed=build_panel_embed(), view=PanelView())
            await interaction.response.send_message(
                f"✅ Панель обновлена на месте: {existing.jump_url}", ephemeral=True
            )
            return

        if existing is not None and not confirm:
            await interaction.response.send_message(
                f"⚠️ Панель уже опубликована в {existing.channel.mention} "
                f"({existing.jump_url}).\nПеренос создаст новое сообщение. "
                "Повторите с `confirm: true`.",
                ephemeral=True,
            )
            return

        message = await channel.send(embed=build_panel_embed(), view=PanelView())
        save_state(
            {"panel_message_id": message.id, "panel_channel_id": channel.id}
        )

        if existing is not None:
            try:
                await existing.edit(
                    embed=discord.Embed(
                        title="⚠️ Панель неактивна",
                        description=f"Актуальная панель: {message.jump_url}",
                        color=discord.Color.greyple(),
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass

        await interaction.response.send_message(
            f"✅ Панель опубликована: {message.jump_url}", ephemeral=True
        )

    # ----------------- /ANKETA_RELOAD -----------------
    @bot.tree.command(
        name="anketa_reload", description="Перечитать справочник из Google-таблицы"
    )
    async def anketa_reload(interaction: discord.Interaction) -> None:
        if not await _check_officer(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await refresh_options()
        mark = "✅" if ok else "❌"
        await interaction.edit_original_response(content=f"{mark} {msg}")

    # ----------------- /ANKETA_LIST -----------------
    @bot.tree.command(
        name="anketa_list", description="Показать варианты анкеты и их роли"
    )
    async def anketa_list(interaction: discord.Interaction) -> None:
        if not await _check_officer(interaction):
            return

        guild = interaction.guild
        options = get_options()
        lines = [
            f"ℹ️ **Версия модуля:** `{VERSION}`",
            f"**Вариантов:** звания {len(options.get('rank', []))}, "
            f"специализации {len(options.get('spec', []))}, "
            f"приписки {len(options.get('att', []))}",
        ]

        for group, title in (("rank", "Звания"), ("spec", "Специализации"), ("att", "Приписки")):
            items = options.get(group, [])
            if not items:
                continue
            lines.append(f"\n**{title} ({len(items)}):**")
            for item in items[:30]:
                mentions = []
                for rid in item.get("roles", []):
                    if not str(rid).isdigit():
                        continue
                    role = guild.get_role(int(rid)) if guild else None
                    mentions.append(role.mention if role else f"`{rid}`")
                lines.append(f"• {item['label']} → {', '.join(mentions) or '—'}")

        if not options:
            lines.append(
                "⚠️ Справочник пуст. Выполните `/anketa_reload`."
            )

        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    # ----------------- /ANKETA_DOCTOR -----------------
    @bot.tree.command(
        name="anketa_doctor", description="Диагностика доступа к таблице и ролей"
    )
    async def anketa_doctor(interaction: discord.Interaction) -> None:
        if not await _check_officer(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = [f"ℹ️ **Версия модуля:** `{VERSION}`"]

        try:
            lines.extend(await asyncio.to_thread(_diagnose_sync))
        except Exception as e:
            lines.append(f"❌ Ошибка доступа к таблице: {e}")

        guild = interaction.guild
        if guild is not None:
            channel = (
                guild.get_channel(APPLICATION_CHANNEL_ID)
                if APPLICATION_CHANNEL_ID
                else None
            )
            mark = "✅" if isinstance(channel, discord.TextChannel) else "❌"
            lines.append(
                f"{mark} **Канал заявок:** "
                + (channel.mention if isinstance(channel, discord.TextChannel) else "не задан")
            )
            for label, ids in (
                ("Гость", GUEST_ROLE_IDS),
                ("Близкий гость", CLOSE_GUEST_ROLE_IDS),
            ):
                roles = [guild.get_role(r) for r in ids]
                ok = bool(ids) and all(r is not None for r in roles)
                lines.append(
                    ("✅" if ok else "⚠️")
                    + f" **{label}:** "
                    + (", ".join(r.mention for r in roles if r is not None) or "не заданы")
                )

        await interaction.edit_original_response(content="\n".join(lines)[:1900])
