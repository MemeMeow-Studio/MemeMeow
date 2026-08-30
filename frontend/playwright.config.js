/** Playwright 浏览器级测试配置，优先使用显式浏览器路径并保留跨环境回退。 */
import { existsSync } from 'node:fs'
import { defineConfig } from '@playwright/test'

const configuredChrome = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
const executablePath = configuredChrome || (existsSync('/usr/bin/google-chrome') ? '/usr/bin/google-chrome' : undefined)
// 允许在同机 Server 代理占用默认端口时隔离启动开源前端，默认行为保持不变。
const configuredPort = Number.parseInt(process.env.PLAYWRIGHT_PORT || '5277', 10)
const e2ePort = Number.isInteger(configuredPort) && configuredPort > 0 ? configuredPort : 5277

export default defineConfig({
  testDir: './e2e',
  // E2E 必须经过 Vite 开发代理，与本地 Chrome 访问的页面路径保持一致。
  use: {
    baseURL: `http://127.0.0.1:${e2ePort}`,
    browserName: 'chromium',
    headless: true,
    launchOptions: executablePath ? { executablePath } : {},
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${e2ePort} --strictPort`,
    url: `http://127.0.0.1:${e2ePort}/`,
    // 使用独占端口，禁止复用可能陈旧的手工服务实例。
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
