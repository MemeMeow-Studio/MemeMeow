## 1. 基线与 OpenSpec

- [x] 1.1 记录 Job/阶段 canonical 与 legacy route 的 path/method/status/tag/order、模型字段、callback 顺序和现有脏状态；确认 Server 实现阶段不被触碰。
- [x] 1.2 读取 image processing repository/worker、现有 API/activity/security 测试，冻结 scope、retry、stage、批量部分失败和 policy 错误边界。

## 2. Image stage HTTP 模块实现

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/image_stage_http.py`，迁移 Job 列表/详情/重试和独立阶段请求模型、单图/批量 handler；模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 service、repository、Worker、options/config、error、task summary 和 operation error callback 复用 scope-bound 行为。
- [x] 2.3 在 `api.py` 删除重复阶段实现，保留 canonical/legacy route decorator、请求模型、handler 和其它完整处理入口兼容。

## 3. 契约测试与验证

- [x] 3.1 增加 route/alias snapshot、module dependency、scope repository、retry revision、stage validation、batch isolation、error projection 和 legacy import 测试。
- [x] 3.2 运行图片处理/API/task/scope/security 相关测试与 compileall，按失败修复。
- [x] 3.3 更新本 change tasks 与开源验证记录，明确公共核心改动尚未同步 Server。开源验证为 `389 passed, 92 skipped`；单独运行 `tests/test_postgres_integration.py` 为 `39 skipped`（未设置 `MEMEMEOW_TEST_DATABASE_URL`，由 fixture 显式 skip），compileall 与 targeted image/task/scope/security 测试通过；Server 尚未同步。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate、`git diff --check`、完整 pytest 和 PostgreSQL marker（无连接串时显式 skip）。OpenSpec strict、compileall、`git diff --check` 通过；完整 pytest `389 passed, 92 skipped`，PostgreSQL 集成为 `39 skipped`。
- [x] 4.2 进行对抗性复核：检查 canonical/legacy route order、scope/target binding、retry 旧 Job 不复活、payload/path 脱敏、批量部分失败和 Server policy callback；修复所有 P1/P2 后重跑验证。未发现 P1/P2；重试 malformed 标识和批量兜底错误均已 fail-closed，Server callback 仍为显式注入点。
- [x] 4.3 在开源仓库提交精确实现 SHA 和验证记录 SHA，核验祖先关系；用户审核授权后才允许 Server 精确 fetch/普通 merge，并记录 Server merge SHA、变更范围和测试结果。实现 commit 为 `5bc4c26`（完整 SHA 由 Git 核验）；验证记录随后以独立 docs commit 提交。当前实现 commit 已通过完整测试和静态门禁，Server 尚未 fetch/merge，等待用户审核授权。
