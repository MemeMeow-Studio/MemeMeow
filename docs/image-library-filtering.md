# 图片库筛选与页内选择

每页数量提供 20、50、100、200 四个选项，默认使用 50。浏览器记住用户选择，修改数量后返回第一页并清空选中图片。

状态类别包括全部状态、全部就绪、未就绪、语境未就绪、文本向量未就绪和视觉向量未就绪。全部就绪要求三项状态均为 `ready`，未就绪表示至少一项尚未就绪；单项类别允许重叠。文本与视觉向量的就绪条件包含当前模型和图片版本，历史向量不会被显示为当前有效向量。

`GET /images` 使用 `status` 指定状态类别，可选值为 `all`、`ready`、`unready`、`context_unready`、`text_unready`、`visual_unready`。状态条件与名称搜索共同参与数据库查询，查询结果经过排序后分页，`total` 表示筛选后的数量。`page_size` 上限为 200。

“选中当前页所有未就绪”会进入选择模式，勾选当前页未就绪图片，保留当前页手动选择。该操作没有处理请求。用户通过“重试选中”选择完整处理或指定阶段。翻页、修改状态条件或每页数量时清空已有选择；当前页全部就绪时禁用批量选择按钮。

数据库验收使用 `tests/test_image_library_status_postgres.py`，需要显式提供开发数据库的 `MEMEMEOW_TEST_DATABASE_URL`。浏览器验收使用 `node scripts/verify_library_filters.mjs <配置文件>`，配置包含开发入口 `baseURL`、测试数据 `prefix`、201 项图片事实 `images`、报告路径 `reportPath`，账户入口还需要测试邮箱和密码。图片事实包含 `meme_id`、`contextReady`、`textReady`、`visualReady`。两个验收入口均使用真实数据库或真实服务。
