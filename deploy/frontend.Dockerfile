FROM node:20-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html postcss.config.js tailwind.config.js vite.config.mjs ./
COPY public ./public
COPY scripts ./scripts
COPY src ./src
COPY plugins ./plugins

ARG VITE_API_BASE_URL=/api/v1
ARG VITE_TURNSTILE_SITE_KEY
ARG ANIMEMO_VERSION=0.0.0
ARG ANIMEMO_COMMIT=unknown
ARG ANIMEMO_CHANNEL=development
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_TURNSTILE_SITE_KEY=${VITE_TURNSTILE_SITE_KEY}
ENV VITE_ANIMEMO_VERSION=${ANIMEMO_VERSION}
ENV VITE_ANIMEMO_COMMIT=${ANIMEMO_COMMIT}
ENV VITE_ANIMEMO_CHANNEL=${ANIMEMO_CHANNEL}

RUN npm exec vite build

FROM nginx:1.27-alpine

ARG ANIMEMO_VERSION=0.0.0
ARG ANIMEMO_COMMIT=unknown
ARG ANIMEMO_CHANNEL=development

LABEL org.opencontainers.image.version=${ANIMEMO_VERSION} \
      org.opencontainers.image.revision=${ANIMEMO_COMMIT} \
      cc.animemo.release.channel=${ANIMEMO_CHANNEL}

ENV TZ=Asia/Shanghai

RUN apk add --no-cache tzdata \
    && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist/client /usr/share/nginx/html

EXPOSE 80
