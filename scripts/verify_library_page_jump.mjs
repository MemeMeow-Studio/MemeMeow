/** 使用真实开发服务和测试账户验收图片库指定页码跳转。 */
import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'
import { chromium, expect } from '../frontend/node_modules/@playwright/test/index.mjs'

const configuration = JSON.parse(await readFile(process.argv[2], 'utf8'))
assert.ok(['http://127.0.0.1:8275', 'http://127.0.0.1:28275', 'https://mememeow-dev.stellarformation.cc'].includes(configuration.baseURL))
assert.match(configuration.prefix, /^test-library-filters-[a-f0-9]+$/)
const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox'] })
const context = await browser.newContext({ baseURL: configuration.baseURL, viewport: { width: 1280, height: 900 } })
const page = await context.newPage()
const errors = []
const results = []
const imageRequests = []
const mutationRequests = []
page.on('pageerror', error => errors.push(error.message))
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
page.on('requestfailed', request => {
  if (new URL(request.url()).pathname === '/images') errors.push(`图片列表请求错误：${request.failure()?.errorText}`)
})
page.on('request', request => {
  const pathname = new URL(request.url()).pathname
  if (pathname === '/images' && request.method() === 'GET') imageRequests.push(request.url())
  if (pathname.startsWith('/images') && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())) mutationRequests.push(request.url())
})

/** 等待界面操作的真实分页响应，并检查图片数量、内容和请求条件。 */
async function actionAndList(action, expectedPage, view) {
  const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname === '/images' && response.request().method() === 'GET')
  await action()
  const response = await responsePromise
  assert.ok(response.ok(), `HTTP ${response.status()}: ${await response.text()}`)
  const payload = await response.json()
  const requestUrl = new URL(response.url())
  assert.equal(requestUrl.searchParams.get('page'), String(expectedPage))
  assert.equal(requestUrl.searchParams.get('page_size'), await page.getByLabel('每页图片数量').inputValue())
  assert.equal(requestUrl.searchParams.get('search'), configuration.prefix)
  assert.equal(requestUrl.searchParams.get('sort'), await page.getByLabel('图片排序方式').inputValue())
  assert.equal(requestUrl.searchParams.get('status') || 'all', await page.getByLabel('图片状态类别').inputValue())
  assert.equal(payload.page, expectedPage)
  const pagination = page.getByRole('navigation', { name: '图片库分页' })
  await expect(pagination).toContainText(`第 ${expectedPage} /`)
  await expect(pagination.getByLabel('目标页码')).toHaveValue(String(expectedPage))
  const items = page.locator(view === '方块' ? '.library-image-tile' : '.library-row')
  await expect(items).toHaveCount(payload.items.length)
  const names = await items.locator(view === '方块' ? '.library-tile-name' : '.file-meta strong').allTextContents()
  assert.deepEqual(names, payload.items.map(image => image.display_name || image.filename))
  assert.equal(await items.locator('input:checked').count(), 0)
  return payload
}

/** 输入指定页码，通过按钮或 Enter 触发真实分页请求。 */
async function jump(target, view, useEnter = false) {
  const pagination = page.getByRole('navigation', { name: '图片库分页' })
  const input = pagination.getByLabel('目标页码')
  await input.fill(String(target))
  return actionAndList(() => useEnter ? input.press('Enter') : pagination.getByRole('button', { name: '跳转', exact: true }).click(), target, view)
}

try {
  if (configuration.email) {
    assert.match(configuration.email, /^test-library-filters-[a-f0-9]+@example\.test$/)
    await page.goto('/login')
    await page.getByLabel('邮箱', { exact: true }).fill(configuration.email)
    await page.getByLabel('密码', { exact: true }).fill(configuration.password)
    const loginResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/auth/sessions')
    await page.getByRole('button', { name: '登录', exact: true }).click()
    const response = await loginResponse
    assert.ok(response.ok(), `登录 HTTP ${response.status()}: ${await response.text()}`)
  }
  await page.goto(configuration.libraryPath || '/library')
  await expect(page.getByRole('heading', { name: '图片库', exact: true })).toBeVisible()
  const bulletin = page.locator('.bulletin-dialog')
  if (await bulletin.isVisible()) await bulletin.getByRole('button', { name: '确认', exact: true }).click()
  await page.evaluate(() => localStorage.setItem('mememeow.library.page-size', '20'))
  await page.reload()
  const filter = page.getByLabel('筛选文件名', { exact: true })
  await filter.fill(configuration.prefix)
  const first = await actionAndList(() => filter.press('Enter'), 1, '方块')
  const sort = page.getByLabel('图片排序方式')
  if (await sort.inputValue() !== 'name_asc') await actionAndList(() => sort.selectOption('name_asc'), 1, '方块')
  const lastPage = Math.ceil(first.total / 20)
  assert.ok(lastPage >= 3, `验收需要至少三页测试图片，当前数量：${first.total}`)

  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport)
    for (const view of ['方块', '列表']) {
      await page.getByRole('button', { name: view, exact: true }).click()
      const pagination = page.getByRole('navigation', { name: '图片库分页' })
      const input = pagination.getByLabel('目标页码')
      const jumpButton = pagination.getByRole('button', { name: '跳转', exact: true })
      await expect(input).toBeVisible()
      await expect(jumpButton).toBeDisabled()
      const selection = page.getByRole('button', { name: '选择图片', exact: true })
      if (await selection.isVisible()) await selection.click()
      await page.locator(view === '方块' ? '.library-image-tile' : '.library-row').first().getByRole('checkbox').check()
      await jump(lastPage, view)
      await expect(pagination.getByRole('button', { name: '下一页', exact: true })).toBeDisabled()
      await jump(1, view, true)
      await expect(pagination.getByRole('button', { name: '上一页', exact: true })).toBeDisabled()
      await jump(3, view)
      await actionAndList(() => pagination.getByRole('button', { name: '上一页', exact: true }).click(), 2, view)
      await actionAndList(() => pagination.getByRole('button', { name: '下一页', exact: true }).click(), 3, view)

      for (const value of ['', '0', '-1', '1.5', String(lastPage + 1), '3']) {
        const beforeRequests = imageRequests.length
        await input.fill(value)
        await expect(jumpButton).toBeDisabled()
        await input.press('Enter')
        await page.waitForLoadState('networkidle')
        assert.equal(imageRequests.length, beforeRequests)
        await expect(pagination).toContainText('第 3 /')
      }
      await jump(1, view)
      const bounds = await pagination.locator('form').evaluate(form => {
        const rectangle = form.getBoundingClientRect()
        const inputRectangle = form.querySelector('input').getBoundingClientRect()
        return { left: rectangle.left, right: rectangle.right, inputLeft: inputRectangle.left, inputRight: inputRectangle.right, viewport: innerWidth }
      })
      assert.ok(bounds.left >= 0 && bounds.right <= bounds.viewport, '页码输入超出页面宽度')
      assert.ok(bounds.inputLeft >= bounds.left && bounds.inputRight <= bounds.right, '页码输入超出表单宽度')
      results.push({ viewport: viewport.width, view, lastPage, checks: ['末页跳转', 'Enter 返回首页', '跨页选择清理', '前后翻页同步', '无效及当前页禁止请求', '页面宽度'] })
    }
  }

  const status = page.getByLabel('图片状态类别')
  const unready = await actionAndList(() => status.selectOption('unready'), 1, '列表')
  assert.ok(unready.total > 20, '状态筛选验收需要至少两页未就绪测试图片')
  await jump(2, '列表')
  await actionAndList(() => page.getByLabel('每页图片数量').selectOption('50'), 1, '列表')
  await expect(page.getByLabel('目标页码')).toHaveAttribute('max', String(Math.ceil(unready.total / 50)))
  await actionAndList(() => status.selectOption('all'), 1, '列表')
  await actionAndList(() => page.getByLabel('每页图片数量').selectOption('20'), 1, '列表')
  await jump(lastPage, '列表')
  const missingFilter = `${configuration.prefix}-page-jump-no-match`
  await filter.fill(missingFilter)
  const emptyResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/images')
  await filter.press('Enter')
  const response = await emptyResponse
  assert.ok(response.ok(), `HTTP ${response.status()}: ${await response.text()}`)
  assert.equal((await response.json()).total, 0)
  await expect(page.locator('.library-row')).toHaveCount(0)
  await expect(page.getByLabel('每页图片数量')).toBeEnabled()
  await filter.fill(configuration.prefix)
  await actionAndList(() => filter.press('Enter'), 1, '列表')
  await expect(page.getByLabel('目标页码')).toHaveAttribute('max', String(lastPage))
  assert.deepEqual(errors, [])
  assert.deepEqual(mutationRequests, [])
  const report = { status: 'passed', total: first.total, results, statusFilterRetained: true, pageSizeReset: true, emptyFilterReset: true, errors, mutationRequests }
  if (process.argv[3]) await writeFile(process.argv[3], JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report, null, 2))
} finally {
  await browser.close()
}
