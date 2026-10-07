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
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    GOPROXY=off GOTOOLCHAIN=local CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/animemo ./cmd/animemo

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=api /out/animemo /app/animemo
COPY --from=web --chown=65532:65532 /source/.local/output/web /app/web
ENV LISTEN_ADDR=0.0.0.0:18081 WEB_DIR=/app/web
EXPOSE 18081
ENTRYPOINT ["/app/animemo"]
CMD ["serve"]
