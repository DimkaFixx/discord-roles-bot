import asyncio
import os
from contextlib import asynccontextmanager
import discord
from discord.ext import commands
from discord import app_commands
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel
import uvicorn

# ----------------- КОНФИГУРАЦИЯ ИЗ ENV -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))
API_SECRET_KEY = os.getenv("API_SECRET_KEY")
# ----------------- НАСТРОЙКИ ИЗ ENV -----------------
# Читаем ID через запятую из .env и преобразуем в списки int
ALLOWED_MODERATOR_ROLE_IDS = [
    int(r_id.strip()) 
    for r_id in os.getenv("ALLOWED_MODERATOR_ROLE_IDS", "").split(",") 
    if r_id.strip().isdigit()
]

START_ROLE_IDS = [
    int(r_id.strip()) 
    for r_id in os.getenv("START_ROLE_IDS", "").split(",") 
    if r_id.strip().isdigit()
]

# ----------------- ИНИЦИАЛИЗА -----------------
intents = discord.Intents.default()
intents.members = True  # Включите Server Members Intent в Developer Portal!
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------- LIFESPAN ДЛЯ ФОНОВОГО ЗАПУСКА БОТА -----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем бота асинхронным таском при старте FastAPI
    bot_task = asyncio.create_task(bot.start(BOT_TOKEN))
    print("Запущен фоновый таск авторизации Discord бота...")
    yield
    # Отключаем бота при остановке приложения
    await bot.close()
    bot_task.cancel()

app = FastAPI(title="Discord Role Manager API", lifespan=lifespan)

# ----------------- МОДЕЛИ ДАННЫХ -----------------
class RoleManageRequest(BaseModel):
    user_id: str
    roles_to_remove: list[str] = []
    roles_to_add: list[str] = []

# ----------------- FASTAPI СЕРВЕР -----------------
@app.post("/api/manage-roles")
async def manage_roles(
    data: RoleManageRequest,
    x_api_key: str = Header(..., alias="X-API-Key")
):
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительный API Ключ"
        )

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        raise HTTPException(status_code=404, detail="Сервер Discord не найден")

    try:
        member = await guild.fetch_member(int(data.user_id))
    except discord.NotFound:
        raise HTTPException(status_code=404, detail="Пользователь не найден на сервере")
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный Discord ID")

    results = {"removed": [], "added": [], "errors": []}

    # 1. Удаление ролей
    for role_id_str in data.roles_to_remove:
        try:
            role = guild.get_role(int(role_id_str))
            if role and role in member.roles:
                await member.remove_roles(role)
                results["removed"].append(role_id_str)
        except Exception as e:
            results["errors"].append(f"Ошибка удаления роли {role_id_str}: {str(e)}")

    # 2. Добавление ролей
    for role_id_str in data.roles_to_add:
        try:
            role = guild.get_role(int(role_id_str))
            if role and role not in member.roles:
                await member.add_roles(role)
                results["added"].append(role_id_str)
        except Exception as e:
            results["errors"].append(f"Ошибка добавления роли {role_id_str}: {str(e)}")

    return {"status": "success", "details": results}

# ----------------- СОБЫТИЯ И КОМАНДЫ -----------------
@bot.event
async def on_ready():
    print(f"Бот успешно запущен как: {bot.user.name} (ID: {bot.user.id})")
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Синхронизировано slash-команд для гильдии {GUILD_ID}: {len(synced)}")
        else:
            synced = await bot.tree.sync()
            print(f"Синхронизировано глобальных slash-команд: {len(synced)}")
    except Exception as e:
        print(f"Ошибка синхронизации команд: {e}")

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong! Бот и API работают.")

# ----------------- СЛЭШ-КОМАНДА /STARTROLES -----------------
@bot.tree.command(name="startroles", description="Выдать начальный комплект ролей участнику")
@app_commands.describe(member="Участник, которому выдаем роли")
async def start_roles(interaction: discord.Interaction, member: discord.Member):
    # 1. Проверяем роли модератора
    user_role_ids = [role.id for role in interaction.user.roles]
    has_permission = any(role_id in user_role_ids for role_id in ALLOWED_MODERATOR_ROLE_IDS)

    if not has_permission:
        await interaction.response.send_message(
            "❌ У вас нет прав для использования этой команды.", 
            ephemeral=True
        )
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ Команда должна выполняться на сервере.", ephemeral=True)
        return

    # 2. Собираем роли для выдачи
    roles_to_add = []
    for role_id in START_ROLE_IDS:
        role = guild.get_role(role_id)
        if role and role not in member.roles:
            roles_to_add.append(role)

    if not roles_to_add:
        await interaction.response.send_message(
            f"⚠️ У участника {member.mention} уже есть все начальные роли или роли не найдены на сервере.", 
            ephemeral=True
        )
        return

    # 3. Выдаем роли
    try:
        await member.add_roles(*roles_to_add, reason=f"Выдача начальных ролей модератором {interaction.user}")
        
        added_mentions = " ".join([r.mention for r in roles_to_add])
        await interaction.response.send_message(
            f"✅ Участнику {member.mention} успешно выданы начальные роли:\n{added_mentions}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Ошибка прав: Убедитесь, что роль бота находится ВЫШЕ выдаваемых ролей в настройках сервера.", 
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Ошибка при выдаче ролей: {str(e)}", 
            ephemeral=True
        )

# ----------------- ЗАПУСК -----------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)