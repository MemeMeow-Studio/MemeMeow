# 图片库指定页码跳转

图片库分页区域提供“目标页码”输入框。输入范围内的整数页码，点击“跳转”或按 Enter，即可读取该页图片。跳转会沿用当前搜索、排序、状态筛选和每页数量，并清空当前图片选择。

加载期间禁用输入与跳转按钮。空白、非整数、超出页码范围及当前页码均无法提交。上一页、下一页和筛选操作完成后，输入框显示当前页码。方块和列表展示共用该控件，手机页面支持相同操作。

## 验证

在 `frontend` 执行 `npm run test -- src/components/library/LibraryPageJump.test.ts`、`npm run typecheck` 和 `npm run build`。

真实浏览器验证前创建工作区内的 `.local-real-test` 目录，执行 `TMPDIR="$PWD/.local-real-test" node scripts/verify_library_page_jump.mjs <配置文件> [报告文件]`。配置字段为 `baseURL`、`prefix`，需要登录时提供测试账户的 `email`、`password`，可通过 `libraryPath` 指定图片库路径。仅使用开发服务及带 `test-library-filters-` 标识的图片，测试图片至少覆盖三页，其中未就绪图片至少覆盖两页。验证包含桌面和手机、方块和列表、按钮和 Enter、页码边界、选中项清理及筛选结果为空时的页面状态。选中项清理覆盖桌面两种展示和手机列表。
