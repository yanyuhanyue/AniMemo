FROM python:3.12-slim

ARG ANIMEMO_VERSION=0.0.0
ARG ANIMEMO_COMMIT=unknown
ARG ANIMEMO_CHANNEL=development

LABEL org.opencontainers.image.version=${ANIMEMO_VERSION} \
      org.opencontainers.image.revision=${ANIMEMO_COMMIT} \
      cc.animemo.release.channel=${ANIMEMO_CHANNEL}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    ANIME_JOURNAL_VERSION=${ANIMEMO_VERSION} \
    ANIME_JOURNAL_COMMIT=${ANIMEMO_COMMIT} \
    ANIME_JOURNAL_RELEASE_CHANNEL=${ANIMEMO_CHANNEL}

RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend /app/backend
COPY plugins /app/plugins
RUN useradd --create-home --uid 10001 animejournal \
    && mkdir -p /app/runtime/plugins /app/logs \
    && chown -R animejournal:animejournal /app/runtime /app/logs /app/backend /app/plugins
WORKDIR /app/backend

USER animejournal

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate && python manage.py sync_official_plugins && python manage.py collectstatic --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 120"]
