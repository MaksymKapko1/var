FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY gridbot ./gridbot

RUN useradd --create-home --uid 10001 bot && mkdir -p /data && chown bot:bot /data
USER bot
VOLUME ["/data"]

# здоров = heartbeat обновлялся последние 3 минуты (пишет watchdog-поток бота)
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,sys,time; f=os.environ.get('DATA_DIR','/data')+'/heartbeat'; sys.exit(0 if os.path.exists(f) and time.time()-os.path.getmtime(f)<180 else 1)"

STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "gridbot"]
CMD ["run"]
