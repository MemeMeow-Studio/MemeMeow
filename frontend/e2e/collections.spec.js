/** 使用真实 Chromium 验证合集列表、详情和响应式资产布局。 */
import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'

const image = readFileSync(new URL('../../legacy/streamlit-v1/screenshots/streamlit_vvquest.png', import.meta.url))
const collection = {
  collection_id: 'collection-1',
  name: '本周会议里的反应合集',
  member_count: 2,
  cover_media_url: '/media/member-1',
  cover_meme_id: 'member-1',
  cover_thumbnail: { status: 'available', media_url: '/media/member-1/thumbnail' },
}
const members = [
  {
    meme_id: 'member-1',
    filename: 'meeting-reaction-with-a-long-descriptive-file-name.png',
    extension: '.png',
    media_url: '/media/member-1',
    thumbnail: { status: 'available', media_url: '/media/member-1/thumbnail' },
  },
  {
    meme_id: 'member-2',
    filename: 'waiting-for-the-decision.jpg',
    extension: '.jpg',
    media_url: '/media/member-2',
    thumbnail: { status: 'available', media_url: '/media/member-2/thumbnail' },
  },
]

test('合集列表和详情在桌面、移动端保持可扫描且图片可见', async ({ page }) => {
  const consoleErrors = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  await page.route('**/api/config', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ embedding_model: 'test-model', embedding_cache_ready: true }),
  }))
  await page.route('**/api/collections**', (route) => {
    const path = new URL(route.request().url()).pathname
    const body = path.endsWith('/collection-1') ? { ...collection, members } : { items: [collection], total: 1, page: 1, page_size: 50 }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.route('**/api/images/metadata**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ filename: members[0].filename }),
  }))
  await page.route('**/media/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/media/member-2/thumbnail') return route.fulfill({ status: 200, contentType: 'image/png', body: Buffer.from('thumbnail unavailable') })
    return route.fulfill({ status: 200, contentType: 'image/png', body: image })
  })

  await page.goto('/')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('button', { name: '合集' }).click()
  await expect(page.getByRole('heading', { name: '合集', exact: true })).toBeVisible()
  await expect(page.locator('.collection-cover img')).toHaveCount(1)
  await expect(page.locator('.collection-cover img')).toHaveAttribute('src', /\/media\/member-1\/thumbnail$/)
  await expect.poll(() => page.locator('.collection-cover img').evaluate((image) => image.naturalWidth)).toBeGreaterThan(0)
  await page.screenshot({ path: '/home/infstellar/vscode/MemeMeow/frontend/test-results/collections-list-desktop.png', fullPage: true })

  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true)
  await page.screenshot({ path: '/home/infstellar/vscode/MemeMeow/frontend/test-results/collections-list-mobile.png', fullPage: true })

  await page.getByRole('button', { name: `打开合集 ${collection.name}` }).click()
  await expect(page.getByRole('heading', { name: collection.name })).toBeVisible()
  await expect(page.getByRole('button', { name: `从合集移除 ${members[0].filename}` })).toBeVisible()
  await expect(page.locator('.collection-asset-media img')).toHaveCount(2)
  await expect(page.locator('.collection-asset-media img').first()).toHaveAttribute('src', /\/media\/member-1\/thumbnail$/)
  await expect(page.locator('.collection-asset-media img').nth(1)).toHaveAttribute('src', /\/media\/member-2$/)
  await expect.poll(() => page.locator('.collection-asset-media img').first().evaluate((image) => image.naturalWidth)).toBeGreaterThan(0)
  await page.getByRole('button', { name: `查看 ${members[0].filename} 图片与元数据` }).click()
  await expect(page.getByRole('button', { name: '关闭图片预览' })).toBeVisible()
  await page.getByRole('button', { name: '关闭图片预览' }).click()
  await page.setViewportSize({ width: 320, height: 844 })
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: '/home/infstellar/vscode/MemeMeow/frontend/test-results/collections-detail-mobile.png', fullPage: true })

  await page.setViewportSize({ width: 1440, height: 1000 })
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true)
  await page.screenshot({ path: '/home/infstellar/vscode/MemeMeow/frontend/test-results/collections-detail-desktop.png', fullPage: true })
  await page.getByRole('button', { name: '返回合集列表' }).focus()
  await expect(page.getByRole('button', { name: '返回合集列表' })).toBeFocused()
  expect(consoleErrors).toEqual([])
})
