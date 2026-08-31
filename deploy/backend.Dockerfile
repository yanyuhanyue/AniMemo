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
    PYTHONPATH=/app \
    TZ=Asia/Shanghai \
    ANIMEMO_ARTIFACT_VERSION=${ANIMEMO_VERSION} \
    ANIMEMO_ARTIFACT_COMMIT=${ANIMEMO_COMMIT} \
    ANIMEMO_ARTIFACT_CHANNEL=${ANIMEMO_CHANNEL} \
    ANIMEMO_VERSION=${ANIMEMO_VERSION} \
    ANIMEMO_COMMIT=${ANIMEMO_COMMIT} \
    ANIMEMO_RELEASE_CHANNEL=${ANIMEMO_CHANNEL}

WORKDIR /app

COPY backend/pip-bootstrap.lock /app/pip-bootstrap.lock
COPY backend/container-requirements.lock /app/container-requirements.lock
COPY backend/requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --no-deps --require-hashes \
      -r /app/pip-bootstrap.lock
RUN python -m pip install --no-cache-dir --require-hashes \
      -r /app/requirements.lock -r /app/container-requirements.lock
RUN zoneinfo="$(python -c 'from importlib.resources import files; print(files("tzdata.zoneinfo").joinpath("Asia", "Shanghai"))')" \
    && test -f "$zoneinfo" \
    && cp "$zoneinfo" /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone
RUN python -m pip uninstall --yes pip

COPY backend /app/backend
COPY plugins /app/plugins
COPY --chown=0:0 --chmod=0444 installer/__init__.py /app/installer/__init__.py
COPY --chown=0:0 --chmod=0444 installer/safe_archive.py /app/installer/safe_archive.py
RUN addgroup -S -g 10001 animemo \
    && adduser -S -D -u 10001 -G animemo -h /home/animemo animemo \
    && mkdir -p /app/runtime/plugins /app/logs \
    && chown root:root /app/installer \
    && chmod 0555 /app/installer \
    && chown -R animemo:animemo /app/runtime /app/logs /app/backend /app/plugins \
    && ANIMEMO_BUILD_STATIC=1 python /app/backend/manage.py collectstatic --noinput \
    && chown -R animemo:animemo /app/backend/staticfiles
WORKDIR /app/backend

USER 10001:10001

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "120"]
