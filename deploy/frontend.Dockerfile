FROM node:24-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 AS builder

WORKDIR /app

RUN apk upgrade --no-cache
RUN npm install --global npm@12.0.2

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html postcss.config.js tailwind.config.js vite.config.mjs ./
COPY public ./public
COPY scripts ./scripts
COPY src ./src
COPY plugins ./plugins

ARG VITE_API_BASE_URL=/api/v1
ARG ANIMEMO_VERSION=0.0.0
ARG ANIMEMO_COMMIT=unknown
ARG ANIMEMO_CHANNEL=development
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_ANIMEMO_VERSION=${ANIMEMO_VERSION}
ENV VITE_ANIMEMO_COMMIT=${ANIMEMO_COMMIT}
ENV VITE_ANIMEMO_CHANNEL=${ANIMEMO_CHANNEL}

RUN npm exec vite build

FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de

ARG ANIMEMO_VERSION=0.0.0
ARG ANIMEMO_COMMIT=unknown
ARG ANIMEMO_CHANNEL=development

LABEL org.opencontainers.image.version=${ANIMEMO_VERSION} \
      org.opencontainers.image.revision=${ANIMEMO_COMMIT} \
      cc.animemo.release.channel=${ANIMEMO_CHANNEL}

ENV TZ=Asia/Shanghai

RUN apk upgrade --no-cache \
    && apk add --no-cache tzdata \
    && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist/client /usr/share/nginx/html

EXPOSE 80
