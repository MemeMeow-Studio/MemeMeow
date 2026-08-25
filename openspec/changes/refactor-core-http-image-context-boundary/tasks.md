## 1. 基线与 OpenSpec

- [x] 1.1 记录五个 canonical route 的 path/method/status/tag/order、模型字段、scope fallback、批量 skip/error 和 metadata repair 现状。
- [x] 1.2 读取 metadata service、图片处理 Job facade、task service、现有 API/scope/security 测试，冻结响应和稳定错误边界。

## 2. Image context HTTP 模块实现

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/image_context_http.py`，迁移 `ContextRequest`、`ContextBatchRequest` 和五个 handler；模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 service、environment、处理 Job、task service、error 和 enqueue error callback 复用 scope-bound 行为。
- [x] 2.3 在 `api.py` 删除重复实现，保留 canonical route、handler、请求模型和旧 import 兼容。

## 3. 契约测试与验证

- [x] 3.1 增加 route snapshot、module dependency、scope target、missing sidecar fallback、batch isolation、skip、error projection、repair callback 和 legacy import 测试。
- [x] 3.2 运行图片处理/API/task/scope/security 相关测试与 compileall，按失败修复。
- [x] 3.3 更新本 change tasks 与验证记录，确认公共核心先行和 Server 尚未同步；当前开源定向验证为 image-context `8 passed`、API `31 passed, 36 skipped`，完整非外部门禁为 `398 passed, 92 skipped`。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate、`git diff --check`、完整 pytest 和 PostgreSQL marker（无连接串时显式 skip）；strict validate、compileall 和 diff-check 通过，完整 pytest `398 passed, 92 skipped`，PostgreSQL 命令在当前仓库无 marker 选择时为 `39 deselected`。
- [x] 4.2 进行对抗性复核：检查 canonical route order、scope target、path/scope payload 拒绝、批量 fail-closed、repair scope、旧 response key 和错误脱敏；发现并修复批量 response 额外字段与 route status 误判，重跑全部门禁无 P1/P2。
- [x] 4.3 在开源仓库提交精确实现 SHA 和验证记录 SHA，核验祖先关系；实现 commit 为 `f7120d219f0b227b43c805a5505bbe6697561279`，验证记录随后以独立 docs commit 提交；Server 尚未同步，等待精确 fetch/普通 merge。
