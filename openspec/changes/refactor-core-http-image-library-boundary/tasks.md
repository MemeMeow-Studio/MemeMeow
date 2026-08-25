## 1. 基线与 OpenSpec

- [x] 1.1 记录图片列表、metadata、media route 的 path/method/tag/order、query、响应字段、错误和 dirty 状态。
- [x] 1.2 读取 PostgresMetadataService、ScopeServices、BlobStore、processing repository 和现有图片测试，冻结指纹/状态边界。

## 2. Image library HTTP 模块

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/image_library_http.py`，模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 services、environment、processing repository、visual identity 和 error callback 保持只读编排顺序。
- [x] 2.3 在 `api.py` 删除重复列表/metadata/media 实现，保留 canonical route、query 声明和旧 handler 兼容 wrapper。

## 3. 契约测试与验证

- [x] 3.1 增加 route/dependency snapshot、query selector 拒绝、列表状态投影、metadata/meme_id、media 类型和错误映射测试。
- [x] 3.2 运行图片库/API/scope/security 测试、compileall 和 diff check，按失败修复；图片库/API/数据库契约 `16 passed, 36 skipped`，编译和 diff check 通过。
- [x] 3.3 更新 tasks 与验证记录，固定实现 SHA、范围和门禁事实；实现 SHA 已固定，验证记录待独立提交。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate 与完整相关回归；完整回归 `431 passed, 92 skipped`，PostgreSQL marker `55 deselected` 且未连接数据库。
- [x] 4.2 进行对抗性复核：检查路径/scope 选择器、指纹 fail-closed、状态脱敏和无重复 route；未发现 P1/P2。
- [ ] 4.3 在开源仓库提交精确实现 SHA 与验证记录 SHA，供 Server 精确 fetch/普通 merge。
