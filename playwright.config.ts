import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
const root = join(process.env.XAU_NEXT_E2E_ROOT ?? tmpdir(), `xau-next-e2e-${randomUUID()}`);
export default defineConfig({
  testDir: './tests/e2e', timeout: 60000, fullyParallel: false, workers: 1,
  use: { baseURL: 'http://127.0.0.1:18766', channel: 'chrome', headless: true,
    viewport: { width: 1440, height: 1050 }, screenshot: 'only-on-failure', trace: 'off' },
  webServer: [
    { command: '.venv\\Scripts\\python.exe -m uvicorn tests.model_api.browser_app:app --host 127.0.0.1 --port 18765',
      url: 'http://127.0.0.1:18765/v1/health', reuseExistingServer: false, timeout: 30000,
      env: { XAU_NEXT_DATA_ROOT: root, XAU_NEXT_SOURCE: 'synthetic', XAU_NEXT_SESSION_TOKEN: 'e2e-isolated-session',
        XAU_NEXT_WEB_ORIGINS: 'http://127.0.0.1:18766', PYTHONIOENCODING: 'utf-8' } },
    { command: 'apps\\web\\node_modules\\.bin\\vite.cmd preview --config apps/web/vite.config.ts --host 127.0.0.1 --port 18766 --strictPort apps/web',
      url: 'http://127.0.0.1:18766', reuseExistingServer: false, timeout: 30000 }
  ]
});
