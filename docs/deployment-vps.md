# VPS Deployment

Build the frontend with `npm run build`, run Django migrations from a clean
database, and configure the backend plugin allowlist through environment
variables. Keep `PLUGIN_PACKAGE_ROOT=/app/runtime/plugins` outside static source, use HTTPS, secure cookies,
and enforce CSP after collecting report-only violations.
