FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY config ./config

RUN pip install --no-cache-dir -e .

ENV PYTHONPATH=/app/src
ENV PAPER_TRADING=true
ENV ALPACA_BASE_URL=https://paper-api.alpaca.markets
ENV DATABASE_PATH=/app/data/state/alpaca_bot.db

VOLUME ["/app/data"]

CMD ["python", "-m", "alpaca_bot.main"]
