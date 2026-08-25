# Collection HTTP 重构验证记录

本记录对应 OpenSpec change `refactor-core-http-collection-boundary`，用于固定公共实现和
验证事实，供 Server 按用户审核通过的精确 SHA 同步。

## 来源

- 实现 commit：`2de9139f574552bf6f941374241af26032e689f2`
- 变更范围：新增 `backend/collection_http.py`、`tests/test_collection_http.py`、本 change
  artifacts；`api.py` 删除合集列表、创建、详情、重命名、删除和成员增删的重复编排，只保留
  canonical route、请求模型、分页 query、旧 handler 和兼容 helper。未修改合集 repository、
  ORM/schema、ZIP 导入、Server 导出边界、scope middleware 或 frontend。

## 验证

- 新增合集 HTTP 模块测试：`10 passed`
- 合集/API/scope/public-result/upload 定向回归：`48 passed, 36 skipped`
- 完整非外部门禁：`449 passed, 92 skipped`
- PostgreSQL marker 命令：`39 deselected`，未选择 PostgreSQL 测试且未连接数据库。
- `openspec validate refactor-core-http-collection-boundary --strict`：通过
- `uv run --project "$PWD" --active python -m compileall -q api.py backend tests/test_collection_http.py`：通过
- `git diff --check`：通过

## 对抗性复核

- 七个 CRUD/详情/成员 canonical route 保持单次注册、原 method/status/tags/query；`/collections/import`
  和 `/collections/{collection_id}/export` 未由新模块重复注册，导入/导出实现保持入口边界。
- 新模块 AST 依赖不包含 `api` 或 `server_api`；environment、metadata service 和 error factory
  均由入口显式注入，旧 handler/helper 名称仍可导入。
- 列表/详情在 repository 前拒绝未知 query；合集摘要和成员详情只投影稳定 ID、当前文件名、
  受控媒体 URL、成员计数及 metadata 状态，不暴露 scope 或物理路径。
- repository 的跨 scope/无效 Meme、名称冲突、批量成员原子性和幂等语义继续通过稳定错误映射
  和当前 scope environment 处理；未发现 P1/P2。

## 同步门禁

该实现已在开源仓库本地提交，但尚未进入 Server。按项目流程，必须先由用户审核并明确授权
精确 SHA 同步；在授权前不得从开源历史 fetch、更新 `main-open` 或 merge 到 Server。开源仓库
未 push。
