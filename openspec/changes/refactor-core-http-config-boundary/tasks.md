## 1. 基线与 OpenSpec

- [x] 1.1 记录开源 `api.py` 与目标测试的干净状态，确认 Server 工作区未被实现阶段触碰。
- [x] 1.2 保存 `/config` route path/method/tag/order、`expose_scope`、runtime 白名单和
  storage summary 的基线，读取 proposal/design/spec 上下文。

## 2. Config HTTP 模块实现

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/config_http.py`，迁移脱敏状态投影与
  storage preflight summary；模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 `request_scope`/`service` callback 复用原有 scope、cache 和 service
  fallback 语义，保持 runtime 固定字段与脱敏输出。
- [x] 2.3 在 `api.py` 删除重复实现，保留 `config_status` 薄 wrapper、storage helper/常量
  aliases，并维持原 `/config` decorator 位置和 route metadata。

## 3. 契约测试与文档

- [x] 3.1 增加 route snapshot、module dependency、alias identity、scope visibility、cache
  fallback、runtime/storage redaction 测试。
- [x] 3.2 运行 config/API/runtime/scope/security 相关测试与 compileall，按失败修复。
- [x] 3.3 更新公共核心 refactor 记录；明确该 change 必须开源 commit 先行，不创建 Server
  平行实现或提前 merge。

## 4. 最终验证与对抗性审查

- [x] 4.1 运行 OpenSpec strict validate、`git diff --check`、相关 pytest 及必要 PostgreSQL
  marker（无连接串时显式 skip）。
- [x] 4.2 复核 scope/callback、route order、脱敏字段、兼容 import、Server sync boundary
  与 active change 脏路径；修复所有 P1/P2 后重新验证。
- [ ] 4.3 在开源仓库形成精确 commit，记录 SHA、祖先和验证结果；仅在其后按用户已授权的
  本地精确 SHA 流程 fetch 并同步 Server。
