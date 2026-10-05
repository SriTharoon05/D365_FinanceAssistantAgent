import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const backend = fileURLToPath(new URL('../backend', import.meta.url));
const python = process.platform === 'win32' ? '.venv\\Scripts\\python.exe' : '.venv/bin/python';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:5174',
    trace: 'retain-on-failure',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `${python} -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --log-level warning --no-access-log`,
      cwd: backend,
      url: 'http://127.0.0.1:8010/api/health',
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        D365_MOCK_MODE: 'true',
        LOG_LEVEL: 'WARNING',
        AZURE_OPENAI_API_KEY: '',
        GROQ_API_KEY: '',
        DATABASE_URL: 'sqlite+aiosqlite:///./data/e2e.db',
        FRONTEND_ORIGIN: 'http://127.0.0.1:5174',
        APP_SESSION_SECRET: 'isolated-e2e-session-key-never-for-deployment',
      },
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5174 --strictPort',
      url: 'http://127.0.0.1:5174',
      reuseExistingServer: false,
      timeout: 60_000,
      env: { API_PROXY_TARGET: 'http://127.0.0.1:8010' },
    },
  ],
});
