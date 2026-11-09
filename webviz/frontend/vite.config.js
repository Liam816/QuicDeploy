import { defineConfig } from 'vite';
export default defineConfig({
  build: { outDir: '../dist', emptyOutDir: true, chunkSizeWarningLimit: 850 },
  server: { proxy: { '/api': 'http://127.0.0.1:8765', '/robot': 'http://127.0.0.1:8765', '/packages': 'http://127.0.0.1:8765' } },
});
