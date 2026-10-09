import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from config import is_moderator

# Списки-переопределения для формы «Дополнительная роль»:
#   enable  — роли, которые показываем даже если они protected/панель/отпуск;
#   disable — роли, которые скрываем из формы несмотря ни на что.
DATA_PATH = os.getenv("ROLE_OVERRIDES_DATA_PATH", "data/role_overrides.json")

VERSION = "2026-10-09-r1"

ENABLE = "enable"
DISABLE = "disable"


# ----------------- ХРАНИЛИЩЕ -----------------
def _empty_state() -> dict:
    return {ENABLE: [], DISABLE: []}


def _normalize(values) -> list[int]:
    """Приводит список к int без дублей, сохраняя порядок."""
    if not isinstance(values, list):
        return []
    result: list[int] = []
    for value in values:
        text = str(value)
        if text.isdigit() and int(text) not in result:
            result.append(int(text))
    return result


def load_state() -> dict:
    """Возвращает оба списка. Пересечение намеренно схлопывается в disable."""
    state = _empty_state()
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return state
    except (OSError, json.JSONDecodeError) as e:
        print(f"[OVERRIDE] Не удалось прочитать {DATA_PATH}: {e}")
        return state

    if not isinstance(data, dict):
        return state

    state[ENABLE] = _normalize(data.get(ENABLE))
    state[DISABLE] = _normalize(data.get(DISABLE))

    overlap = [rid for rid in state[ENABLE] if rid in state[DISABLE]]
    if overlap:
        print(
            f"[OVERRIDE] Роли в обоих списках (оставлены в disable): {overlap}"
        )
        state[ENABLE] = [rid for rid in state[ENABLE] if rid not in state[DISABLE]]
    return state


def save_state(state: dict) -> None:
    directory = os.path.dirname(DATA_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {ENABLE: _normalize(state.get(ENABLE)), DISABLE: _normalize(state.get(DISABLE))}
    tmp_path = f"{DATA_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_PATH)


def enabled_role_ids() -> set[int]:
    return set(load_state()[ENABLE])


def disabled_role_ids() -> set[int]:
    return set(load_state()[DISABLE])


def check_data_path() -> tuple[bool, str]:
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


# ----------------- ВСПОМОГАТЕЛЬНОЕ -----------------
def _can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return me is not None and me.top_role.position > role.position


async def _check_moderator(interaction: discord.Interaction) -> bool:
    if is_moderator(interaction.user):
        return True
    await interaction.response.send_message(
        "❌ У вас нет прав для использования этой команды.",
        ephemeral=True,
    )
    return False


async def _require_guild(interaction: discord.Interaction) -> discord.Guild | None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Команда должна выполняться на сервере.", ephemeral=True
        )
        return None
    return interaction.guild


def _enable_note(guild: discord.Guild, role: discord.Role) -> str:
    """Пометка о том, что роль в enable не попадёт в форму, несмотря на настройку."""
    if role.is_default():
        return "\n⚠️ Это @everyone — роль не показывается в форме."
    if role.managed:
        return "\n⚠️ Это managed-роль (выдаётся интеграцией) — в форме не покажется."
    if not _can_manage_role(guild, role):
        return (
            "\n⚠️ Роль выше роли бота — в форме не покажется, пока роль бота не поднята."
        )
    return ""


# ----------------- КОМАНДЫ -----------------
def setup(bot: commands.Bot) -> None:
    print(f"[OVERRIDE] Версия модуля переопределений ролей: {VERSION}")

    # ----------------- /ROLES_ENABLE_ADD -----------------
    @bot.tree.command(
        name="roles_enable_add",
        description="Показывать роль в форме «Дополнительная роль» несмотря на фильтры",
    )
    @app_commands.describe(role="Роль, которую обязательно показывать в форме")
    async def roles_enable_add(
        interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not await _check_moderator(interaction):
            return
        guild = await _require_guild(interaction)
        if guild is None:
            return

        state = load_state()
        if role.id in state[ENABLE]:
            await interaction.response.send_message(
                f"ℹ️ Роль {role.mention} уже в списке enable.", ephemeral=True
            )
            return
        if role.id in state[DISABLE]:
            await interaction.response.send_message(
                f"❌ Роль {role.mention} уже в списке disable. "
                "Сначала уберите её: `/roles_disable_remove`.",
                ephemeral=True,
            )
            return

        state[ENABLE].append(role.id)
        save_state(state)

        await interaction.response.send_message(
            f"✅ Роль {role.mention} добавлена в enable (всего: {len(state[ENABLE])})."
            + _enable_note(guild, role),
            ephemeral=True,
        )

    # ----------------- /ROLES_ENABLE_REMOVE -----------------
    @bot.tree.command(
        name="roles_enable_remove",
        description="Убрать роль из списка enable",
    )
    @app_commands.describe(role="Роль, которую убрать из enable")
    async def roles_enable_remove(
        interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not await _check_moderator(interaction):
            return

        state = load_state()
        if role.id not in state[ENABLE]:
            await interaction.response.send_message(
                f"⚠️ Роли {role.mention} нет в списке enable.", ephemeral=True
            )
            return

        state[ENABLE].remove(role.id)
        save_state(state)

        await interaction.response.send_message(
            f"✅ Роль {role.mention} убрана из enable (осталось: {len(state[ENABLE])}).",
            ephemeral=True,
        )

    # ----------------- /ROLES_DISABLE_ADD -----------------
    @bot.tree.command(
        name="roles_disable_add",
        description="Скрыть роль из формы «Дополнительная роль» несмотря ни на что",
    )
    @app_commands.describe(role="Роль, которую надо скрыть из формы")
    async def roles_disable_add(
        interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not await _check_moderator(interaction):
            return

        state = load_state()
        if role.id in state[DISABLE]:
            await interaction.response.send_message(
                f"ℹ️ Роль {role.mention} уже в списке disable.", ephemeral=True
            )
            return
        if role.id in state[ENABLE]:
            await interaction.response.send_message(
                f"❌ Роль {role.mention} уже в списке enable. "
                "Сначала уберите её: `/roles_enable_remove`.",
                ephemeral=True,
            )
            return

        state[DISABLE].append(role.id)
        save_state(state)

        await interaction.response.send_message(
            f"✅ Роль {role.mention} добавлена в disable (всего: {len(state[DISABLE])}).",
            ephemeral=True,
        )

    # ----------------- /ROLES_DISABLE_REMOVE -----------------
    @bot.tree.command(
        name="roles_disable_remove",
        description="Убрать роль из списка disable",
    )
    @app_commands.describe(role="Роль, которую убрать из disable")
    async def roles_disable_remove(
        interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not await _check_moderator(interaction):
            return

        state = load_state()
        if role.id not in state[DISABLE]:
            await interaction.response.send_message(
                f"⚠️ Роли {role.mention} нет в списке disable.", ephemeral=True
            )
            return

        state[DISABLE].remove(role.id)
        save_state(state)

        await interaction.response.send_message(
            f"✅ Роль {role.mention} убрана из disable (осталось: {len(state[DISABLE])}).",
            ephemeral=True,
        )

    # ----------------- /ROLES_STATUS_LIST -----------------
    @bot.tree.command(
        name="roles_status_list",
        description="Показать списки enable/disable для формы «Дополнительная роль»",
    )
    async def roles_status_list(interaction: discord.Interaction) -> None:
        if not await _check_moderator(interaction):
            return
        guild = await _require_guild(interaction)
        if guild is None:
            return

        state = load_state()
        lines = [f"ℹ️ **Версия модуля:** `{VERSION}`"]

        def render(title: str, role_ids: list[int], empty_hint: str) -> None:
            lines.append(f"\n**{title} ({len(role_ids)}):**")
            if not role_ids:
                lines.append(f"• {empty_hint}")
                return
            for role_id in role_ids:
                role = guild.get_role(role_id)
                if role is None:
                    lines.append(f"• <@&{role_id}> — ⚠️ роль не найдена на сервере")
                    continue
                note = ""
                if role.is_default():
                    note = " — ⚠️ @everyone"
                elif role.managed:
                    note = " — ⚠️ managed-роль"
                elif not _can_manage_role(guild, role):
                    note = " — ⚠️ выше роли бота"
                lines.append(f"• {role.mention}{note}")

        render(
            "✅ enable — показывать в форме",
            state[ENABLE],
            "пусто. Добавьте роли через `/roles_enable_add`.",
        )
        render(
            "⛔ disable — скрывать из формы",
            state[DISABLE],
            "пусто. Добавьте роли через `/roles_disable_add`.",
        )

        ok, note = check_data_path()
        lines.append(("\n✅" if ok else "\n❌") + f" **Файл:** {note}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)
