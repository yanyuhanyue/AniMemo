import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  cacheDir: '../.local/cache/vite',
  plugins: [react()],
  server: {
    host: '127.0.0.1', port: 5177, strictPort: true,
    proxy: { '/api': { target: process.env.ANIMEMO_API_PROXY || 'http://127.0.0.1:18081', changeOrigin: false } },
  },
  build: { outDir: '../.local/output/web', emptyOutDir: true, sourcemap: false },
});
