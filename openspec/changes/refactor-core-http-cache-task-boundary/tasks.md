## 1. 基线与 OpenSpec

- [x] 1.1 记录开源 `/generate-cache` route、响应、service 顺序和测试脏状态，确认 Server
  工作区未被实现阶段触碰。
- [x] 1.2 保存 route path/method/status/tag/order、GET 405、service 缺失和 task response
  基线，读取 proposal/design/spec 上下文。

## 2. Cache task HTTP 模块实现

- [x] 2.1 新增带中文 docstring 的 `backend/cache_task_http.py`，迁移缓存任务提交编排；
  模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 service/error callback 复用 scope-bound readiness、task submit 和错误
  投影语义。
- [x] 2.3 在 `api.py` 删除重复实现，保留 `generate_cache` wrapper 与原 decorator 位置。

## 3. 契约测试与验证

- [x] 3.1 增加 route snapshot、module dependency、service order、error、response projection
  和 legacy import 测试。
- [x] 3.2 运行 cache/API/task/scope/security 相关测试与 compileall，按失败修复。
- [x] 3.3 更新公共核心重构记录，明确开源 commit 先行且 Server 不创建平行实现。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate、`git diff --check`、全套 pytest 和 PostgreSQL marker
  （无连接串时显式 skip）。
- [x] 4.2 对抗性复核 route order、service readiness 顺序、空 payload、响应脱敏和 active
  change 脏路径；修复所有 P1/P2 后重新验证。
- [ ] 4.3 在开源仓库提交精确 SHA，核验祖先与测试，再按授权的本地精确 fetch/普通 merge 同步
  Server，记录两个 SHA、变更范围和验证结果。
