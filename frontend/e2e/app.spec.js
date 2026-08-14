/** Vite 开发入口的浏览器级工作流冒烟测试。 */
import { expect, test } from '@playwright/test'

test('首页可加载并切换核心工作区', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveTitle('MemeMeow')
  await expect(page.getByRole('heading', { name: '找到合适的表达' })).toBeVisible()
  await page.getByRole('button', { name: '图片库' }).click()
  await expect(page.getByRole('heading', { name: '图片库', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '上传' }).click()
  await expect(page.getByRole('heading', { name: '上传图片' })).toBeVisible()
  await page.getByRole('button', { name: '处理任务' }).click()
  await expect(page.getByRole('heading', { name: '处理任务' })).toBeVisible()
})

test('搜索表单拒绝空查询并保留工作区', async ({ page }) => {
  await page.goto('/')
  const submit = page.getByRole('button', { name: '开始检索' })
  await expect(submit).toBeDisabled()
  await page.getByPlaceholder('例如：开会时发现自己忘记准备材料').fill('测试查询')
  await expect(submit).toBeEnabled()
})

/** 使用真实 Chromium 验证延迟网络下仍保持图片写入手势，并读取系统剪贴板确认没有文本或 URL。 */
test('检索结果在延迟加载后仍只写入图片剪贴板', async ({ page, context }) => {
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64')
  await page.route('**/api/config', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ embedding_model: 'test-model', embedding_cache_ready: false }) }))
  const memeId = '66666666-6666-4666-8666-666666666666'
  await page.route('**/api/search', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ results: [`/media/${memeId}`] }) }))
  await page.route(`**/media/${memeId}`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 5500))
    await route.fulfill({ status: 200, contentType: 'image/png', body: png })
  })

  await page.goto('/')
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(page.url()).origin })
  const capabilities = await page.evaluate(() => ({
    secureContext: window.isSecureContext,
    clipboardWrite: typeof navigator.clipboard?.write === 'function',
    clipboardItem: typeof ClipboardItem === 'function',
    pngSupported: typeof ClipboardItem === 'function' && ClipboardItem.supports?.('image/png') === true,
  }))
  expect(capabilities).toEqual({ secureContext: true, clipboardWrite: true, clipboardItem: true, pngSupported: true })

  await page.getByPlaceholder('例如：开会时发现自己忘记准备材料').fill('测试查询')
  await page.getByRole('button', { name: '开始检索' }).click()
  await page.getByRole('button', { name: '复制检索结果 1' }).click()
  await expect(page.locator('.copy-notice')).toHaveText('图片已复制', { timeout: 12_000 })

  const clipboard = await page.evaluate(async () => {
    const items = await navigator.clipboard.read()
    const item = items[0]
    const image = item?.types.includes('image/png') ? await item.getType('image/png') : null
    return { types: item?.types || [], imageBytes: image ? (await image.arrayBuffer()).byteLength : 0 }
  })
  expect(clipboard.types).toEqual(['image/png'])
  expect(clipboard.imageBytes).toBeGreaterThan(0)
})
