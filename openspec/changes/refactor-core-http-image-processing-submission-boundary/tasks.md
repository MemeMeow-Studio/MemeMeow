## 1. 基线与 OpenSpec

- [x] 1.1 记录 `/images/processing` route 的 path/method/status/tag/order、query、payload、response、错误和 dirty 状态。
- [x] 1.2 读取 Worker/Repository、metadata service、processing options、scope 装配及现有图片处理测试，冻结复用/逐项失败边界。

## 2. Image processing submission HTTP 模块

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/image_processing_submission_http.py`，模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 Worker、options、Repository、metadata、environment、config 和 error callback 保持提交顺序。
- [x] 2.3 在 `api.py` 删除重复批量提交实现，保留 canonical route、query/model 声明和旧 handler 兼容 wrapper。

## 3. 契约测试与验证

- [x] 3.1 增加 route/dependency snapshot、readiness/options、service 顺序、Job reuse/retry、partial failure 和 response projection 测试。
- [x] 3.2 运行图片处理/API/scope/security 测试、compileall 和 diff check，按失败修复；图片处理/阶段/context/API 定向回归 `26 passed, 36 skipped`，编译和 diff check 通过。
- [x] 3.3 更新 tasks 与验证记录，固定实现 SHA、范围和门禁事实；实现 SHA 已固定，验证记录待独立提交。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate 与完整相关回归；完整回归 `439 passed, 92 skipped`，PostgreSQL marker `55 deselected` 且未连接数据库。
- [x] 4.2 进行对抗性复核：检查客户端字段边界、scope 目标派生、Worker readiness、partial failure 和无重复 route；未发现 P1/P2。
- [ ] 4.3 在开源仓库提交精确实现 SHA 与验证记录 SHA，供 Server 精确 fetch/普通 merge。
