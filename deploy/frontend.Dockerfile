FROM node:24-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 AS builder

WORKDIR /app

RUN npm pack npm@12.0.2 --ignore-scripts --pack-destination /tmp \
    && echo "b885e890b9418fa1693544d05f53e64f9a73ec194837d4258b15fecdd692347b1dd2a517b1b0cbaf9d31cd8e92c3b70956bd2ecc72833a57b4b3098f5bfa7943  /tmp/npm-12.0.2.tgz" \
      > /tmp/npm-12.0.2.sha512 \
    && sha512sum -c /tmp/npm-12.0.2.sha512 \
    && npm install --global --ignore-scripts /tmp/npm-12.0.2.tgz \
    && test "$(npm --version)" = "12.0.2" \
    && rm -f /tmp/npm-12.0.2.tgz /tmp/npm-12.0.2.sha512

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

ENV TZ=CST-8

RUN mkdir -p /etc/nginx/animemo
COPY deploy/nginx.conf /etc/nginx/animemo/default.conf.template
COPY deploy/nginx-entrypoint.sh /usr/local/bin/animemo-nginx-entrypoint
RUN chmod 0555 /usr/local/bin/animemo-nginx-entrypoint
COPY --from=builder /app/dist/client /usr/share/nginx/html

EXPOSE 80

ENTRYPOINT ["/usr/local/bin/animemo-nginx-entrypoint"]
CMD ["nginx", "-g", "daemon off;"]
