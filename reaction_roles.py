import asyncio
import json
import os
import re

import discord
from discord import app_commands
from discord.ext import commands

import applications
from config import GUILD_ID, is_moderator, is_officer

DATA_PATH = os.getenv("REACTION_ROLES_DATA_PATH", "data/reaction_roles.json")

# Путь к состоянию старой (отдельной) панели анкеты — для одноразовой миграции.
LEGACY_ANKETA_STATE_PATH = os.getenv(
    "ANKETA_STATE_PATH", os.path.join("data", "anketa_state.json")
)

# Маркер версии модуля. Печатается при старте и виден в /doctor.
# Нужен, чтобы отличать «баг в коде» от «контейнер собран из старого образа».
PANEL_VERSION = "2026-10-08-r6-merged-anketa-panel"

# Пауза между изменениями ролей, чтобы не упереться в rate limit на больших серверах
SYNC_DELAY = 0.5

# Лимит Discord на количество embed-полей
MAX_PANEL_FIELDS = 25

# Блокировки на пользователя: без них быстрый спам реакцией даёт
# конкурентные add_roles/remove_roles по одному участнику
_locks: dict[int, asyncio.Lock] = {}


def _get_lock(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[user_id] = lock
    return lock


# ----------------- ХРАНИЛИЩЕ -----------------
def _empty_state() -> dict:
    return {"message_id": None, "channel_id": None, "roles": {}}


def load_state() -> dict:
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return _empty_state()
    except (OSError, json.JSONDecodeError) as e:
        print(f"[PANEL] Не удалось прочитать {DATA_PATH}: {e}")
        return _empty_state()

    state = _empty_state()
    if data.get("message_id"):
        state["message_id"] = str(data["message_id"])
    if data.get("channel_id"):
        state["channel_id"] = str(data["channel_id"])
    roles = data.get("roles")
    if isinstance(roles, dict):
        # Ключи нормализуем так же, как emoji_key: иначе старые записи с
        # U+FE0F перестанут совпадать с эмодзи из реакции
        state["roles"] = {
            _normalize_unicode_emoji(str(key)): str(value)
            for key, value in roles.items()
            if str(value).isdigit()
        }
    return state


def save_state(state: dict) -> None:
    directory = os.path.dirname(DATA_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{DATA_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_PATH)


# ----------------- РАБОТА С ЭМОДЗИ -----------------
def emoji_key(emoji) -> str:
    """Единый ключ эмодзи: обычный эмодзи — символ, кастомный — <:name:id>.

    Ключи должны совпадать в raw-событии и в message.reactions, иначе
    маппинг молча перестанет работать.

    ВАЖНО: типы на входе разные. В raw-событии `payload.emoji` — это
    PartialEmoji, а вот у `Reaction.emoji` обычный юникод-эмодзи приходит
    ГОЛОЙ СТРОКОЙ (discord/state.py, `get_reaction_emoji`) и атрибута
    `.name` у неё нет.
    """
    if isinstance(emoji, str):
        return _normalize_unicode_emoji(emoji)
    emoji_id = getattr(emoji, "id", None)
    if emoji_id:
        return f"<:{emoji.name}:{emoji_id}>"
    return _normalize_unicode_emoji(emoji.name)


# U+FE0F — невидимый «селектор вариации». Discord и разные клиенты ставят
# его не всегда: тот же 🍕 может прийти как с ним, так и без. Без снятия
# ключ привязки и ключ реакции расходятся, и роль не выдаётся.
_VARIATION_SELECTOR = "\ufe0f"


def _normalize_unicode_emoji(text: str) -> str:
    return text.replace(_VARIATION_SELECTOR, "")


# Discord отдаёт кастомные эмодзи как <:name:id> и <a:name:id> (анимированные).
# Двоеточие после < сделано необязательным — принимаем и голый <name:id>.
_CUSTOM_EMOJI_RE = re.compile(r"^<?a?:?([A-Za-z0-9_]{2,32}):(\d{17,20})>?$")

_EMOJI_HINT = "Вставьте сам эмодзи (например 🍕) или скопируйте кастомный из сообщения."


async def normalize_emoji_input(bot, raw: str) -> tuple[str | None, str | None]:
    """Проверяет ввод эмодзи. Возвращает (ключ, ошибка).

    Без валидации ввод вида ':pizza:' создал бы ключ, который не совпадёт
    ни с одной реальной реакцией, и роли не выдавались бы молча.
    """
    text = (raw or "").strip()
    if not text:
        return None, "Эмодзи не указан."

    match = _CUSTOM_EMOJI_RE.fullmatch(text)
    if match:
        emoji_id = int(match.group(2))
        cached = bot.get_emoji(emoji_id)
        if cached is None:
            return None, f"Кастомный эмодзи {text} не найден. Бот не видит его на сервере."
        return emoji_key(cached), None

    # Отсекаем заведомо не-эмодзи: текстовые токены, пробелы, слишком длинный ввод
    if text.isascii() or len(text) > 40 or any(char.isspace() for char in text):
        return None, f"Это не эмодзи. {_EMOJI_HINT}"

    # Нормализуем так же, как emoji_key, иначе ключ привязки и ключ реакции
    # разойдутся на невидимом U+FE0F
    return _normalize_unicode_emoji(text), None


# ----------------- ОТРИСОВКА ПАНЕЛИ -----------------
def build_panel_embed(guild: discord.Guild, state: dict) -> discord.Embed:
    """Объединённая панель: описание кнопок анкеты + роли из маппинга.

    Текст (заголовок/описание/футер) берём из анкеты, чтобы правки делались
    в одном месте; роли автоматически досыпаются полями.
    """
    embed = applications.build_panel_embed()
    for key, role_id_str in state["roles"].items():
        role = guild.get_role(int(role_id_str))
        value = role.mention if role is not None else "⚠️ роль не найдена на сервере"
        embed.add_field(name=f"{key} Роль", value=value, inline=True)
    return embed


def build_inactive_embed(mention: str | None) -> discord.Embed:
    description = "Эта панель больше не активна — нажатия на реакции ничего не делают."
    if mention:
        description += f"\n\nАктуальная панель: {mention}"
    return discord.Embed(
        title="⚠️ Панель неактивна",
        description=description,
        color=discord.Color.greyple(),
    )


async def ensure_reactions(message: discord.Message, state: dict) -> None:
    for key in state["roles"]:
        try:
            await message.add_reaction(key)
        except discord.HTTPException as e:
            print(f"[PANEL] Не удалось поставить реакцию {key}: {e}")


async def render_panel(message: discord.Message, state: dict) -> None:
    """Обновляет текст панели и досыпает недостающие реакции.

    Реакции живут на сообщении, а message.edit() их не трогает —
    поэтому правка маппинга безопасна для уже выданных ролей. Панель
    рендерится даже без привязок: кнопки анкеты должны работать всегда.
    """
    await message.edit(embed=build_panel_embed(message.guild, state))
    await ensure_reactions(message, state)


def panel_matches(message: discord.Message, state: dict) -> bool:
    """Сверяет только те поля, что Discord возвращает дословно.

    Полное сравнение embed.to_dict() не годится: Discord нормализует
    структуру, и наша же правка порождала бы следующую правку по кругу.
    """
    if not message.embeds:
        return False
    embed = message.embeds[0]
    base = applications.build_panel_embed()
    if embed.title != base.title or embed.description != base.description:
        return False
    expected = [f"{key} Роль" for key in state["roles"]]
    return [field.name for field in embed.fields] == expected


async def mark_panel_inactive(message: discord.Message, new_panel_url: str) -> None:
    try:
        await message.edit(embed=build_inactive_embed(new_panel_url), view=None)
    except discord.HTTPException as e:
        print(f"[PANEL] Не удалось пометить старую панель неактивной: {e}")


# ----------------- ПРИМЕНЕНИЕ РОЛЕЙ -----------------
def _managed_role_ids(state: dict) -> set[int]:
    return {int(role_id_str) for role_id_str in state["roles"].values()}


def _protected_role_ids() -> set[int]:
    """Роли, которые панель не должна снимать: стартовые, анкеты и гостей."""
    try:
        return applications.protected_role_ids()
    except Exception:
        return set()


def _desired_role_ids(state: dict, emoji_keys: set[str]) -> set[int]:
    mapping = state["roles"]
    return {int(mapping[key]) for key in emoji_keys if key in mapping}


async def _collect_emoji_keys(
    state: dict, message: discord.Message, only_user_id: int | None = None
) -> dict[int, set[str]]:
    """Собирает user_id -> набор ключей эмодзи, на которые он отреагировал.

    Интересуют только эмодзи из маппинга — это ограничивает и лишние
    API-запросы, и объём работы на панели с посторонними реакциями.

    only_user_id нужен на горячем пути (клик по реакции): перебирать всех
    участников каждой реакции ради одного человека — лишние вызовы API.
    """
    mapping = state["roles"]
    result: dict[int, set[str]] = {}
    for reaction in message.reactions:
        key = emoji_key(reaction.emoji)
        if key not in mapping:
            continue
        try:
            # ВАЖНО: limit=None значит «все». limit=0 в discord.py — это ноль
            # элементов (`while limit > 0`), то есть пустой результат.
            async for user in reaction.users(limit=None):
                if user.bot:
                    continue
                result.setdefault(user.id, set()).add(key)
                if only_user_id is not None and user.id == only_user_id:
                    break
        except discord.HTTPException as e:
            print(f"[PANEL] Не удалось получить участников реакции {key}: {e}")
            continue
    return result


def _can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return me is not None and me.top_role.position > role.position


async def _apply_roles(
    guild: discord.Guild,
    member: discord.Member,
    desired_role_ids: set[int],
    managed_role_ids: set[int],
) -> tuple[list[str], list[str]]:
    """Единая точка выдачи/снятия ролей для участника.

    Роли вне панели (managed_role_ids) не трогает.
    """
    current_managed = {role.id for role in member.roles if role.id in managed_role_ids}

    to_add = []
    for role_id in desired_role_ids - current_managed:
        role = guild.get_role(role_id)
        if role is None:
            print(f"[PANEL] Роль {role_id} не найдена на сервере {guild.id}")
            continue
        if not _can_manage_role(guild, role):
            print(f"[PANEL] Роль {role.name} выше роли бота — выдача невозможна")
            continue
        to_add.append(role)

    to_remove = [
        role
        for role in member.roles
        if role.id in managed_role_ids
        and role.id not in desired_role_ids
        and role.id not in _protected_role_ids()
    ]

    added: list[str] = []
    removed: list[str] = []

    if to_add:
        try:
            await member.add_roles(*to_add, reason="Панель ролей: реакция на сообщении")
            added = [role.mention for role in to_add]
        except discord.Forbidden:
            print(f"[PANEL] Нет прав на выдачу ролей участнику {member.id}")
        except discord.HTTPException as e:
            print(f"[PANEL] Ошибка выдачи ролей участнику {member.id}: {e}")

    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Панель ролей: реакция снята")
            removed = [role.mention for role in to_remove]
        except discord.Forbidden:
            print(f"[PANEL] Нет прав на снятие ролей участника {member.id}")
        except discord.HTTPException as e:
            print(f"[PANEL] Ошибка снятия ролей участника {member.id}: {e}")

    return added, removed


# ----------------- СОБЫТИЯ РЕАКЦИЙ -----------------
def check_data_path() -> tuple[bool, str]:
    """Проверяет, что файл состояния доступен и в него можно писать."""
    directory = os.path.dirname(DATA_PATH) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        return False, f"не удалось создать каталог `{directory}`: {e}"

    probe = os.path.join(directory, ".write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as file:
            file.write("ok")
        os.remove(probe)
    except OSError as e:
        return False, f"каталог `{directory}` недоступен для записи: {e}"

    return True, f"`{os.path.abspath(DATA_PATH)}` доступен для записи"


async def _handle_reaction(bot, payload) -> None:
    """Один обработчик и на добавление, и на снятие реакции.

    Тип события не используется: состояние перечитывается с самого сообщения,
    поэтому порядок доставки событий не влияет на результат. Заодно это
    корректно обрабатывает случай, когда на одну роль ведут два эмодзи —
    роль снимается, только если активных эмодзи для неё не осталось вовсе.

    Молчаливых выходов почти нет: если панель не настроена, это пишется в лог.
    Единственный немой гард — «реакция не на нашем сообщении»: он срабатывает
    на каждую реакцию в сервере и в логе превратился бы в поток мусора.
    """
    state = load_state()
    message_id = state["message_id"]
    if not message_id or not state["roles"]:
        print(
            "[PANEL] Реакция проигнорирована: панель не настроена "
            f"(message_id={message_id}, ролей в маппинге={len(state['roles'])}). "
            "Выполните /reactionroles_add и /anketa_post."
        )
        return
    if str(payload.message_id) != message_id:
        return
    if bot.user is not None and payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        print(f"[PANEL] Реакция проигнорирована: сервер {payload.guild_id} не найден в кэше")
        return

    key = emoji_key(payload.emoji)
    if key not in state["roles"]:
        print(
            f"[PANEL] Реакция {key!r} на панели не привязана к роли.\n"
            f"        В маппинге сейчас: {list(state['roles'])!r}\n"
            f"        (repr показывает невидимые символы — так видно расхождение ключей)"
        )
        return

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        print(f"[PANEL] Реакция проигнорирована: канал {payload.channel_id} не найден")
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        print(
            f"[PANEL] Сообщение-панель {payload.message_id} не найдено (удалено?)"
        )
        return
    except discord.HTTPException as e:
        print(f"[PANEL] Ошибка загрузки сообщения-панели: {e}")
        return

    async with _get_lock(payload.user_id):
        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(
                payload.user_id
            )
        except discord.NotFound:
            print(f"[PANEL] Участник {payload.user_id} не найден на сервере")
            return
        except discord.HTTPException as e:
            print(f"[PANEL] Не удалось получить участника {payload.user_id}: {e}")
            return

        keys = (await _collect_emoji_keys(state, message, only_user_id=member.id)).get(
            member.id, set()
        )
        desired = _desired_role_ids(state, keys)
        managed = _managed_role_ids(state)
        added, removed = await _apply_roles(guild, member, desired, managed)

        print(
            f"[PANEL] {member} нажал {key or '—'}; активные эмодзи: "
            f"{sorted(keys) or 'нет'}; "
            f"выдано: {', '.join(added) or '—'}; снято: {', '.join(removed) or '—'}"
        )


# ----------------- СИНХРОНИЗАЦИЯ -----------------
async def _fetch_panel(bot, state: dict) -> tuple[discord.Message | None, str]:
    """Возвращает (сообщение-панель, статус): ok | no-panel | broken | error."""
    if not state["message_id"]:
        return None, "no-panel"

    guild = bot.get_guild(GUILD_ID)
    if guild is None or not state["channel_id"]:
        return None, "error"

    channel = guild.get_channel(int(state["channel_id"]))
    if channel is None:
        print(f"[PANEL] Канал панели {state['channel_id']} не найден")
        return None, "error"

    try:
        message = await channel.fetch_message(int(state["message_id"]))
    except discord.NotFound:
        print(f"[PANEL] Сообщение-панель {state['message_id']} удалено")
        return None, "broken"
    except discord.HTTPException as e:
        print(f"[PANEL] Ошибка загрузки панели: {e}")
        return None, "error"
    return message, "ok"


async def sync_panel(bot) -> str:
    """Сверяет роли участников с текущими реакциями на панели.

    Возвращает: skipped | broken | anomaly | ok
    """
    state = load_state()
    if not state["message_id"] or not state["roles"]:
        print(
            "[PANEL] Синхронизация пропущена: панель не настроена "
            f"(message_id={state['message_id']}, ролей в маппинге={len(state['roles'])})"
        )
        return "skipped"

    message, status = await _fetch_panel(bot, state)
    if status != "ok" or message is None:
        print(f"[PANEL] Синхронизация невозможна, статус панели: {status}")
        return "broken" if status == "broken" else "skipped"

    guild = message.guild
    managed = _managed_role_ids(state)
    keys_by_user = await _collect_emoji_keys(state, message)
    desired_by_user = {
        user_id: _desired_role_ids(state, keys) for user_id, keys in keys_by_user.items()
    }

    # Защита: панель без единой реакции при наличии выданных ролей почти
    # наверняка означает ручную очистку реакций. Снятие пропускаем,
    # роли не разлетаются молча — единственное место, где решение
    # принимается не в пользу пользователя.
    total_reactions = sum(reaction.count for reaction in message.reactions)
    if total_reactions == 0:
        holders = sum(
            1 for member in guild.members if any(r.id in managed for r in member.roles)
        )
        if holders:
            print(
                f"[PANEL] ВНИМАНИЕ: на панели 0 реакций, но роли у {holders} участников. "
                "Снятие пропущено — вероятно, реакции очистили вручную."
            )
            return "anomaly"

    changed = 0
    for member in guild.members:
        desired = desired_by_user.get(member.id, set())
        current_managed = {role.id for role in member.roles if role.id in managed}
        if desired == current_managed:
            continue

        async with _get_lock(member.id):
            added, removed = await _apply_roles(guild, member, desired, managed)
        if added or removed:
            changed += 1
            print(
                f"[PANEL] Синхронизация {member.id}: +{len(added)} -{len(removed)}"
            )
            await asyncio.sleep(SYNC_DELAY)

    print(f"[PANEL] Синхронизация завершена, изменено участников: {changed}")
    return "ok"


# ----------------- ХЕЛПЕРЫ КОМАНД -----------------
async def _check_moderator(interaction: discord.Interaction) -> bool:
    if is_moderator(interaction.user):
        return True
    await interaction.response.send_message(
        "❌ У вас нет прав для использования этой команды.",
        ephemeral=True,
    )
    return False


async def _check_officer(interaction: discord.Interaction) -> bool:
    if is_officer(interaction.user):
        return True
    await interaction.response.send_message(
        "❌ У вас нет прав для использования этой команды.",
        ephemeral=True,
    )
    return False


async def _deactivate_legacy_anketa_panel(
    guild: discord.Guild, keep_message_id: int | None = None
) -> None:
    """Одноразово гасит старую отдельную панель анкеты после слияния.

    Раньше анкета публиковалась своим сообщением и хранилась в anketa_state.json.
    После перехода на единую панель старый файл больше не нужен: помечаем его
    сообщение неактивным и удаляем файл, чтобы не осталось «висящей» панели.
    """
    try:
        with open(LEGACY_ANKETA_STATE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return

    if isinstance(data, dict):
        message_id = data.get("panel_message_id")
        channel_id = data.get("panel_channel_id")
        if message_id and channel_id and str(message_id) != str(keep_message_id):
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(int(message_id))
                    await mark_panel_inactive(message, None)
                except discord.HTTPException:
                    pass

    try:
        os.remove(LEGACY_ANKETA_STATE_PATH)
    except OSError:
        pass


def _resolve_channel(
    guild: discord.Guild, interaction: discord.Interaction, channel_id: str | None
) -> discord.TextChannel | None:
    if not channel_id or not channel_id.strip().isdigit():
        channel = interaction.channel
    else:
        channel = guild.get_channel(int(channel_id.strip()))

    if not isinstance(channel, discord.TextChannel):
        return None
    return channel


# ----------------- SLASH-КОМАНДЫ -----------------
def setup(bot: commands.Bot) -> None:
    print(f"[PANEL] Версия модуля панели ролей: {PANEL_VERSION}")

    @bot.event
    async def on_raw_reaction_add(payload):
        await _handle_reaction(bot, payload)

    @bot.event
    async def on_raw_reaction_remove(payload):
        await _handle_reaction(bot, payload)

    @bot.event
    async def on_raw_message_delete(payload):
        state = load_state()
        if state["message_id"] and str(payload.message_id) == state["message_id"]:
            print(
                f"[PANEL] Сообщение-панель {payload.message_id} удалено. "
                "Выдача ролей по реакции не работает до /anketa_post."
            )

    @bot.event
    async def on_raw_message_edit(payload):
        state = load_state()
        if not state["message_id"]:
            return
        if str(payload.message_id) != state["message_id"]:
            return
        message, status = await _fetch_panel(bot, state)
        if status != "ok" or message is None:
            return
        # Восстанавливаем, если поехал текст/поля или пропали кнопки анкеты.
        if panel_matches(message, state) and message.components:
            return
        try:
            await message.edit(
                embed=build_panel_embed(message.guild, state),
                view=applications.PanelView(),
            )
            print(
                f"[PANEL] Панель {payload.message_id} отредактирована вручную, "
                "восстановлена"
            )
        except discord.HTTPException as e:
            print(f"[PANEL] Не удалось восстановить панель: {e}")

    # ----------------- /REACTIONROLES_ADD -----------------
    @bot.tree.command(
        name="reactionroles_add", description="Привязать эмодзи к роли в панели"
    )
    @app_commands.describe(
        emoji="Эмодзи: вставьте символ или скопируйте кастомный",
        role="Роль, которая будет выдаваться",
    )
    async def add_binding(
        interaction: discord.Interaction, emoji: str, role: discord.Role
    ) -> None:
        if not await _check_moderator(interaction):
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        key, error = await normalize_emoji_input(bot, emoji)
        if error:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return

        if not _can_manage_role(guild, role):
            await interaction.response.send_message(
                f"❌ Роль {role.mention} находится выше роли бота. "
                "Поднимите роль бота в настройках сервера.",
                ephemeral=True,
            )
            return

        state = load_state()
        if key not in state["roles"] and len(state["roles"]) >= MAX_PANEL_FIELDS:
            await interaction.response.send_message(
                f"❌ В панели уже {MAX_PANEL_FIELDS} эмодзи — это лимит Discord.",
                ephemeral=True,
            )
            return

        previous = state["roles"].get(key)
        state["roles"][key] = str(role.id)
        save_state(state)

        message, status = await _fetch_panel(bot, state)
        if status == "ok" and message is not None:
            try:
                await render_panel(message, state)
            except discord.HTTPException as e:
                print(f"[PANEL] Ошибка обновления панели: {e}")

        if previous and previous != str(role.id):
            old_role = guild.get_role(int(previous))
            was = old_role.mention if old_role else f"роль {previous}"
            text = f"✅ Эмодзи {key} теперь привязан к {role.mention} (было: {was})."
        else:
            text = f"✅ Эмодзи {key} привязан к {role.mention}."

        if role.id in _protected_role_ids():
            text += (
                "\n⚠️ Роль защищена (входит в стартовый комплект `/startroles_list` "
                "или в справочник анкеты `/anketa_list`). Панель не будет снимать "
                "её за отсутствие реакции."
            )

        if not state["message_id"] or status != "ok":
            text += "\nℹ️ Панель не опубликована — выполните `/anketa_post`."

        await interaction.response.send_message(text, ephemeral=True)

    # ----------------- /REACTIONROLES_REMOVE -----------------
    @bot.tree.command(
        name="reactionroles_remove", description="Отвязать эмодзи от роли в панели"
    )
    @app_commands.describe(
        emoji="Эмодзи, который нужно отвязать",
        revoke="Снять роль со всех участников, у кого она есть",
    )
    async def remove_binding(
        interaction: discord.Interaction, emoji: str, revoke: bool = False
    ) -> None:
        if not await _check_moderator(interaction):
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        state = load_state()
        key, error = await normalize_emoji_input(bot, emoji)
        if error:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        if key not in state["roles"]:
            await interaction.response.send_message(
                f"⚠️ Эмодзи {key} не привязан ни к одной роли.", ephemeral=True
            )
            return

        role_id = int(state["roles"].pop(key))
        save_state(state)

        role = guild.get_role(role_id)
        revoked = 0
        if revoke and role is not None:
            for member in guild.members:
                if role not in member.roles:
                    continue
                try:
                    await member.remove_roles(
                        role, reason="Панель ролей: эмодзи отвязан"
                    )
                    revoked += 1
                    await asyncio.sleep(SYNC_DELAY)
                except discord.Forbidden:
                    print(f"[PANEL] Нет прав на снятие роли у {member.id}")
                except discord.HTTPException as e:
                    print(f"[PANEL] Ошибка снятия роли у {member.id}: {e}")

        message, status = await _fetch_panel(bot, state)
        if status == "ok" and message is not None:
            try:
                await render_panel(message, state)
            except discord.HTTPException as e:
                print(f"[PANEL] Ошибка обновления панели: {e}")

        text = f"✅ Эмодзи {key} отвязан от {role.mention if role else f'роли {role_id}'}."
        if revoke:
            text += f" Роль снята у {revoked} участников."
        else:
            text += " Роли у участников остались — для отзыва используйте `revoke: true`."

        await interaction.response.send_message(text, ephemeral=True)

    # ----------------- /ANKETA_POST (единая панель) -----------------
    @bot.tree.command(
        name="anketa_post",
        description="Создать или обновить единую панель: анкета и выбор ролей",
    )
    @app_commands.describe(
        channel_id="Канал для панели (по умолчанию — текущий канал)",
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
        if not guild:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        state = load_state()

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

        existing, status = await _fetch_panel(bot, state)

        # Панель жива в том же канале — идемпотентно обновляем на месте.
        # Это защищает от единственного разрушительного сценария: пересоздания
        # сообщения, при котором слетают все реакции и sync снимает роли у людей.
        if status == "ok" and existing is not None and existing.channel.id == channel.id:
            await existing.edit(
                embed=build_panel_embed(guild, state),
                view=applications.PanelView(),
            )
            await ensure_reactions(existing, state)
            await _deactivate_legacy_anketa_panel(guild, existing.id)
            await interaction.response.send_message(
                f"✅ Панель обновлена на месте: {existing.jump_url}", ephemeral=True
            )
            return

        # Панель жива, но канал другой — единственный случай, где нужно подтверждение
        if status == "ok" and existing is not None and not confirm:
            await interaction.response.send_message(
                f"⚠️ Панель уже опубликована в {existing.channel.mention} "
                f"({existing.jump_url}).\n"
                "Перенос в другой канал создаст новое сообщение и "
                "сбросит все реакции. Повторите с `confirm: true`.",
                ephemeral=True,
            )
            return

        new_message = await channel.send(
            embed=build_panel_embed(guild, state),
            view=applications.PanelView(),
        )
        state["message_id"] = str(new_message.id)
        state["channel_id"] = str(channel.id)
        save_state(state)

        await ensure_reactions(new_message, state)

        if existing is not None:
            await mark_panel_inactive(existing, new_message.jump_url)

        await _deactivate_legacy_anketa_panel(guild, new_message.id)

        await interaction.response.send_message(
            f"✅ Панель опубликована: {new_message.jump_url}", ephemeral=True
        )

    # ----------------- /REACTIONROLES_LIST -----------------
    @bot.tree.command(
        name="reactionroles_list", description="Показать текущее состояние панели"
    )
    async def list_panel(interaction: discord.Interaction) -> None:
        if not await _check_moderator(interaction):
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        state = load_state()
        lines = []

        if not state["roles"]:
            lines.append("⚠️ Маппинг пуст. Добавьте роли через `/reactionroles_add`.")
        else:
            lines.append(f"**Маппинг ({len(state['roles'])}):**")
            for key, role_id_str in state["roles"].items():
                role = guild.get_role(int(role_id_str))
                if role is None:
                    lines.append(f"• {key} → ⚠️ роль {role_id_str} не найдена")
                    continue
                note = ""
                if role.id in applications.start_role_ids():
                    note = " ⚠️ входит в начальный комплект `/startroles_list`"
                lines.append(f"• {key} → {role.mention}{note}")

        message, status = await _fetch_panel(bot, state)
        if status == "ok" and message is not None:
            lines.append(f"\n**Панель:** жива — {message.jump_url}")
            if message.channel.id != interaction.channel_id:
                lines.append(
                    f"⚠️ Панель в другом канале ({message.channel.mention}). "
                    "Используйте `/anketa_post channel_id: …`."
                )
        elif status == "broken":
            lines.append(
                "\n⚠️ **Панель удалена.** Роли не трогаются. "
                "Восстановить: `/anketa_post`."
            )
        elif status == "error":
            lines.append("\n⚠️ Не удалось определить канал панели — проверьте конфиг.")
        else:
            lines.append("\n**Панель:** не создана. Выполните `/anketa_post`.")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ----------------- /REACTIONROLES_DOCTOR -----------------
    @bot.tree.command(
        name="reactionroles_doctor", description="Диагностика панели: где именно затык"
    )
    async def doctor(interaction: discord.Interaction) -> None:
        if not await _check_moderator(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        lines: list[str] = [f"ℹ️ **Версия модуля:** `{PANEL_VERSION}`"]

        # 1. Файл состояния
        writable, path_note = check_data_path()
        mark = "✅" if writable else "❌"
        lines.append(f"{mark} **Файл состояния:** {path_note}")
        lines.append(
            f"{'✅' if writable else '❌'} **Содержимое:** "
            + json.dumps(load_state(), ensure_ascii=False)[:300]
        )

        # 2. Гильдия
        configured = bot.get_guild(GUILD_ID)
        lines.append(
            f"{'✅' if configured else '❌'} **GUILD_ID={GUILD_ID}:** "
            + ("сервер найден в кэше бота" if configured else "сервер НЕ найден в кэше")
        )

        if guild is None:
            await interaction.edit_original_response(
                content="\n".join(lines) + "\n❌ Команда вне сервера."
            )
            return

        state = load_state()

        # 3. Иерархия ролей — самая частая причина «роль не выдаётся»
        me = guild.me
        if me is not None:
            lines.append(
                f"ℹ️ **Роль бота:** {me.top_role.name} "
                f"(позиция {me.top_role.position})"
            )
        for key, role_id_str in state["roles"].items():
            role = guild.get_role(int(role_id_str))
            if role is None:
                lines.append(f"❌ **Роль для {key}:** {role_id_str} не найдена на сервере")
            elif me is None or me.top_role.position > role.position:
                lines.append(f"✅ **Роль для {key}:** {role.mention} — иерархия в порядке")
            else:
                lines.append(
                    f"❌ **Роль для {key}:** {role.mention} ВЫШЕ роли бота — "
                    "выдача невозможна, поднимите роль бота"
                )

        # 4. Панель
        if not state["message_id"]:
            lines.append("❌ **Панель:** не создана. Выполните `/anketa_post`")
        else:
            channel = (
                guild.get_channel(int(state["channel_id"]))
                if state["channel_id"]
                else None
            )
            if channel is None:
                lines.append(
                    f"❌ **Канал панели** `{state['channel_id']}` не найден на сервере"
                )
            else:
                if guild.me is None:
                    lines.append(
                        f"❌ **Права в {channel.mention}:** бот не участник сервера"
                    )
                else:
                    perms = channel.permissions_for(guild.me)
                    lines.append(
                        f"{'✅' if perms.send_messages else '❌'} **Право Send Messages** "
                        f"в {channel.mention}"
                    )
                    lines.append(
                        f"{'✅' if perms.embed_links else '⚠️'} **Право Embed Links** "
                        f"в {channel.mention}"
                    )
                try:
                    message = await channel.fetch_message(int(state["message_id"]))
                except discord.NotFound:
                    lines.append(
                        f"❌ **Сообщение {state['message_id']} удалено.** "
                        "Восстановить: `/anketa_post`"
                    )
                except discord.HTTPException as e:
                    lines.append(f"❌ **Не удалось загрузить сообщение:** {e}")
                else:
                    total = sum(r.count for r in message.reactions)
                    lines.append(
                        f"✅ **Сообщение-панель:** {message.jump_url} "
                        f"(реакций всего: {total})"
                    )
                    for key in state["roles"]:
                        found = next(
                            (r for r in message.reactions if emoji_key(r.emoji) == key),
                            None,
                        )
                        lines.append(
                            f"{'✅' if found else '⚠️'} **Эмодзи {key}:** "
                            + (f"реакций {found.count}" if found else "реакций нет")
                        )

        # 5. Интенты
        lines.append(
            f"{'✅' if bot.intents.reactions else '❌'} **intent reactions** "
            f"в коде: {bot.intents.reactions}"
        )
        lines.append(
            "ℹ️ Если события реакций не приходят вовсе, проверь "
            "**Developer Portal → Message Reactions Intent** — это настройка "
            "на стороне Discord, её нельзя включить кодом."
        )

        await interaction.edit_original_response(content="\n".join(lines)[:1900])

    # ----------------- /REACTIONROLES_SYNC -----------------
    @bot.tree.command(
        name="reactionroles_sync", description="Сверить роли с текущими реакциями"
    )
    async def sync_command(interaction: discord.Interaction) -> None:
        if not await _check_moderator(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await sync_panel(bot)

        if result == "ok":
            text = "✅ Синхронизация завершена."
        elif result == "anomaly":
            text = (
                "⚠️ На панели нет ни одной реакции, но роли у участников есть.\n"
                "Снятие пропущено — роли не разлетелись.\n"
                "Если так задумано, снимите роли вручную или очистите маппинг."
            )
        elif result == "broken":
            text = (
                "⚠️ Сообщение-панель удалено — роли не трогались.\n"
                "Восстановить: `/anketa_post`."
            )
        else:
            text = "ℹ️ Нечего синхронизировать: панель не создана или маппинг пуст."

        await interaction.edit_original_response(content=text)

    # ----------------- /REACTIONROLES_DELETE -----------------
    @bot.tree.command(
        name="reactionroles_delete",
        description="Полностью удалить панель и очистить настройки",
    )
    @app_commands.describe(
        confirm="Обязательное подтверждение — действие необратимо",
        delete_message="Удалить само сообщение (иначе пометить неактивным)",
        revoke_roles="Снять управляемые роли со всех участников",
    )
    async def delete_panel(
        interaction: discord.Interaction,
        confirm: bool = False,
        delete_message: bool = True,
        revoke_roles: bool = False,
    ) -> None:
        if not await _check_moderator(interaction):
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        state = load_state()
        if not state["message_id"] and not state["roles"]:
            await interaction.response.send_message(
                "ℹ️ Панель и так не настроена — удалять нечего.", ephemeral=True
            )
            return

        if not confirm:
            await interaction.response.send_message(
                "⚠️ Это **необратимо**: панель будет удалена, маппинг очищен.\n"
                + (
                    "⚠️ `revoke_roles: true` дополнительно **снимет роли у всех** "
                    "участников.\n"
                    if revoke_roles
                    else ""
                )
                + "Повторите с `confirm: true`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        managed = _managed_role_ids(state)
        revoked = 0
        revoked_errors = 0

        if revoke_roles and managed:
            for member in guild.members:
                roles = [r for r in member.roles if r.id in managed]
                if not roles:
                    continue
                try:
                    await member.remove_roles(
                        *roles, reason="Панель ролей: удаление панели"
                    )
                    revoked += 1
                    await asyncio.sleep(SYNC_DELAY)
                except discord.Forbidden:
                    revoked_errors += 1
                    print(f"[PANEL] Нет прав на снятие ролей у {member.id}")
                except discord.HTTPException as e:
                    revoked_errors += 1
                    print(f"[PANEL] Ошибка снятия ролей у {member.id}: {e}")

        message, status = await _fetch_panel(bot, state)
        message_note = "сообщения не было"
        if status == "ok" and message is not None:
            try:
                if delete_message:
                    await message.delete()
                    message_note = "сообщение удалено"
                else:
                    await mark_panel_inactive(message, None)
                    message_note = "сообщение помечено неактивным"
            except discord.NotFound:
                message_note = "сообщение уже было удалено"
            except discord.HTTPException as e:
                message_note = f"не удалось обработать сообщение: {e}"
                print(f"[PANEL] Ошибка удаления панели: {e}")
        elif status == "broken":
            message_note = "сообщение уже было удалено"

        save_state(_empty_state())
        print(
            f"[PANEL] Панель удалена ({message_note}); "
            f"роли сняты у {revoked} участников, ошибок: {revoked_errors}"
        )

        lines = [
            "✅ **Панель удалена.**",
            f"• Сообщение: {message_note}",
            "• Маппинг очищен",
        ]
        if revoke_roles:
            lines.append(
                f"• Роли сняты у {revoked} участников"
                + (f", ошибок: {revoked_errors}" if revoked_errors else "")
            )
        else:
            lines.append("• Роли у участников оставлены (`revoke_roles: false`)")
        lines.append("Создать заново: `/anketa_post`.")

        await interaction.edit_original_response(content="\n".join(lines))
