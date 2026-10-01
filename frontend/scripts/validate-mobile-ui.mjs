/** 通过已有开发入口和真实 API 验证手机布局，并保存页面尺寸、交互证据与截图。 */
import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { chromium, expect } from '@playwright/test'

const baseURL = process.env.MOBILE_UI_BASE_URL || 'http://127.0.0.1:5275'
const serverMode = process.env.MOBILE_UI_MODE === 'server'
const apiPrefix = process.env.MOBILE_UI_API_PREFIX ?? (serverMode && baseURL.startsWith('https://') ? '' : '/api')
const outputDirectory = resolve(process.env.MOBILE_UI_OUTPUT_DIR || 'test-results/mobile-ui')
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] })
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 })
const page = await context.newPage()
const evidence = []
const errors = []
page.on('pageerror', (error) => errors.push(error.message))
await mkdir(outputDirectory, { recursive: true })

/** 在真实页面之间切换，服务器版本保留原有路由，公共版本通过导航事件切换。 */
async function navigate(pageId) {
  if (serverMode) {
    await page.goto(`${baseURL}/${pageId}`)
  } else {
    if (page.url() === 'about:blank') await page.goto(baseURL)
    const navigation = page.locator('.workspace-mobile-nav')
    if (await navigation.isVisible()) {
      if (pageId === 'collections') {
        await navigation.getByRole('button', { name: '更多', exact: true }).click()
        await page.getByRole('dialog', { name: '更多', exact: true }).getByRole('button', { name: '合集', exact: true }).click()
      } else {
        const labels = { search: '检索', library: '图片库', upload: '上传', tasks: '任务' }
        await navigation.getByRole('button', { name: labels[pageId], exact: true }).click()
      }
    } else {
      const labels = { search: '检索', library: '图片库', upload: '上传', tasks: '处理任务', collections: '合集' }
      await page.locator('.workspace-desktop-nav').getByRole('button', { name: labels[pageId], exact: true }).click()
    }
  }
  await page.waitForLoadState('networkidle')
  if (pageId === 'library') {
    const listToggle = page.getByRole('button', { name: '列表', exact: true })
    if (await listToggle.count()) await listToggle.click()
  }
}

/** 读取浏览器的实际布局；不拦截接口，也不修改页面的业务数据或样式。 */
async function capture(label) {
  const measurements = await page.evaluate(() => {
    const visible = (element) => element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden'
    const rectangle = (element) => {
      const value = element.getBoundingClientRect()
      return { x: value.x, y: value.y, width: value.width, height: value.height, right: value.right, bottom: value.bottom }
    }
    const read = (selector) => [...document.querySelectorAll(selector)].filter(visible).map((element) => ({ ...rectangle(element), text: element.innerText?.slice(0, 100), fontSize: getComputedStyle(element).fontSize }))
    return {
      viewport: { width: innerWidth, height: innerHeight },
      documentWidth: document.documentElement.scrollWidth,
      serviceStates: [...document.querySelectorAll(".mobile-service-state .embedding-global")].filter(visible).map((element) => ({ text: element.textContent, clientWidth: element.clientWidth, scrollWidth: element.scrollWidth })),
      choiceTargets: read(".option-choice, .option-toggle"),
      controls: read(".library-toolbar button, .library-view-toggle button, .image-row-actions button, .image-dialog button, .topbar-actions button, .account-entry"),
      header: read('.topbar'), brand: read('.brand'), headerActions: read('.topbar-actions'),
      navigation: read('.sidebar'), navigationButtons: read('.sidebar nav button'),
      toolbar: read('.library-toolbar'), rows: read('.library-row'),
      help: read('.help-popover-trigger'),
      inputs: read('input:not([type="checkbox"]):not([type="radio"]):not([type="file"]):not([type="hidden"]), select, textarea'),
      dialogs: read('[role="dialog"][aria-modal="true"]'),
      dialogImages: read('.search-result-dialog-body img, .image-dialog-preview img'),
      dialogActions: read('.processing-options-actions, .search-result-dialog-actions'),
    }
  })
  evidence.push({ label, ...measurements })
  assert.equal(measurements.documentWidth, measurements.viewport.width, `${label} 页面出现横向溢出`)
  if (measurements.viewport.width <= 760) {
    for (const state of measurements.serviceStates) assert(state.scrollWidth <= state.clientWidth, `${label} 手机服务状态文字截断`)
    for (const target of measurements.choiceTargets) assert(target.width >= 44 && target.height >= 44, `${label} 选项点击区域 ${target.text}`)
    assert.equal(measurements.navigationButtons.length, 5, `${label} 手机导航入口数量`)
    for (const button of measurements.navigationButtons) {
      assert(button.width >= 44 && button.height >= 44, `${label} 导航点击区域 ${button.text}`)
    }
    if (measurements.headerActions.length) assert(measurements.brand[0].right <= measurements.headerActions[0].x, `${label} 品牌与顶栏操作重叠`)
    for (const input of measurements.inputs) assert(Number.parseFloat(input.fontSize) >= 16, `${label} 输入文字尺寸`)
    for (const help of measurements.help) assert(help.width >= 44 && help.height >= 44, `${label} 帮助点击区域`)
    for (const dialog of measurements.dialogs) {
      assert(dialog.y >= 0 && dialog.bottom <= measurements.viewport.height, `${label} 弹窗超出可用高度`)
    }
    for (const control of measurements.controls) assert(control.width >= 44 && control.height >= 44, `${label} 操作点击区域 ${control.text}`)
    for (const actions of measurements.dialogActions) assert(actions.bottom <= measurements.viewport.height, `${label} 弹窗主要操作超出可用高度`)
  }
  await page.screenshot({ path: resolve(outputDirectory, `${label}.png`) })
  assert.equal(errors.length, 0, errors.join('\n'))
  return measurements
}

try {
  if (serverMode) {
    assert(process.env.MOBILE_UI_CREDENTIALS, '服务器验证需要指定测试账户凭据文件')
    const credentials = JSON.parse(await readFile(process.env.MOBILE_UI_CREDENTIALS, 'utf8'))
    assert(/^mobile-ui-.*@example\.test$/.test(credentials.email), "仅允许使用有 mobile-ui 标识的独立测试账户")
    await page.goto(`${baseURL}/login`)
    await page.locator('input[type="email"]').fill(credentials.email)
    await page.locator('input[type="password"]').fill(credentials.password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL('**/search')
  }
  if (process.env.MOBILE_UI_UPLOAD_FIXTURES === 'true') {
    const uploadRun = Date.now()
    for (const sourcePage of ['library', 'search']) {
      await navigate(sourcePage)
      const imagePath = resolve(outputDirectory, `mobile-ui-${uploadRun}-${sourcePage}.png`)
      await page.screenshot({ path: imagePath })
      await navigate('upload')
      await page.locator('input[type="file"]').setInputFiles(imagePath)
      await page.getByRole('button', { name: '上传所选图片', exact: true }).click()
      await page.getByLabel('直接上传', { exact: false }).check()
      const uploadResponse = page.waitForResponse((response) => new URL(response.url()).pathname === `${apiPrefix}/images/upload` && response.request().method() === 'POST')
      await page.getByRole('button', { name: '确认并提交', exact: true }).click()
      const response = await uploadResponse
      assert(response.ok(), `测试图片上传返回 HTTP ${response.status()}: ${await response.text()}`)
      const result = await response.json()
      assert(result.results?.length === 1 && result.results[0].ok === true, `图片接收结果：${JSON.stringify(result)}`)
      const headers = {}
      if (serverMode) {
        const sessionResponse = await context.request.get(`${baseURL}${apiPrefix}/auth/session`)
        assert(sessionResponse.ok(), `读取测试会话返回 HTTP ${sessionResponse.status()}`)
        const session = await sessionResponse.json()
        headers.Origin = baseURL
        headers['X-CSRF-Token'] = session.csrf_token
      }
      let uploadStatus
      await expect.poll(async () => {
        const statusResponse = await context.request.post(`${baseURL}${apiPrefix}/images/uploads/status`, { headers, data: { request_ids: [result.batch_id] } })
        assert(statusResponse.ok(), `读取上传状态返回 HTTP ${statusResponse.status()}: ${await statusResponse.text()}`)
        uploadStatus = (await statusResponse.json()).items[0]
        if (uploadStatus?.status === 'failed') throw new Error(`图片上传终态：${JSON.stringify(uploadStatus)}`)
        return uploadStatus?.status
      }, { timeout: 60000 }).toBe('succeeded')
      evidence.push({ label: `upload-${sourcePage}`, httpStatus: response.status(), result })
      evidence.push({ label: `upload-${sourcePage}-terminal`, uploadStatus })
      await page.waitForLoadState('networkidle')
      await expect(page.getByRole('dialog', { name: '图片处理选项' })).toHaveCount(0)
    }
  }
  const pageIds = serverMode ? ['search', 'library', 'collections', 'upload', 'tasks', 'account', 'subscription', 'redemption'] : ['search', 'library', 'collections', 'upload', 'tasks']
  for (const width of [320, 375, 390, 430, 600, 768, 1280]) {
    await page.setViewportSize({ width, height: width > 760 ? 900 : 844 })
    for (const pageId of pageIds) {
      await navigate(pageId)
      const measurements = await capture(`${pageId}-${width}`)
      if (pageId === 'library' && width === 390 && measurements.rows.length) {
        assert(measurements.rows[0].y <= 340, '图片库首张图片开始位置')
        if (measurements.rows.length >= 2) assert(measurements.rows[1].bottom <= measurements.navigation[0].y, '图片库首屏无法完整显示两张图片')
      }
      if (width <= 760) {
        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
        await capture(`${pageId}-bottom-${width}`)
        await page.evaluate(() => window.scrollTo(0, 0))
      }
    }
  }
  await page.setViewportSize({ width: 390, height: 844 })
  if (serverMode) {
    await navigate('search')
    const navigation = page.locator('.workspace-mobile-nav')
    await navigation.getByRole('button', { name: '图片库', exact: true }).click()
    await page.waitForURL('**/library')
    await navigation.getByRole('button', { name: '上传', exact: true }).click()
    await page.waitForURL('**/upload')
    await page.goBack()
    await page.waitForURL('**/library')
    await page.reload()
    await expect(navigation.getByRole('button', { name: '图片库', exact: true })).toHaveAttribute('aria-current', 'page')
    await navigation.getByRole('button', { name: '更多', exact: true }).click()
    await page.getByRole('dialog', { name: '更多', exact: true }).getByRole('button', { name: '合集', exact: true }).click()
    await page.waitForURL('**/collections')
    await expect(navigation.getByRole('button', { name: '更多', exact: true })).toHaveClass(/active/)
    await page.locator('.account-entry').click()
    await page.waitForURL('**/account')
    assert.equal(await navigation.locator('[aria-current="page"]').count(), 0, '账户页面的工作区激活状态')
    evidence.push({ label: 'route-history-refresh-account', passed: true })
  }
  await navigate('library')
  const management = page.locator('.library-management')
  const managementTrigger = management.getByRole("button", { name: "管理", exact: true })
  await expect(managementTrigger).toBeEnabled()
  await managementTrigger.focus()
  await expect(managementTrigger).toBeFocused()
  await page.keyboard.press("Enter")
  await expect(managementTrigger).toHaveAttribute("aria-expanded", "true")
  await capture("management-keyboard-open")
  await expect(management.getByRole("button", { name: "修复所有未就绪", exact: true })).toBeFocused()
  await page.keyboard.press("Tab")
  await expect(management.getByRole("button", { name: "生成检索缓存", exact: true })).toBeFocused()
  await page.keyboard.press("Shift+Tab")
  await expect(management.getByRole("button", { name: "修复所有未就绪", exact: true })).toBeFocused()
  await page.keyboard.press("Escape")
  await expect(managementTrigger).toBeFocused()
  evidence.push({ label: "management-keyboard-focus", passed: true })
  await managementTrigger.click()
  await expect(management.getByRole('button', { name: '修复所有未就绪', exact: true })).toBeVisible()
  await management.getByRole('button', { name: '修复所有未就绪', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '图片处理选项' })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 480 })
  await capture('processing-options-short')
  const lastOption = page.locator(".processing-options-fields .option-toggle").last()
  await lastOption.scrollIntoViewIfNeeded()
  await expect(lastOption).toBeInViewport({ ratio: 1 })
  const optionBounds = await lastOption.boundingBox()
  const actionBounds = await page.locator(".processing-options-actions").boundingBox()
  assert(optionBounds.y + optionBounds.height <= actionBounds.y, "短视口最后一个选项被确认区域遮挡")
  await capture("processing-options-short-scrolled")
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '图片处理选项' })).toHaveCount(0)
  assert.equal(await page.evaluate(() => document.body.style.overflow), '', '关闭弹窗后背景滚动状态')
  await page.setViewportSize({ width: 390, height: 844 })
  await management.getByRole('button', { name: '管理', exact: true }).click()
  if (await page.locator('.library-row').count()) {
    await page.getByRole('button', { name: '选择图片', exact: true }).click()
    await page.locator('.image-check').first().click()
    await expect(page.locator('.library-batch-actions')).toBeVisible()
    await capture('library-selected')
    if (process.env.MOBILE_UI_RETRY_FIXTURE === "true") {
      await page.locator(".image-check input:checked").uncheck()
      const fixture = page.locator(".library-row").filter({ hasText: /mobile-ui-/ }).first()
      await expect(fixture).toBeVisible()
      await fixture.locator(".image-check input").check()
      await page.getByRole("button", { name: /重试选中/ }).click()
      await page.getByRole("radio", { name: /指定部分/ }).check()
      await page.setViewportSize({ width: 390, height: 480 })
      await page.getByRole("checkbox", { name: /视觉向量生成/ }).check()
      await capture("selected-retry-short")
      const retryResponse = page.waitForResponse((response) => new URL(response.url()).pathname === `${apiPrefix}/images/stages/batch` && response.request().method() === "POST")
      await page.getByRole("button", { name: "重试已选部分（1）", exact: true }).click()
      const response = await retryResponse
      assert(response.ok(), `阶段重试返回 HTTP ${response.status()}: ${await response.text()}`)
      const result = await response.json()
      assert.equal(result.failed_count, 0, `阶段重试提交结果：${JSON.stringify(result)}`)
      assert.equal(result.submitted_count, 1, `阶段重试提交数量：${JSON.stringify(result)}`)
      await expect(page.getByRole("dialog", { name: "重试选中" })).toHaveCount(0)
      await expect(page.getByRole("button", { name: "刷新", exact: true })).toBeFocused()
      await expect(page.locator(".library-batch-actions")).not.toBeVisible()
      evidence.push({ label: "selected-retry-submit-focus", httpStatus: response.status(), result })
      for (const item of result.results) {
        assert(item.task_id, "阶段任务响应缺少 task_id")
        let task
        await expect.poll(async () => {
          const taskResponse = await context.request.get(`${baseURL}${apiPrefix}/tasks/${encodeURIComponent(item.task_id)}`)
          assert(taskResponse.ok(), `阶段任务查询返回 HTTP ${taskResponse.status()}`)
          task = await taskResponse.json()
          if (task.status === "failed" || task.status === "cancelled") throw new Error(`阶段任务终态：${JSON.stringify(task)}`)
          return task.status
        }, { timeout: 120000 }).toBe("succeeded")
        evidence.push({ label: "selected-retry-terminal", task })
      }
      await page.setViewportSize({ width: 390, height: 844 })
    }
    await page.getByRole('button', { name: '结束选择', exact: true }).click()
    await expect(page.locator('.library-batch-actions')).not.toBeVisible()
    await page.locator('.image-row-actions').first().getByRole('button', { name: '更多', exact: true }).click()
    await capture('library-image-actions')
    await page.locator('.image-row-actions').first().getByRole('button', { name: '更多', exact: true }).click()
    await page.locator('.metadata-button').first().click()
    await page.waitForLoadState('networkidle')
    await capture('library-image-details')
    await page.keyboard.press('Escape')
  }
  await page.locator('.workspace-mobile-nav').getByRole('button', { name: '更多', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '更多', exact: true }).getByRole('link', { name: /使用文档/ })).toBeVisible()
  await capture('navigation-more')
  await page.keyboard.press('Escape')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), '更多', '更多导航的焦点恢复')
  await navigate('search')
  await page.locator('.search-form input').fill(process.env.MOBILE_UI_SEARCH_QUERY || 'tieba')
  const searchResponse = page.waitForResponse((response) => new URL(response.url()).pathname === `${apiPrefix}/search` && response.request().method() === 'POST')
  await page.getByRole('button', { name: '开始检索', exact: true }).click()
  const searched = await searchResponse
  assert(searched.ok(), `真实检索返回 HTTP ${searched.status()}: ${await searched.text()}`)
  await expect(page.locator('.result-details-button').first()).toBeVisible({ timeout: 20000 })
  evidence.push({ label: 'search-http', httpStatus: searched.status() })
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 })
    await page.locator('.result-details-button').first().click()
    const details = await capture(`search-result-details-${width}`)
    assert(details.dialogImages[0].width >= details.dialogs[0].width - 26, '检索图片内容区域宽度')
    await page.setViewportSize({ width, height: 480 })
    await capture(`search-result-details-short-${width}`)
    await page.keyboard.press('Escape')
    await expect(page.locator('.result-details-button').first()).toBeFocused()
  }
  await writeFile(resolve(outputDirectory, 'results.json'), JSON.stringify({ baseURL, serverMode, evidence, errors }, null, 2))
  console.log(`已通过 ${evidence.length} 个真实页面状态检查；截图与尺寸记录：${outputDirectory}`)
} finally {
  const lastState = await page.evaluate(() => {
    const focused = document.activeElement
    return { url: location.href, title: document.title, text: document.body.innerText, focusedElement: focused ? { tagName: focused.tagName, role: focused.getAttribute("role"), label: focused.getAttribute("aria-label"), type: focused.getAttribute("type") } : null }
  })
  await writeFile(resolve(outputDirectory, "last-state.json"), JSON.stringify(lastState, null, 2))
  await page.screenshot({ path: resolve(outputDirectory, "last-state.png") })
  await writeFile(resolve(outputDirectory, 'latest-observations.json'), JSON.stringify({ evidence, errors }, null, 2))
  await browser.close()
}
