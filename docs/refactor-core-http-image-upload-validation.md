# 图片上传 HTTP 边界验证记录

本记录对应 `refactor-core-http-image-upload-boundary`，用于固定开源实现先行、验证范围和
后续 Server 精确同步所需的证据。

## 范围

- 新增 `backend/image_upload_http.py`，迁移有界 multipart parser、逐文件读取、幂等结果和
  `/images/upload` 编排。
- `api.py` 保留 canonical route、旧 handler/helper 名称和合集导入复用的 parser 别名。
- 未修改数据库 schema、前端、合集导入业务、图片处理实现或公开 URL/method/status/response
  contract。

## 验证

- 上传边界/API/operation/公共边界定向：`32 passed, 36 skipped`。
- 开源全套 pytest：`465 passed, 92 skipped`。
- `uv run python -m compileall -q .`：通过。
- `git diff --check`：通过。
- `openspec validate refactor-core-http-image-upload-boundary --strict --json`：通过。
- skipped 主要来自 PostgreSQL marker；本次环境未配置可用 PostgreSQL，未伪造数据库验证结果。

## 提交与同步记录

- 实现 SHA：`fa8f97d554b02efcaa07b982d34dc993121c9012`（`refactor(core): isolate image upload HTTP boundary`）。
- 验证/收尾 SHA：本验证记录与 tasks 收尾提交后填写。
- Server merge SHA：待用户已批准的本地精确 fetch/普通 `--no-ff` merge 后填写。
- 回滚点：恢复 `api.py` 原上传编排并删除 `backend/image_upload_http.py`、契约测试和本 change
  artifacts；不执行 schema 或数据回滚。
