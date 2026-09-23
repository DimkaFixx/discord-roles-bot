import asyncio
import os
import discord
from discord.ext import commands
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel
import uvicorn

# ----------------- КОНФИГУРАЦИЯ ИЗ ENV -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))
API_SECRET_KEY = os.getenv("API_SECRET_KEY")

# ----------------- ИНИЦИАЛИЗА -----------------
intents = discord.Intents.default()
intents.members = True  # Включите Server Members Intent в Developer Portal!
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
app = FastAPI(title="Discord Role Manager API")

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

# ----------------- КОМАНДЫ БОТА (ДЛЯ РАСШИРЕНИЯ) -----------------
@bot.event
async def on_ready():
    print(f"Бот успешно запущен как: {bot.user.name} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано slash-команд: {len(synced)}")
    except Exception as e:
        print(f"Ошибка синхронизации команд: {e}")

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong! Бот и API работают локально в Docker.")

# ----------------- ЗАПУСК СЕРВИСОВ -----------------
async def main():
    # Запускаем FastAPI внутри контейнера на 0.0.0.0:8000
    config = uvicorn.Config(app=app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    await asyncio.gather(
        bot.start(BOT_TOKEN),
        server.serve()
    )

if __name__ == "__main__":
    asyncio.run(main())