import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://127.0.0.1:8275', browserName: 'chromium', headless: true, launchOptions: { executablePath: '/usr/bin/google-chrome' } },
  webServer: {
    command: 'npm run build && cd .. && .venv/bin/python -m uvicorn api:app --host 127.0.0.1 --port 8275',
    url: 'http://127.0.0.1:8275/health',
    reuseExistingServer: true,
    timeout: 30_000,
  },
})
