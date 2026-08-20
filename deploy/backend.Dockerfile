FROM python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

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
    ANIMEMO_ARTIFACT_VERSION=${ANIMEMO_VERSION} \
    ANIMEMO_ARTIFACT_COMMIT=${ANIMEMO_COMMIT} \
    ANIMEMO_ARTIFACT_CHANNEL=${ANIMEMO_CHANNEL} \
    ANIMEMO_VERSION=${ANIMEMO_VERSION} \
    ANIMEMO_COMMIT=${ANIMEMO_COMMIT} \
    ANIMEMO_RELEASE_CHANNEL=${ANIMEMO_CHANNEL}

RUN apk upgrade --no-cache \
    && apk add --no-cache tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
RUN python -m pip uninstall --yes pip

COPY backend /app/backend
COPY plugins /app/plugins
RUN adduser -D -u 10001 -h /home/animemo animemo \
    && mkdir -p /app/runtime/plugins /app/logs \
    && chown -R animemo:animemo /app/runtime /app/logs /app/backend /app/plugins \
    && ANIMEMO_BUILD_STATIC=1 python /app/backend/manage.py collectstatic --noinput \
    && chown -R animemo:animemo /app/backend/staticfiles
WORKDIR /app/backend

USER animemo

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "120"]
