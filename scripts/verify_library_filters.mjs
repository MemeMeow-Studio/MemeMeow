/** 使用真实服务和带测试标识的数据验收图片库分页、状态筛选与页内选择。 */
import assert from "node:assert/strict"
import { readFile, writeFile } from "node:fs/promises"
import { chromium, expect } from "../frontend/node_modules/@playwright/test/index.mjs"

const configuration = JSON.parse(await readFile(process.argv[2], "utf8"))
assert.ok(["http://127.0.0.1:8275", "http://127.0.0.1:28275", "https://mememeow-dev.stellarformation.cc"].includes(configuration.baseURL))
assert.match(configuration.prefix, /^test-library-filters-[a-f0-9]+$/)
assert.equal(configuration.images.length, 201)
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ baseURL: configuration.baseURL, viewport: { width: 1280, height: 900 } })
const page = await context.newPage()
const pageErrors = []
const mutationRequests = []
const checks = []
page.on("pageerror", error => pageErrors.push(error.message))
page.on("request", request => {
  if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method()) && new URL(request.url()).pathname.includes("/images")) mutationRequests.push(request.url())
})

/** 对真实列表请求检查响应，完整保留 HTTP 状态与服务端错误。 */
async function list(parameters) {
  const response = await context.request.get(`/images?${new URLSearchParams(parameters)}`)
  assert.ok(response.ok(), `HTTP ${response.status()}: ${await response.text()}`)
  return response.json()
}

/** 等待一次真实列表请求完成，再检查页面中展示的图片数量。 */
async function actionAndList(action) {
  const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/images") && response.request().method() === "GET")
  await action()
  const response = await responsePromise
  assert.ok(response.ok(), `HTTP ${response.status()}: ${await response.text()}`)
  const payload = await response.json()
  const requestUrl = new URL(response.url())
  assert.equal(requestUrl.searchParams.get("search"), configuration.prefix)
  assert.equal(requestUrl.searchParams.get("page_size"), await page.getByLabel("每页图片数量").inputValue())
  assert.equal(requestUrl.searchParams.get("status") || "all", await page.getByLabel("图片状态类别").inputValue())
  assert.equal(requestUrl.searchParams.get("sort"), await page.getByLabel("图片排序方式").inputValue())
  await expect(page.locator(".library-image-tile")).toHaveCount(payload.items.length)
  return payload
}

/** 根据预设数据的三项独立状态计算各类别，核验服务端筛选结果。 */
function matches(image, status) {
  if (status === "all") return true
  if (status === "context_unready") return !image.contextReady
  if (status === "text_unready") return !image.textReady
  if (status === "visual_unready") return !image.visualReady
  const ready = image.contextReady && image.textReady && image.visualReady
  return status === "ready" ? ready : !ready
}

/** 手机展开管理菜单，桌面使用已经展示的操作按钮。 */
async function openManagement() {
  const trigger = page.getByRole("button", { name: "管理", exact: true })
  if (await trigger.isVisible() && await trigger.getAttribute("aria-expanded") !== "true") await trigger.click()
}

try {
  if (configuration.email) {
    assert.match(configuration.email, /^test-library-filters-[a-f0-9]+@example\.test$/)
    await page.goto("/login")
    await page.getByLabel("邮箱", { exact: true }).fill(configuration.email)
    await page.getByLabel("密码", { exact: true }).fill(configuration.password)
    const response = page.waitForResponse(response => new URL(response.url()).pathname === "/auth/sessions")
    await page.getByRole("button", { name: "登录", exact: true }).click()
    assert.equal((await response).status(), 200)
  }
  await page.goto(configuration.libraryPath || "/library")
  await expect(page.getByRole("heading", { name: "图片库", exact: true })).toBeVisible()
  await expect(page.getByLabel("每页图片数量")).toHaveValue("50")
  assert.deepEqual(await page.getByLabel("每页图片数量").locator("option").evaluateAll(options => options.map(option => option.value)), ["20", "50", "100", "200"])
  const prefixInput = page.getByLabel("筛选文件名", { exact: true })
  await prefixInput.fill(configuration.prefix)
  await actionAndList(() => prefixInput.press("Enter"))

  for (const status of ["all", "ready", "unready", "context_unready", "text_unready", "visual_unready"]) {
    const expected = configuration.images.filter(image => matches(image, status))
    for (const sort of ["name_asc", "updated_desc"]) {
      const first = await list({ search: configuration.prefix, status, page: "1", page_size: "20", sort })
      const second = await list({ search: configuration.prefix, status, page: "2", page_size: "20", sort })
      assert.equal(first.total, expected.length)
      assert.equal(second.total, expected.length)
      assert.equal(first.items.length, Math.min(20, expected.length))
      assert.equal(second.items.length, Math.min(20, Math.max(0, expected.length - 20)))
      const expectedIds = new Set(expected.map(image => image.meme_id))
      for (const image of [...first.items, ...second.items]) {
        assert.ok(expectedIds.has(image.meme_id))
        assert.equal(matches({ contextReady: image.metadata.status === "ready", textReady: image.embedding_status === "ready", visualReady: image.visual_embedding_status === "ready" }, status), true)
      }
      assert.equal(new Set([...first.items, ...second.items].map(image => image.meme_id)).size, first.items.length + second.items.length)
      if (sort === "name_asc") {
        const names = [...first.items, ...second.items].map(image => image.display_name.toLowerCase())
        assert.deepEqual(names, [...names].sort())
      }
      const full = await list({ search: configuration.prefix, status, page: "1", page_size: "200", sort })
      assert.deepEqual([...first.items, ...second.items].map(image => image.meme_id), full.items.slice(0, 40).map(image => image.meme_id))
      if (sort === "updated_desc") assert.equal(first.items[0].meme_id, expected.at(-1).meme_id)
    }
  }
  for (const pageSize of ["20", "50", "100", "200"]) {
    const payload = await list({ search: configuration.prefix, page_size: pageSize, page: "1", status: "all" })
    assert.equal(payload.items.length, Number(pageSize))
    assert.equal(payload.total, 201)
  }
  const tooLarge = await context.request.get("/images?page_size=201")
  assert.equal(tooLarge.status(), 400)
  assert.equal((await tooLarge.json()).error, "invalid_request")
  const invalidStatus = await context.request.get("/images?status=invalid")
  assert.equal(invalidStatus.status(), 400)
  assert.equal((await invalidStatus.json()).error, "invalid_request")
  checks.push("真实 API 六类状态、搜索、两种排序、筛选后计数与分页、四档数量及参数上限")

  await actionAndList(() => page.getByLabel("每页图片数量").selectOption("20"))
  await actionAndList(() => page.getByRole("button", { name: "下一页", exact: true }).click())
  await expect(page.locator(".library-pagination")).toContainText("第 2 /")
  await page.getByRole("button", { name: "选择图片", exact: true }).click()
  await page.locator(".library-image-tile").first().getByRole("checkbox").check()
  await actionAndList(() => page.getByLabel("每页图片数量").selectOption("100"))
  await expect(page.locator(".library-pagination")).toContainText("第 1 /")
  assert.equal(await page.locator(".library-image-tile input:checked").count(), 0)
  await page.reload()
  await expect(page.getByLabel("每页图片数量")).toHaveValue("100")
  await prefixInput.fill(configuration.prefix)
  await actionAndList(() => prefixInput.press("Enter"))
  checks.push("修改每页数量返回第一页、清空选择、刷新保留数量")

  await actionAndList(() => page.getByLabel("图片排序方式").selectOption("name_asc"))
  const first = await actionAndList(() => page.getByLabel("每页图片数量").selectOption("20"))
  await page.getByRole("button", { name: "选择图片", exact: true }).click()
  const readyIndex = first.items.findIndex(image => image.metadata.status === "ready" && image.embedding_status === "ready" && image.visual_embedding_status === "ready")
  assert.ok(readyIndex >= 0)
  await page.locator(".library-image-tile").nth(readyIndex).getByRole("checkbox").check()
  const beforeSelection = mutationRequests.length
  await openManagement()
  await page.getByRole("button", { name: "选中当前页所有未就绪", exact: true }).click()
  for (const [index, image] of first.items.entries()) {
    const checkbox = page.locator(".library-image-tile").nth(index).getByRole("checkbox")
    const shouldSelect = index === readyIndex || image.metadata.status !== "ready" || image.embedding_status !== "ready" || image.visual_embedding_status !== "ready"
    if (shouldSelect) await expect(checkbox).toBeChecked()
    else await expect(checkbox).not.toBeChecked()
  }
  assert.equal(mutationRequests.length, beforeSelection)
  assert.equal(await page.getByRole("dialog").count(), 0)
  await page.locator(".library-image-tile").first().getByRole("checkbox").uncheck()
  await page.getByRole("button", { name: "列表", exact: true }).click()
  const selectedInList = await page.locator(".library-row input:checked").count()
  assert.ok(selectedInList > 0)
  await page.getByRole("button", { name: "方块", exact: true }).click()
  assert.equal(await page.locator(".library-image-tile input:checked").count(), selectedInList)
  checks.push("页内选择使用三项状态、保留手动选择、允许取消、两种展示保留选择、选择操作没有写请求")

  await actionAndList(() => page.getByRole("button", { name: "下一页", exact: true }).click())
  assert.equal(await page.locator(".library-image-tile input:checked").count(), 0)
  await page.locator(".library-image-tile").first().getByRole("checkbox").check()
  await actionAndList(() => page.getByLabel("图片状态类别").selectOption("ready"))
  await expect(page.locator(".library-pagination")).toContainText("第 1 /")
  assert.equal(await page.locator(".library-image-tile input:checked").count(), 0)
  await openManagement()
  await expect(page.getByRole("button", { name: "选中当前页所有未就绪", exact: true })).toBeDisabled()
  await page.keyboard.press("Escape")
  await actionAndList(() => page.getByLabel("图片状态类别").selectOption("unready"))
  await expect(page.locator(".library-image-tile")).toHaveCount(20)
  checks.push("翻页与状态变化清空选择、状态变化返回第一页、全部就绪时禁用批量选择")

  await context.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByLabel("图片状态类别")).toBeVisible()
  await expect(page.getByLabel("每页图片数量")).toBeVisible()
  await openManagement()
  await page.getByRole("button", { name: "选中当前页所有未就绪", exact: true }).click()
  await expect(page.getByRole("button", { name: "结束选择", exact: true })).toHaveAttribute("aria-pressed", "true")
  assert.equal(await page.locator(".library-image-tile input:checked").count(), 20)
  assert.deepEqual(pageErrors, [])
  checks.push("手机尺寸下状态与数量控件可用，批量选择进入选择模式，无浏览器运行错误")
  await writeFile(configuration.reportPath, JSON.stringify({ checks, pageErrors, mutationRequests }, null, 2))
  console.log(JSON.stringify({ checks, pageErrors, mutationRequests }, null, 2))
} finally {
  await browser.close()
}
