## 1. 基线与 OpenSpec

- [x] 1.1 记录视觉 callback route、VisualMatchRequest 字段、binding/claim/fact 顺序、错误族和现有 dirty 状态。
- [x] 1.2 读取 callback binding/fact repository、VisualSearchService、scope 装配和现有安全测试，冻结幂等与失败收束边界。

## 2. Visual callback HTTP 模块实现

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/visual_callback_http.py`，迁移 request model 和 handler；模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 binding、registration、database、scope service 和 error callback 复用现有安全事实顺序。
- [x] 2.3 在 `api.py` 删除重复实现，保留 canonical route、handler、model 和 legacy import 兼容。

## 3. 契约测试与验证

- [x] 3.1 增加 route/dependency snapshot、binding fail-closed、request id/header digest、completed replay、fact failure/success finish 和 legacy import 测试。
- [x] 3.2 运行 callback/visual/database/scope/security 测试与 compileall，按失败修复；定向回归 `70 passed, 36 skipped`，完整编译通过。
- [x] 3.3 更新本 change tasks 与验证记录，确认公共核心先行和 Server 尚未同步；验证记录固定实现 SHA、范围和门禁事实。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate、`git diff --check`、完整 pytest 和 PostgreSQL marker（无连接串时显式 skip）；完整 pytest `413 passed, 92 skipped`，直接 PostgreSQL 测试 `55 skipped`，当前 marker 命令 `55 deselected`。
- [x] 4.2 进行对抗性复核：检查 callback body 无 scope override、旧 claim、fact 状态、幂等 replay、错误脱敏和 service 不提前调用；补充 binding/header fail-closed 检查后重跑验证，无未处理 P1/P2。
- [x] 4.3 在开源仓库提交精确实现 SHA 和验证记录 SHA，核验祖先关系；实现 commit 为 `7c1b68d2e1dde6d129e89e6f528656c4baf8c1ff`，验证记录已独立提交；Server 待精确 fetch/普通 merge 后补记 merge SHA。
