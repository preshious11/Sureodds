# Deployable image for the predbot Telegram bot (code lives in predictions-bot/).
FROM python:3.11-slim

WORKDIR /app/predictions-bot

COPY predictions-bot/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY predictions-bot/ ./

# Runtime artifacts inside the image layer (mount a volume at /data to persist)
ENV DB_PATH=/data/predbot.db \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data

CMD ["python", "main.py"]