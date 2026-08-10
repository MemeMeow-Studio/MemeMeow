/** 构建产物的浏览器级工作流冒烟测试。 */
import { expect, test } from '@playwright/test'

test('首页可加载并切换核心工作区', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveTitle('MemeMeow')
  await expect(page.getByRole('heading', { name: '找到合适的表达' })).toBeVisible()
  await page.getByRole('button', { name: '图片库' }).click()
  await expect(page.getByRole('heading', { name: '图片库' })).toBeVisible()
  await page.getByRole('button', { name: '上传' }).click()
  await expect(page.getByRole('heading', { name: '上传图片' })).toBeVisible()
})

test('搜索表单拒绝空查询并保留工作区', async ({ page }) => {
  await page.goto('/')
  const submit = page.getByRole('button', { name: '开始检索' })
  await expect(submit).toBeDisabled()
  await page.getByPlaceholder('例如：开会时发现自己忘记准备材料').fill('测试查询')
  await expect(submit).toBeEnabled()
})
