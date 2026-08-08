import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const securityHeaders = {
  "Content-Security-Policy": "default-src 'self'; script-src 'self' 'sha256-xjQsrThiVsL5TEjVM6dTosT1AwZvcPBi17BPpLuouCM=' https://challenges.cloudflare.com; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data: blob: https://media.re-anime.cc https://img.re-anime.cc https://lain.bgm.tv https://bgm-img-proxy.xhcytus100.workers.dev; font-src 'self' data:; connect-src 'self' https://api.bgm.tv wss://re-anime.cc; frame-src https://challenges.cloudflare.com; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
  "X-Frame-Options": "DENY",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
};
const developmentHeaders = {
  "X-Frame-Options": "DENY",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
};
const localProxy = {
  "/api": "http://127.0.0.1:8000",
  "/plugin-assets": "http://127.0.0.1:8000",
  "/media": "http://127.0.0.1:8000",
};

export default defineConfig(({ mode }) => {
  const demoDataModule = fileURLToPath(new URL(
    mode === "development"
      ? "./src/data/demoData.development.js"
      : "./src/data/demoData.production.js",
    import.meta.url,
  ));

  return {
    build: {
      outDir: "dist/client",
      emptyOutDir: true,
    },
    optimizeDeps: {
      include: ["react", "react-dom/client"],
    },
    resolve: {
      alias: {
        "@demo-data": demoDataModule,
      },
    },
    server: {
      host: "0.0.0.0",
      allowedHosts: ["terminal.local"],
      headers: developmentHeaders,
      proxy: localProxy,
      warmup: {
        clientFiles: ["./src/main.jsx"],
      },
    },
    preview: {
      headers: securityHeaders,
      proxy: localProxy,
    },
    plugins: [react()],
  };
});
