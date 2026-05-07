FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.45/supercronic-linux-amd64
RUN wget -qO /usr/local/bin/supercronic "$SUPERCRONIC_URL" && \
    chmod +x /usr/local/bin/supercronic

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data

COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

COPY main.py .

VOLUME ["/app/data"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["supercronic", "/app/crontab"]
