FROM node:24.12.0-bookworm-slim AS web
WORKDIR /source
COPY package.json package-lock.json .npmrc ./
RUN npm ci --ignore-scripts
COPY tsconfig.json ./
COPY web ./web
RUN node node_modules/typescript/bin/tsc --noEmit && node node_modules/vite/bin/vite.js build --config web/vite.config.ts

FROM golang:1.26.6-bookworm AS api
WORKDIR /source
COPY server/go.mod server/go.sum ./
RUN go mod download
COPY server ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/animemo ./cmd/animemo

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=api /out/animemo /app/animemo
COPY --from=web /source/.local/output/web /app/web
ENV LISTEN_ADDR=0.0.0.0:18081 WEB_DIR=/app/web
EXPOSE 18081
ENTRYPOINT ["/app/animemo"]
CMD ["serve"]
