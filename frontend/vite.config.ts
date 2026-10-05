import { defineConfig } from 'vitest/config';
import { loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: process.env.API_PROXY_TARGET || env.API_PROXY_TARGET || 'http://127.0.0.1:8000',
          changeOrigin: true,
        },
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: (id: string) => {
            if (!id.includes('node_modules')) return;
            if (/\/(react|react-dom|scheduler)\//.test(id)) return 'react-vendor';
            if (id.includes('@radix-ui')) return 'ui-vendor';
            if (/react-markdown|remark-|micromark|mdast|hast|unified|rehype|unist|vfile/.test(id))
              return 'markdown-vendor';
          },
        },
      },
    },
    test: {
      environment: 'jsdom',
      include: ['src/**/*.{test,spec}.{ts,tsx}'],
      setupFiles: './src/test-setup.ts',
      globals: true,
      restoreMocks: true,
    },
  };
});
