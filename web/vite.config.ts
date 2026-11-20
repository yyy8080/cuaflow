import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const backend = 'http://127.0.0.1:8765';

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../src/cuaflow/server/static',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/ws': { target: backend, ws: true, changeOrigin: true },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
