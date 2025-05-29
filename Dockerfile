FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt
COPY . .

FROM python:3.12-slim AS main-bot
WORKDIR /app
RUN apt-get update && \
    apt-get install -y tzdata fonts-dejavu && \
    ln -fs /usr/share/zoneinfo/Europe/Moscow /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    echo "TZ=Europe/Moscow" > /etc/default/locale && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /install/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /app/bot.py /app/
COPY --from=builder /app/lib /app/lib
COPY --from=builder /app/.env /app/
CMD ["python", "bot.py"]

FROM python:3.12-slim AS admin-bot
WORKDIR /app
RUN apt-get update && \
    apt-get install -y tzdata fonts-dejavu && \
    ln -fs /usr/share/zoneinfo/Europe/Moscow /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    echo "TZ=Europe/Moscow" > /etc/default/locale && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /install/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /app/admin.py /app/
COPY --from=builder /app/lib /app/lib
COPY --from=builder /app/.env /app/
CMD ["python", "admin.py"]

FROM python:3.12-slim AS scheduler
WORKDIR /app
RUN apt-get update && \
    apt-get install -y tzdata cron fonts-dejavu && \
    ln -fs /usr/share/zoneinfo/Europe/Moscow /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    echo "TZ=Europe/Moscow" > /etc/default/locale && \
    rm -rf /var/lib/apt/lists/* && \
    echo "" >> /etc/cron.d/bot-cron
COPY --from=builder /install/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /app/db_updater.py /app/
COPY --from=builder /app/reminder.py /app/
COPY --from=builder /app/lib /app/lib
COPY --from=builder /app/.env /app/
COPY crontab /etc/cron.d/bot-cron
CMD ["cron", "-f"]