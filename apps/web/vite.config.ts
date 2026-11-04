import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

const local = (path: string) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1' },
  resolve: { alias: {
    react: local('./node_modules/react'),
    'react-dom': local('./node_modules/react-dom'),
    'react-router-dom': local('./node_modules/react-router-dom'),
    '@testing-library/react': local('./node_modules/@testing-library/react'),
    '@testing-library/user-event': local('./node_modules/@testing-library/user-event'),
    '@tanstack/react-query': local('./node_modules/@tanstack/react-query'),
    '@xau/api-client': local('../../packages/api-client/src/index.ts'),
  } },
  test: { include: ['../../tests/web/**/*.test.{ts,tsx}'], environment: 'jsdom', testTimeout: 10000, restoreMocks: true },
});
