FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SERVER_MONITOR_DATA_DIR=/var/lib/server-monitor \
    SERVER_MONITOR_BIND=0.0.0.0:8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --home-dir /home/server-monitor --shell /usr/sbin/nologin server-monitor \
    && install -d -o server-monitor -g server-monitor -m 0700 /var/lib/server-monitor

WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY gunicorn.conf.py ./
COPY monitor ./monitor
COPY scripts/reset_admin_password.py ./scripts/reset_admin_password.py
RUN chown -R server-monitor:server-monitor /app

USER server-monitor
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "gunicorn", "-c", "gunicorn.conf.py", "monitor.wsgi:app"]
