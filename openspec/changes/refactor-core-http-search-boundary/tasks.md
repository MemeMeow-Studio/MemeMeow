## 1. 基线与 OpenSpec

- [x] 1.1 记录开源 `SearchRequest`、`/search` route、`_media_for_meme` 和测试脏状态，确认
  Server 工作区未被实现阶段触碰。
- [x] 1.2 保存 route path/method/tag/order、输入错误矩阵和 LLM fallback/媒体去重基线。

## 2. Search HTTP 模块实现

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/search_http.py`，迁移 SearchRequest
  与搜索编排；模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 service/media/error callback 复用 scope、metadata mapper 和错误投影，
  保持 cache、embedding、fallback 和结果上限语义。
- [x] 2.3 在 `api.py` 删除重复 SearchRequest/handler，实现兼容 aliases/wrapper，保留
  `_media_for_meme` 及 `/search` decorator 原位置。

## 3. 契约测试与文档

- [x] 3.1 增加 route snapshot、module dependency、alias identity、strict input、cache/error、
  fallback、media dedupe 测试。
- [x] 3.2 运行 search/API/scope/security/runtime 相关测试与 compileall，按失败修复。
- [x] 3.3 更新公共核心重构记录，明确开源 commit 先行且 Server 不创建平行实现。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate、`git diff --check`、全套 pytest 和 PostgreSQL marker
  （无连接串时显式 skip）。
- [x] 4.2 对抗性复核 scope callback、route order、error fallback、未知/重复媒体过滤和 active
  change 脏路径；修复所有 P1/P2 后重新验证。
- [x] 4.3 在开源仓库提交精确 SHA，核验祖先与测试，再按用户授权的本地精确 fetch/普通 merge
  同步 Server，并记录两个 SHA、变更范围和验证结果。实现 commit 为 `0a48d9e`（父提交
  `7edc956`）；全套测试 `369 passed, 92 skipped`、compileall、OpenSpec strict validate
  和 `git diff --check` 通过，Server 尚未同步。
