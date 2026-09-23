FROM python:3.11-slim

# Отключаем буферизацию вывода Python (чтобы логи бота сразу видна были в консоли)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY main.py .

# Открываем порт для FastAPI
EXPOSE 8000

# Запускаем бота
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]