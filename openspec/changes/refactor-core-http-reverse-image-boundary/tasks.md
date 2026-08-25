## 1. 基线与 OpenSpec

- [x] 1.1 记录 reverse-image callback route、multipart 字段、绑定/目标校验顺序、错误族和 dirty 状态。
- [x] 1.2 读取 ReverseImageService、callback repository、scope 装配及现有安全测试，冻结兼容边界。

## 2. Reverse image callback HTTP 模块

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/reverse_image_http.py`，模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 binding、registration、database、scope service 和 error callback 保持安全事实顺序。
- [x] 2.3 在 `api.py` 删除重复编排，保留 canonical route、表单声明和旧 handler 兼容 wrapper。

## 3. 契约测试与验证

- [x] 3.1 增加 route/dependency snapshot、绑定拒绝、目标 SHA、request id/digest、controlled crop 和 service 转发测试。
- [x] 3.2 运行 reverse-image/callback/API/scope/security 测试、compileall 和 diff check，按失败修复；定向回归 `62 passed`，完整编译通过。
- [x] 3.3 更新 tasks 与验证记录，固定实现 SHA、范围和门禁事实；验证记录固定实现与独立文档提交。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate 与完整相关回归；完整回归 `423 passed, 92 skipped`，PostgreSQL marker 未选择测试且未连接数据库。
- [x] 4.2 进行对抗性复核：检查 body 不可覆盖 scope/path/target、旧 claim fail-closed、错误脱敏和无重复 route；未发现 P1/P2。
- [x] 4.3 在开源仓库提交精确实现 SHA `082e454884f2a736d9f8f4988b649b41298010de` 与验证记录 SHA，供 Server 精确 fetch/普通 merge。
