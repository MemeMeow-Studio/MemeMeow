## 1. 基线与 OpenSpec

- [x] 1.1 固定 `/images/rename`、`/images/delete` route 的 path、method、status、tags、顺序、请求/响应和错误事实，并确认上传与只读图片边界不被本切片覆盖。
- [x] 1.2 读取 metadata service、BlobStore、operation policy、scope 装配和现有图片/API 测试，冻结文件名、扩展名、目标冲突与 grant 收束边界。

## 2. Image mutation HTTP 模块

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/image_mutation_http.py`，模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 metadata、filename、storage-key、operation、search 和 error callback 实现重命名与删除编排，保持 fail-closed 和副作用顺序。
- [x] 2.3 在 `api.py` 删除两个 route 的重复业务实现，保留请求模型、canonical route decorator、旧 handler 名称和兼容 callback wrapper；上传实现保持原位置。

## 3. 契约测试与验证

- [x] 3.1 增加 route/dependency snapshot、meme_id/scope 拒绝、文件名/扩展名/冲突、metadata 错误、operation 顺序和响应投影测试。
- [x] 3.2 运行图片变更/API/scope/security 定向测试、compileall 和 diff check，按失败修复并记录未运行的外部门禁；图片变更/API 定向 `13 passed, 36 skipped`，全套回归 `461 passed, 92 skipped`，PostgreSQL marker `553 deselected`。
- [x] 3.3 更新 tasks 与验证记录，固定实现 SHA、范围和测试事实；验证记录已建立，待提交 SHA 回填。

## 4. 最终验证与同步

- [x] 4.1 运行本 change OpenSpec strict validate 和开源完整回归，确认上传与只读 route 未重复或改变；strict validate 通过，完整回归 `461 passed, 92 skipped`。
- [x] 4.2 进行对抗性复核：检查 scope/路径选择器、文件边界、grant 拒绝/释放/commit 失败、缓存失效时机和无重复 route；未发现 P1/P2。
- [ ] 4.3 在开源仓库提交精确实现 SHA 与验证记录 SHA，停在用户审核门禁，未经批准不 fetch/merge 到 Server。
