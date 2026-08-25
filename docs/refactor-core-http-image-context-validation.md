# Image Context HTTP 重构验证记录

本记录对应 OpenSpec change `refactor-core-http-image-context-boundary`，用于固定开源实现
和验证事实，供 Server 按精确 SHA 同步。

## 来源

- 实现 commit：`f7120d219f0b227b43c805a5505bbe6697561279`
- 实现父提交：`22495ceaa1502346f5263d05b50933907cd07be0`
- 变更范围：新增 `backend/image_context_http.py`、`tests/test_image_context_http.py`、
  本 change artifacts；`api.py` 仅保留五个图片语境/视觉/repair wrapper、模型 re-export
  和显式 callback 注入。未修改图片处理 Worker、任务 service、metadata repository、
  数据库 schema、Server adapter 或 frontend。

## 验证

- 新增模块测试：`9 passed`
- API/图片处理/scope 定向回归：`31 passed, 36 skipped`
- 完整非外部门禁：`399 passed, 92 skipped`
- PostgreSQL 命令：`uv run pytest tests/test_postgres_integration.py -m postgres -q`，当前
  开源仓库无 `postgres` marker 选择，结果为 `39 deselected`；未设置连接串，未连接默认数据库。
- `openspec validate refactor-core-http-image-context-boundary --strict`：通过
- `uv run python -m compileall -q .`：通过
- `git diff --check`：通过

## 对抗性复核

- 五个 route 的 method/status/tag/order 与旧入口快照一致；`/images/context/batch` 保留旧
  `200` status，其余四个入口保留 `202`。
- `ContextRequest`/`ContextBatchRequest` 拒绝 path、scope 和未知字段；目标始终从当前
  scope service 派生，单图 sidecar 读取失败时才使用当前 scope Meme fallback。
- 批量语境/视觉结果逐项隔离，旧 response key 集合、ready skip 和稳定 enqueue error 保持；
  metadata repair 只提交固定的 `metadata_repair` 空 payload。
- 图片语境提交的 operation policy 错误通过宿主 callback 保留 `retry_at`/`Retry-After`；修复
  commit 为 `8b7ce10878e6069950de63116ba331ebf0282a1d`，父提交为验证记录
  `b305bfc53c6569836d8427905238cdc32348fe7b`。
- 新模块静态依赖不包含 `api` 或 `server_api`；未发现 P1/P2。

## 同步门禁

用户已批准从本地开源仓库精确 fetch 并普通 merge；Server 尚未同步本 change，Server merge
SHA 待同步后记录。开源仓库未 push。
