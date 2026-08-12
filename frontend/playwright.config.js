import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  // E2E 必须经过 Vite 开发代理，与本地 Chrome 访问的页面路径保持一致。
  use: { baseURL: 'http://127.0.0.1:5277', browserName: 'chromium', headless: true, launchOptions: { executablePath: '/usr/bin/google-chrome' } },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5277 --strictPort',
    url: 'http://127.0.0.1:5277/',
    // 使用独占端口，禁止复用可能陈旧的手工服务实例。
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
