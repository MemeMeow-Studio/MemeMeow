/** Vite 开发入口的浏览器级工作流冒烟测试。 */
import { expect, test } from '@playwright/test'

// 真实浏览器拖放需要构造可被 DataTransfer 接受的本地图片字节，而不是只伪造文件名。
const ONE_PIXEL_PNG_BYTES = Array.from(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'))

/** 在上传区域派发带有真实 File 对象的原生拖放事件，并返回默认行为是否已被阻止。 */
async function dispatchFileDrag(page, type, files) {
  return page.locator('.drop-zone').evaluate((element, payload) => {
    const dataTransfer = new DataTransfer()
    for (const entry of payload.files) {
      dataTransfer.items.add(new File([Uint8Array.from(entry.bytes)], entry.name, { type: entry.type }))
    }
    const event = new DragEvent(payload.type, { bubbles: true, cancelable: true, dataTransfer })
    element.dispatchEvent(event)
    return event.defaultPrevented
  }, { type, files })
}

/** 读取上传工作区在窄屏下的横向边界和关键行的矩形，避免只凭 CSS 类名判断布局。 */
async function readUploadQueueLayout(page) {
  return page.evaluate(() => {
    const nodes = [
      document.querySelector('.drop-zone'),
      document.querySelector('.upload-panel > .primary'),
      ...document.querySelectorAll('.upload-pending-item'),
    ].filter(Boolean)
    const rects = nodes.map((node) => {
      const rect = node.getBoundingClientRect()
      return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height }
    })
    const overlaps = (left, right) => left.left < right.right - 1
      && right.left < left.right - 1
      && left.top < right.bottom - 1
      && right.top < left.bottom - 1
    const noOverlap = rects.every((rect, index) => rects.slice(index + 1).every((other) => !overlaps(rect, other)))
    return {
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      withinViewport: rects.every((rect) => rect.left >= -1 && rect.right <= window.innerWidth + 1 && rect.width > 0 && rect.height > 0),
      noOverlap,
      rects,
    }
  })
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/config', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ embedding_model: 'test-model', embedding_cache_ready: true, reverse_image_available: true }),
  }))
  await page.route('**/api/images?**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 50 }),
  }))
  await page.route('**/api/tasks?**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [], next_cursor: null }),
  }))
  await page.route('https://api.github.com/repos/MemeMeow-Studio/MemeMeow', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ stargazers_count: 1260 }),
  }))
  await page.route('https://api.github.com/repos/MemeMeow-Studio/MemeMeow/commits?per_page=1', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{ sha: '543d876521581ecf2baa88c80814e4cedae83252' }]),
  }))
})

test('上传选项先取消再确认，并发送两项处理选项', async ({ page }) => {
  const uploadRequests = []
  await page.route('**/api/images/upload', async (route) => {
    uploadRequests.push(route.request())
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [{ meme_id: 'meme-upload', filename: 'sample.png', ok: true, saved_filename: 'sample.png' }] }),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: '上传' }).click()
  await page.setInputFiles('input[type="file"]', { name: 'sample.png', mimeType: 'image/png', buffer: Buffer.from([1, 2, 3]) })
  await page.getByRole('button', { name: '上传所选图片' }).click()
  const dialog = page.getByRole('dialog', { name: '图片处理选项' })
  await expect(dialog).toBeVisible()
  expect(uploadRequests).toHaveLength(0)

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  expect(uploadRequests).toHaveLength(0)

  await page.getByRole('button', { name: '上传所选图片' }).click()
  await dialog.getByRole('radio', { name: /按需允许联网/ }).check()
  await dialog.getByRole('checkbox', { name: '按标题自动命名' }).check()
  await dialog.getByRole('button', { name: '确认并提交' }).click()
  await expect.poll(() => uploadRequests.length).toBe(1)
  const body = uploadRequests[0].postData() || ''
  expect(body).toContain('name="reverse_image_policy"')
  expect(body).toContain('name="auto_name"')
  expect(body).toContain('auto')
  expect(body).toContain('true')
})

/** 使用真实键盘操作验证文件选择输入和待上传移除按钮均可访问。 */
test('上传队列支持键盘选择和移除，并清空原生文件输入值', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '上传' }).click()
  const input = page.getByLabel('选择图片文件')
  await input.focus()
  await expect(input).toBeFocused()

  const chooserPromise = page.waitForEvent('filechooser')
  await input.press('Enter')
  const chooser = await chooserPromise
  await chooser.setFiles({ name: 'keyboard.png', mimeType: 'image/png', buffer: Buffer.from(ONE_PIXEL_PNG_BYTES) })

  const pending = page.locator('.upload-pending-item')
  await expect(pending).toHaveCount(1)
  await expect(input).toHaveValue('')
  const remove = pending.getByRole('button', { name: '移除待上传图片 keyboard.png' })
  await remove.focus()
  await expect(remove).toBeFocused()
  await remove.press('Enter')
  await expect(pending).toHaveCount(0)
})

/** 使用真实图片拖放核对按序追加、本地解码、确认前移除和窄屏可操作性。 */
test('拖放图片按序追加本地预览，可删除待上传项并只提交剩余集合', async ({ page }) => {
  const uploadRequests = []
  const mediaRequests = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/media/')) mediaRequests.push(request)
  })
  await page.route('**/api/images/upload', async (route) => {
    uploadRequests.push(route.request())
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [{ meme_id: 'meme-first', filename: 'first.png', ok: true, saved_filename: 'first.png' }] }),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: '上传' }).click()
  const first = { name: 'first.png', type: 'image/png', bytes: ONE_PIXEL_PNG_BYTES }
  const second = { name: 'second.png', type: 'image/png', bytes: ONE_PIXEL_PNG_BYTES }
  const dropZone = page.locator('.drop-zone')

  expect(await dispatchFileDrag(page, 'dragenter', [first])).toBe(true)
  await expect(dropZone).toHaveClass(/is-dragging/)
  expect(await dispatchFileDrag(page, 'dragenter', [second])).toBe(true)
  expect(await dispatchFileDrag(page, 'dragleave', [second])).toBe(true)
  await expect(dropZone).toHaveClass(/is-dragging/)
  expect(await dispatchFileDrag(page, 'dragover', [first, second])).toBe(true)
  expect(await dispatchFileDrag(page, 'drop', [first, second])).toBe(true)
  await expect(dropZone).not.toHaveClass(/is-dragging/)
  expect(uploadRequests).toHaveLength(0)

  const pending = page.locator('.upload-pending-item')
  await expect(pending).toHaveCount(2)
  await expect(pending.nth(0).locator('strong')).toHaveText('first.png')
  await expect(pending.nth(1).locator('strong')).toHaveText('second.png')
  for (let index = 0; index < 2; index += 1) {
    const image = pending.nth(index).locator('img')
    await expect(image).toHaveAttribute('src', /^blob:/)
    await expect.poll(() => image.evaluate((element) => ({
      complete: element.complete,
      naturalWidth: element.naturalWidth,
      naturalHeight: element.naturalHeight,
    }))).toEqual({ complete: true, naturalWidth: 1, naturalHeight: 1 })
  }

  const desktopLayout = await readUploadQueueLayout(page)
  expect(desktopLayout.documentWidth).toBeLessThanOrEqual(desktopLayout.viewportWidth)
  expect(desktopLayout.withinViewport).toBe(true)
  expect(desktopLayout.noOverlap).toBe(true)
  await page.screenshot({ path: '/home/infstellar/vscode/MemeMeow/frontend/test-results/upload-queue-desktop.png', fullPage: true })

  await pending.nth(1).getByRole('button', { name: '移除待上传图片 second.png' }).click()
  await expect(pending).toHaveCount(1)
  await expect(pending.first().locator('strong')).toHaveText('first.png')
  await expect(page.getByText('second.png', { exact: true })).toHaveCount(0)
  expect(uploadRequests).toHaveLength(0)

  await page.setViewportSize({ width: 320, height: 844 })
  const mobileLayout = await readUploadQueueLayout(page)
  expect(mobileLayout.viewportWidth).toBe(320)
  expect(mobileLayout.documentWidth).toBeLessThanOrEqual(320)
  expect(mobileLayout.withinViewport).toBe(true)
  expect(mobileLayout.noOverlap).toBe(true)
  await expect(pending.first().locator('img')).toBeVisible()
  await page.screenshot({ path: '/home/infstellar/vscode/MemeMeow/frontend/test-results/upload-queue-mobile.png', fullPage: true })

  await page.getByRole('button', { name: '上传所选图片' }).click()
  const dialog = page.getByRole('dialog', { name: '图片处理选项' })
  await expect(dialog).toBeVisible()
  expect(uploadRequests).toHaveLength(0)
  await dialog.getByRole('button', { name: '确认并提交' }).click()
  await expect.poll(() => uploadRequests.length).toBe(1)
  const body = uploadRequests[0].postData() || ''
  expect(body).toContain('filename="first.png"')
  expect(body).not.toContain('filename="second.png"')
  await expect(page.locator('.upload-result')).toContainText('first.png')
  expect(mediaRequests).toHaveLength(0)
})

/** 以无空格长文件名验证 320px 视口下文件名换行且队列不产生横向溢出。 */
test('窄屏长文件名待上传行不越界', async ({ page }) => {
  await page.goto('/')
  await page.setViewportSize({ width: 320, height: 844 })
  await page.getByRole('button', { name: '上传' }).click()
  const longName = `${'very-long-uploaded-image-name-'.repeat(8)}.png`
  expect(await dispatchFileDrag(page, 'drop', [{ name: longName, type: 'image/png', bytes: ONE_PIXEL_PNG_BYTES }])).toBe(true)

  const pending = page.locator('.upload-pending-item')
  await expect(pending).toHaveCount(1)
  await expect(pending.locator('strong')).toHaveText(longName)
  const layout = await readUploadQueueLayout(page)
  expect(layout.viewportWidth).toBe(320)
  expect(layout.documentWidth).toBeLessThanOrEqual(320)
  expect(layout.withinViewport).toBe(true)
  expect(layout.noOverlap).toBe(true)
})

/** 坏图片只能使本地预览回退，不能抹掉文件名、移除能力或触发服务端媒体请求。 */
test('预览解码失败仍保留文件名和移除动作且不请求服务端媒体', async ({ page }) => {
  const uploadRequests = []
  const mediaRequests = []
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/media/')) mediaRequests.push(request)
  })
  await page.route('**/api/images/upload', (route) => {
    uploadRequests.push(route.request())
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [] }),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: '上传' }).click()
  const broken = { name: 'broken.png', type: 'image/png', bytes: [0, 1, 2, 3, 4, 5] }
  expect(await dispatchFileDrag(page, 'drop', [broken])).toBe(true)
  const pending = page.locator('.upload-pending-item')
  await expect(pending).toHaveCount(1)
  await expect(pending.getByRole('img', { name: '无法预览 broken.png' })).toBeVisible()
  await expect(pending.locator('img')).toHaveCount(0)
  await expect(pending.locator('strong')).toHaveText('broken.png')
  await expect(pending.getByRole('button', { name: '移除待上传图片 broken.png' })).toBeEnabled()
  expect(uploadRequests).toHaveLength(0)
  expect(mediaRequests).toHaveLength(0)

  await pending.getByRole('button', { name: '移除待上传图片 broken.png' }).click()
  await expect(pending).toHaveCount(0)
  expect(uploadRequests).toHaveLength(0)
  expect(mediaRequests).toHaveLength(0)
})

/** 上传请求保持挂起时，拖放仍须阻止浏览器默认导航并冻结活动批次。 */
test('上传中拖放不修改活动批次', async ({ page }) => {
  const uploadRequests = []
  let releaseUpload
  const uploadGate = new Promise((resolve) => { releaseUpload = resolve })
  await page.route('**/api/images/upload', async (route) => {
    uploadRequests.push(route.request())
    await uploadGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [{ meme_id: 'meme-active', filename: 'active.png', ok: true, saved_filename: 'active.png' }] }),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: '上传' }).click()
  await page.setInputFiles('input[type="file"]', { name: 'active.png', mimeType: 'image/png', buffer: Buffer.from(ONE_PIXEL_PNG_BYTES) })
  await page.getByRole('button', { name: '上传所选图片' }).click()
  await page.getByRole('dialog', { name: '图片处理选项' }).getByRole('button', { name: '确认并提交' }).click()
  await expect.poll(() => uploadRequests.length).toBe(1)

  const activeDrop = { name: 'added-while-uploading.png', type: 'image/png', bytes: ONE_PIXEL_PNG_BYTES }
  expect(await dispatchFileDrag(page, 'drop', [activeDrop])).toBe(true)
  await expect(page.locator('input[type="file"]')).toBeDisabled()
  await expect(page.locator('.upload-results')).toContainText('active.png')
  await expect(page.locator('.upload-results')).not.toContainText('added-while-uploading.png')
  expect(uploadRequests).toHaveLength(1)

  releaseUpload()
  await expect(page.locator('.upload-results')).toContainText('完成')
})

test('完整重试使用 scope 端点，取消不请求且 320px 弹层不越界', async ({ page }) => {
  const retryRequests = []
  await page.route('**/api/images/processing/unready', async (route) => {
    retryRequests.push(route.request())
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ target_count: 1, submitted_count: 1, reused_count: 0, conflict_count: 0, failed_count: 0, results: [{ meme_id: 'meme-1', processing_job_id: 'job-1', status: 'queued' }] }),
    })
  })

  await page.goto('/')
  await page.setViewportSize({ width: 320, height: 844 })
  await page.getByRole('button', { name: '图片库' }).click()
  await page.getByRole('button', { name: '完整重试所有未就绪' }).click()
  const dialog = page.getByRole('dialog', { name: '图片处理选项' })
  await expect(dialog).toBeVisible()
  expect(retryRequests).toHaveLength(0)
  const box = await dialog.boundingBox()
  expect(box).not.toBeNull()
  expect(box.x + box.width).toBeLessThanOrEqual(320)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  expect(retryRequests).toHaveLength(0)

  await page.getByRole('button', { name: '完整重试所有未就绪' }).click()
  await dialog.getByRole('button', { name: '确认并提交' }).click()
  await expect.poll(() => retryRequests.length).toBe(1)
  expect(retryRequests[0].postDataJSON()).toEqual({ reverse_image_policy: 'forbid', auto_name: false })
  await expect(page.locator('.inline-notice')).toContainText('完整重试：目标 1，提交 1')
})

/** 使用真实浏览器核对默认选择、指定阶段多选和桌面/移动弹层布局。 */
test('重试选中默认可选择并按指定阶段提交', async ({ page }) => {
  const stageRequests = []
  const fullRetryRequests = []
  const consoleErrors = []
  page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  page.on('pageerror', (error) => { consoleErrors.push(error.message) })
  await page.route('**/api/images?**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      items: [
        { meme_id: '55555555-5555-4555-8555-555555555555', filename: 'pending.png', size: 1024, extension: '.png', media_url: 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=', metadata: { status: 'pending' }, embedding_status: 'pending', visual_embedding_status: 'pending' },
        { meme_id: '66666666-6666-4666-8666-666666666666', filename: 'ready.png', size: 2048, extension: '.png', media_url: 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=', metadata: { status: 'ready' }, embedding_status: 'ready', visual_embedding_status: 'ready' },
      ],
      total: 2,
      page: 1,
      page_size: 50,
    }),
  }))
  await page.route('**/api/images/stages/batch', async (route) => {
    stageRequests.push(route.request())
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ target_count: 2, submitted_count: 2, failed_count: 0, results: [] }),
    })
  })
  await page.route('**/api/images/context/batch', async (route) => {
    fullRetryRequests.push(route.request())
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ results: [{ meme_id: '55555555-5555-4555-8555-555555555555', task_id: 'job-1' }] }),
    })
  })

  await page.goto('/')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('button', { name: '图片库' }).click()
  await expect(page.getByRole('button', { name: '选择图片', exact: true })).toHaveCount(0)
  await expect(page.locator('.image-check input')).toHaveCount(2)
  await expect(page.getByRole('button', { name: /^重试选中/ })).toBeDisabled()

  await page.locator('.image-check input').first().check()
  const retryButton = page.getByRole('button', { name: /^重试选中/ })
  expect(stageRequests).toHaveLength(0)
  expect(fullRetryRequests).toHaveLength(0)
  await retryButton.click()
  const dialog = page.getByRole('dialog', { name: '重试选中' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('已选 1 张图片')
  await expect(dialog.getByRole('button', { name: '完整重试' })).toBeEnabled()
  expect(stageRequests).toHaveLength(0)
  expect(fullRetryRequests).toHaveLength(0)
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(retryButton).toBeFocused()
  expect(stageRequests).toHaveLength(0)
  expect(fullRetryRequests).toHaveLength(0)

  await retryButton.click()
  await expect(dialog).toBeVisible()
  await page.screenshot({ path: '/home/infstellar/vscode/MemeMeow/frontend/test-results/retry-selected-desktop.png', fullPage: true })

  await dialog.getByRole('radio', { name: '指定部分' }).check()
  await dialog.getByRole('checkbox', { name: '图片语境' }).check()
  await dialog.getByRole('checkbox', { name: '文本索引' }).check()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true)
  await page.screenshot({ path: '/home/infstellar/vscode/MemeMeow/frontend/test-results/retry-selected-mobile.png', fullPage: true })
  await dialog.getByRole('button', { name: '重试已选部分（2）' }).click()
  const optionsDialog = page.getByRole('dialog', { name: '图片处理选项' })
  await expect(optionsDialog).toBeVisible()
  await optionsDialog.getByRole('button', { name: '确认并提交' }).click()
  await expect.poll(() => stageRequests.length).toBe(1)
  expect(stageRequests[0].postDataJSON()).toEqual({
    items: [{ meme_id: '55555555-5555-4555-8555-555555555555' }],
    stages: ['agent', 'text_embedding'],
    reverse_image_policy: 'forbid',
    auto_name: false,
  })
  expect(consoleErrors).toEqual([])
})

test('首页可加载并切换核心工作区', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveTitle('MemeMeow')
  await expect(page.getByText('API 已连接')).toBeVisible()
  await expect(page.getByRole('heading', { name: '通过自然语言检索表情包' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'MemeMeow GitHub 仓库' })).toHaveAttribute('href', 'https://github.com/MemeMeow-Studio/MemeMeow')
  await expect(page.locator('.repository-stars')).toHaveText('1,260')
  await expect(page.locator('.repository-hash')).toHaveText('543d876')
  await page.getByRole('button', { name: '图片库' }).click()
  await expect(page.getByRole('heading', { name: '图片库', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '上传' }).click()
  await expect(page.getByRole('heading', { name: '上传图片' })).toBeVisible()
  await page.getByRole('button', { name: '处理任务' }).click()
  await expect(page.getByRole('heading', { name: '处理任务' })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('搜索表单拒绝空查询并保留工作区', async ({ page }) => {
  await page.goto('/')
  const submit = page.getByRole('button', { name: '开始检索' })
  await expect(submit).toBeDisabled()
  await page.getByPlaceholder('例如：开会时发现自己忘记准备材料').fill('测试查询')
  await expect(submit).toBeEnabled()
})

/** 使用真实浏览器核对文本索引和图片视觉向量不会被混成同一个状态。 */
test('图片库分别显示文本索引和图片向量状态', async ({ page }) => {
  await page.route('**/api/images?**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      items: [
        { meme_id: '33333333-3333-4333-8333-333333333333', filename: 'pending.png', size: 1024, extension: '.png', media_url: '/media/33333333-3333-4333-8333-333333333333', metadata: { status: 'pending' }, embedding_status: 'pending', visual_embedding_status: 'ready' },
        { meme_id: '44444444-4444-4444-8444-444444444444', filename: 'ready.png', size: 2048, extension: '.png', media_url: '/media/44444444-4444-4444-8444-444444444444', metadata: { status: 'ready' }, embedding_status: 'ready', visual_embedding_status: 'pending' },
      ],
      total: 2,
      page: 1,
      page_size: 50,
    }),
  }))

  await page.goto('/')
  await page.getByRole('button', { name: '图片库' }).click()
  await expect(page.locator('.embedding-state')).toHaveText(['文本索引待生成', '文本索引已就绪'])
  await expect(page.locator('.visual-embedding-state')).toHaveText(['图片向量已就绪', '图片向量待生成'])
})

/** 使用真实浏览器确认元数据面板内容超出可视区域时仍可滚动到底部。 */
test('图片预览元数据面板可滚动查看底部详情', async ({ page }) => {
  await page.route('**/api/images?**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      items: [{
        meme_id: 'meme-metadata-scroll',
        filename: 'metadata-scroll.png',
        size: 2048,
        extension: '.png',
        media_url: 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=',
        metadata: { status: 'ready' },
        processing_status: 'succeeded',
        processing_stages: [{ stage: 'agent', status: 'succeeded' }],
      }],
      total: 1,
      page: 1,
      page_size: 50,
    }),
  }))
  await page.route('**/api/images/metadata**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schema_version: 1,
      image: { relative_path: 'metadata-scroll.png', extension: '.png', size_bytes: 2048 },
      context_status: 'ready',
      meme_context: {
        title: '可滚动元数据',
        summary: '这是一段足够长的摘要，用于确认元数据主体能够在有限高度内滚动。'.repeat(10),
        subjects: ['主体'],
        visible_text: ['图片文字'],
        meaning: '含义',
        keywords: ['关键词'],
        source_urls: ['https://example.com/metadata-scroll'],
        references: ['文本引用'],
        uncertainties: ['底部不确定项'],
      },
      provenance: { producer: 'agent', updated_at: '2026-08-26T08:30:00Z' },
    }),
  }))

  await page.goto('/')
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.getByRole('button', { name: '图片库' }).click()
  await page.getByRole('button', { name: '查看 metadata-scroll.png 图片与详情' }).click()
  const metadataDetails = page.locator('.metadata-details')
  await metadataDetails.first().locator('summary').click()
  await expect.poll(() => page.locator('.metadata-panel-body').evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)

  const scrollState = await page.locator('.metadata-panel-body').evaluate((element) => {
    element.scrollTop = element.scrollHeight
    const bottomGroup = element.querySelector('.metadata-detail-group:last-child')
    if (!bottomGroup) return null
    const bodyRect = element.getBoundingClientRect()
    const groupRect = bottomGroup.getBoundingClientRect()
    return { scrollTop: element.scrollTop, bottomVisible: groupRect.bottom <= bodyRect.bottom + 1 }
  })
  expect(scrollState).toEqual({ scrollTop: expect.any(Number), bottomVisible: true })
  expect(scrollState.scrollTop).toBeGreaterThan(0)
})

/** 使用真实 Chromium 验证延迟网络下仍保持图片写入手势，并读取系统剪贴板确认没有文本或 URL。 */
test('检索结果在延迟加载后仍只写入图片剪贴板', async ({ page, context }) => {
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64')
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

/** 使用真实浏览器核对父 Job 默认折叠、子任务图片抽屉和窄屏布局。 */
test('处理任务父 Job 默认折叠并在子任务详情显示图片', async ({ page }) => {
  const mediaUrl = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='
  await page.route('**/api/tasks?**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [], next_cursor: null }),
  }))
  await page.route('**/api/images/processing?**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [{
      task_id: 'job-task',
      task_type: 'image_processing',
      job_id: 'job-1',
      meme_id: 'meme-1',
      processing_job_id: 'job-1',
      revision: 1,
      image_sha256: 'a'.repeat(64),
      reverse_image_policy: 'forbid',
      status: 'succeeded',
      stages: [{ stage: 'visual', status: 'succeeded', task_id: 'visual-1' }],
    }] }),
  }))
  await page.route('**/api/tasks/visual-1', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      task_id: 'visual-1',
      task_type: 'visual_embedding_generation',
      submission_mode: 'pipeline',
      image_stage: 'visual',
      processing_job_id: 'job-1',
      status: 'succeeded',
      image: { meme_id: 'meme-1', filename: 'sample.png', media_url: mediaUrl },
    }),
  }))

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await page.getByRole('button', { name: '处理任务' }).click()

  const parent = page.locator('.processing-job').first()
  await expect(parent).toBeVisible()
  await expect.poll(() => parent.evaluate((element) => element.open)).toBe(false)
  await parent.locator('summary').click()
  await expect.poll(() => parent.evaluate((element) => element.open)).toBe(true)

  await parent.locator('.task-stage-row').click()
  const drawer = page.getByRole('dialog', { name: '任务详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer.locator('.task-image-preview img')).toHaveAttribute('src', mediaUrl)
  await expect(drawer.locator('.task-image-preview img')).toHaveAttribute('alt', '处理图片：sample.png')

  const desktopBox = await drawer.boundingBox()
  expect(desktopBox).not.toBeNull()
  expect(desktopBox.x + desktopBox.width).toBeLessThanOrEqual(1440)

  await page.setViewportSize({ width: 320, height: 844 })
  const mobileBox = await drawer.boundingBox()
  expect(mobileBox).not.toBeNull()
  expect(mobileBox.x + mobileBox.width).toBeLessThanOrEqual(320)
  await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true)
})
