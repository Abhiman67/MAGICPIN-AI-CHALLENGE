FROM python:3.11-slim

WORKDIR /app

COPY bot.py /app/bot.py
COPY README.md /app/README.md

ENV BOT_HOST=0.0.0.0
ENV BOT_PORT=8080

EXPOSE 8080

CMD ["python", "bot.py"]
