import asyncio
import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from config import START_ROLE_IDS, is_moderator

# Отдельный файл от панели ролей: если хранить стартовые роли в маппинге
# панели, то sync_panel начнёт снимать их как «не имеющие реакции».
DATA_PATH = os.getenv("START_ROLES_DATA_PATH", "data/start_roles.json")

START_VERSION = "2026-09-28-r1"

# Синхронизация по одному участнику, когда снимаем роль у всех
SYNC_DELAY = 0.5


# ----------------- ХРАНИЛИЩЕ -----------------
def load_roles() -> list[int]:
    """Возвращает список ID стартовых ролей. Порядок сохраняется, дубли убираются."""
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        print(f"[START] Не удалось прочитать {DATA_PATH}: {e}")
        return []

    values = data.get("roles")
    if not isinstance(values, list):
        return []

    result: list[int] = []
    for value in values:
        text = str(value)
        if text.isdigit() and int(text) not in result:
            result.append(int(text))
    return result


def save_roles(role_ids: list[int]) -> None:
    directory = os.path.dirname(DATA_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{DATA_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump({"roles": [int(r) for r in role_ids]}, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_PATH)


def seed_from_env() -> None:
    """Первый запуск: переносим START_ROLE_IDS из .env в редактируемый файл.

    Без этого уже выданные роли пришлось бы заводить заново руками.
    """
    if os.path.exists(DATA_PATH) or not START_ROLE_IDS:
        return
    save_roles(START_ROLE_IDS)
    print(
        f"[START] Стартовые роли импортированы из START_ROLE_IDS: {START_ROLE_IDS}"
    )


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


def _panel_role_ids() -> set[int]:
    """Роли панели — для предупреждения о конфликте.

    Импорт локальный: reaction_roles импортирует этот модуль на верхнем
    уровне, поэтому обратный импорт на верхнем уровне дал бы цикл.
    """
    try:
        import reaction_roles

        return {int(v) for v in reaction_roles.load_state()["roles"].values()}
    except Exception:
        return set()


async def _check_moderator(interaction: discord.Interaction) -> bool:
    if is_moderator(interaction.user):
        return True
    await interaction.response.send_message(
        "❌ У вас нет прав для использования этой команды.",
        ephemeral=True,
    )
    return False


# ----------------- КОМАНДЫ -----------------
def setup(bot: commands.Bot) -> None:
    print(f"[START] Версия модуля стартовых ролей: {START_VERSION}")
    seed_from_env()

    # ----------------- /STARTROLES_ADD -----------------
    @bot.tree.command(
        name="startroles_add", description="Добавить роль в начальный комплект"
    )
    @app_commands.describe(role="Роль, которая будет выдаваться при вступлении")
    async def startroles_add(
        interaction: discord.Interaction, role: discord.Role
    ) -> None:
        if not await _check_moderator(interaction):
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        if not _can_manage_role(guild, role):
            await interaction.response.send_message(
                f"❌ Роль {role.mention} находится выше роли бота. "
                "Поднимите роль бота в настройках сервера.",
                ephemeral=True,
            )
            return

        roles = load_roles()
        if role.id in roles:
            await interaction.response.send_message(
                f"ℹ️ Роль {role.mention} уже есть в начальном комплекте.",
                ephemeral=True,
            )
            return

        roles.append(role.id)
        save_roles(roles)

        text = f"✅ Роль {role.mention} добавлена в начальный комплект (всего: {len(roles)})."
        if role.id in _panel_role_ids():
            text += (
                "\n⚠️ Эта роль уже привязана в панели ролей: реакция будет и выдавать "
                "её, и снимать при отсутствии реакции. Оставьте её только в одном месте."
            )
        await interaction.response.send_message(text, ephemeral=True)

    # ----------------- /STARTROLES_REMOVE -----------------
    @bot.tree.command(
        name="startroles_remove", description="Убрать роль из начального комплекта"
    )
    @app_commands.describe(
        role="Роль, которую убрать",
        revoke="Снять эту роль со всех участников, у кого она есть",
    )
    async def startroles_remove(
        interaction: discord.Interaction,
        role: discord.Role,
        revoke: bool = False,
    ) -> None:
        if not await _check_moderator(interaction):
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        roles = load_roles()
        if role.id not in roles:
            await interaction.response.send_message(
                f"⚠️ Роли {role.mention} нет в начальном комплекте.", ephemeral=True
            )
            return

        roles.remove(role.id)
        save_roles(roles)

        if not revoke:
            await interaction.response.send_message(
                f"✅ Роль {role.mention} убрана из комплекта (осталось: {len(roles)}).\n"
                "Роли у участников остались — для отзыва используйте `revoke: true`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        revoked = 0
        errors = 0
        for member in guild.members:
            if role not in member.roles:
                continue
            try:
                await member.remove_roles(role, reason="Начальные роли: роль убрана")
                revoked += 1
                if revoked % 10 == 0:
                    await asyncio.sleep(SYNC_DELAY)
            except discord.Forbidden:
                errors += 1
                print(f"[START] Нет прав на снятие роли у {member.id}")
            except discord.HTTPException as e:
                errors += 1
                print(f"[START] Ошибка снятия роли у {member.id}: {e}")

        print(f"[START] Роль {role.name} снята у {revoked} участников, ошибок: {errors}")
        await interaction.edit_original_response(
            content=(
                f"✅ Роль {role.mention} убрана из комплекта (осталось: {len(roles)}).\n"
                f"Снята у {revoked} участников"
                + (f", ошибок: {errors}" if errors else "") + "."
            )
        )

    # ----------------- /STARTROLES_LIST -----------------
    @bot.tree.command(
        name="startroles_list", description="Показать начальный комплект ролей"
    )
    async def startroles_list(interaction: discord.Interaction) -> None:
        if not await _check_moderator(interaction):
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Команда должна выполняться на сервере.", ephemeral=True
            )
            return

        roles = load_roles()
        lines = [f"ℹ️ **Версия модуля:** `{START_VERSION}`"]

        if not roles:
            lines.append(
                "⚠️ Начальный комплект пуст. Добавьте роли через `/startroles_add`."
            )
        else:
            lines.append(f"**Начальный комплект ({len(roles)}):**")
            panel_ids = _panel_role_ids()
            for role_id in roles:
                role = guild.get_role(role_id)
                if role is None:
                    lines.append(f"• <@&{role_id}> — ⚠️ роль не найдена на сервере")
                    continue
                note = ""
                if not _can_manage_role(guild, role):
                    note = " — ❌ выше роли бота"
                elif role.id in panel_ids:
                    note = " — ⚠️ также используется в панели ролей"
                lines.append(f"• {role.mention}{note}")

        ok, note = check_data_path()
        lines.append(("✅" if ok else "❌") + f" **Файл:** {note}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ----------------- /STARTROLES_CLEAR -----------------
    @bot.tree.command(
        name="startroles_clear", description="Полностью очистить начальный комплект"
    )
    @app_commands.describe(
        confirm="Обязательное подтверждение — действие необратимо",
        revoke_roles="Снять все роли комплекта со всех участников",
    )
    async def startroles_clear(
        interaction: discord.Interaction,
        confirm: bool = False,
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

        roles = load_roles()
        if not roles:
            await interaction.response.send_message(
                "ℹ️ Комплект и так пуст — очищать нечего.", ephemeral=True
            )
            return

        if not confirm:
            await interaction.response.send_message(
                f"⚠️ Это **необратимо**: комплект из {len(roles)} ролей будет очищен.\n"
                + (
                    "⚠️ `revoke_roles: true` дополнительно **снимет эти роли у всех** "
                    "участников.\n"
                    if revoke_roles
                    else ""
                )
                + "Повторите с `confirm: true`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        target_roles = [r for rid in roles if (r := guild.get_role(rid)) is not None]
        target_ids = {r.id for r in target_roles}

        revoked = 0
        errors = 0
        if revoke_roles and target_ids:
            for member in guild.members:
                present = [r for r in member.roles if r.id in target_ids]
                if not present:
                    continue
                try:
                    await member.remove_roles(
                        *present, reason="Начальные роли: комплект очищен"
                    )
                    revoked += 1
                    if revoked % 10 == 0:
                        await asyncio.sleep(SYNC_DELAY)
                except discord.Forbidden:
                    errors += 1
                    print(f"[START] Нет прав на снятие ролей у {member.id}")
                except discord.HTTPException as e:
                    errors += 1
                    print(f"[START] Ошибка снятия ролей у {member.id}: {e}")

        save_roles([])
        print(
            f"[START] Комплект очищен; роли сняты у {revoked} участников, ошибок: {errors}"
        )

        lines = [
            "✅ **Начальный комплект очищен.**",
            f"• Убрано ролей: {len(roles)}",
        ]
        if revoke_roles:
            lines.append(
                f"• Снято у {revoked} участников"
                + (f", ошибок: {errors}" if errors else "")
            )
        else:
            lines.append("• Роли у участников оставлены (`revoke_roles: false`)")
        lines.append("Наполнить заново: `/startroles_add`.")

        await interaction.edit_original_response(content="\n".join(lines))
