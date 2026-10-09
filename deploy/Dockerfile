FROM node:24.12.0-bookworm-slim AS web
WORKDIR /source
COPY package.json package-lock.json .npmrc ./
RUN --mount=type=secret,id=build_ca \
    --mount=type=cache,target=/source/.local/cache/npm \
    if [ -f /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    npm ci --ignore-scripts
COPY tsconfig.json ./
COPY web ./web
RUN node node_modules/typescript/bin/tsc --noEmit && node node_modules/vite/bin/vite.js build --config web/vite.config.ts

FROM golang:1.26.6-alpine AS api
WORKDIR /source
COPY server/go.mod server/go.sum ./
RUN --mount=type=secret,id=build_ca \
    --mount=type=cache,target=/go/pkg/mod \
    if [ -f /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    go mod download
COPY server ./
ARG ANIMEMO_VERSION=development
ARG ANIMEMO_REVISION=unknown
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    GOPROXY=off GOTOOLCHAIN=local CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X animemo.local/server/internal/buildinfo.Version=${ANIMEMO_VERSION} -X animemo.local/server/internal/buildinfo.Revision=${ANIMEMO_REVISION}" -o /out/animemo ./cmd/animemo

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    GOPROXY=off GOTOOLCHAIN=local CGO_ENABLED=0 GOOS=wasip1 GOARCH=wasm go build -trimpath -ldflags="-s -w" -o /out/watch-history-text.wasm ./examples/watch-history-text

FROM node:24.12.0-bookworm-slim AS extensions
WORKDIR /build
COPY --from=api /out/ /build/
COPY server/examples/watch-history-text/manifest.json /build/manifest.json
COPY server/examples/notes-hanami/manifest.json /build/theme.json
COPY server/examples/notes-gallery /build/notes-gallery
COPY tooling/bundle-extensions.mjs tooling/plugin-package.mjs /build/
RUN node bundle-extensions.mjs animemo watch-history-text.wasm manifest.json /build/extensions theme.json notes-gallery/manifest.json

FROM gcr.io/distroless/static-debian12:nonroot
ARG ANIMEMO_VERSION=development
ARG ANIMEMO_REVISION=unknown
WORKDIR /app
LABEL org.animemo.worker-mode="separate-v1"
LABEL org.opencontainers.image.version=$ANIMEMO_VERSION org.opencontainers.image.revision=$ANIMEMO_REVISION org.opencontainers.image.source="https://github.com/yanyuhanyue/AniMemo"
COPY --from=api /out/animemo /app/animemo
COPY --from=extensions /build/extensions /app/extensions
COPY --from=web --chown=65532:65532 /source/.local/output/web /app/web
ENV LISTEN_ADDR=0.0.0.0:18081 WEB_DIR=/app/web ANIMEMO_BUNDLED_DIR=/app/extensions
EXPOSE 18081
ENTRYPOINT ["/app/animemo"]
CMD ["serve"]
