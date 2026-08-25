## 1. 基线与 OpenSpec

- [x] 1.1 固定 `/collections/import` route 的 path、method、status、tag、顺序、multipart 字段、响应 keys 和旧 helper/handler 事实，并确认合集 CRUD 与 Server export route 不被本切片覆盖。
- [x] 1.2 读取 `collection_packages`、multipart parser、metadata/BlobStore、scope environment、operation policy、processing worker 和现有导入测试，冻结资源、成员 SHA/path、权限和副作用顺序。

## 2. Collection import HTTP 模块

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/collection_import_http.py`，模块不得导入 `api.py` 或 `server_api`，且不注册 route。
- [x] 2.2 通过显式 callback 实现 multipart/ZIP 预检、scope-bound 复用/写入、成员关系、operation policy、任务投递和稳定错误映射，保持 fail-closed 与副作用顺序。
- [x] 2.3 在 `api.py` 删除导入 route 的重复业务实现，保留 canonical decorator、旧 `import_collection` 名称和 `_collection_package_error` 兼容 wrapper；CRUD/Server export 不得重复注册。

## 3. 契约测试与验证

- [x] 3.1 增加 route/dependency snapshot、multipart/ZIP/资源上限、manifest SHA/path、scope/query 拒绝、同名复用/冲突、operation 顺序、任务告警和逐项响应投影测试。
- [x] 3.2 运行合集导入/API/scope/security 定向测试、compileall 和 diff check，按失败修复并记录 PostgreSQL/外部门禁 skip；合集导入/合集包/CRUD/API 定向 `40 passed`，上传/operation 定向 `26 passed`，完整回归待最终门禁。
- [x] 3.3 更新 tasks 与验证记录，固定实现 SHA、范围和测试事实；实现 commits `13605361752792926bf2672a7dbda3283bcdf232`、`8b39a0b` 已固定，验证记录更新提交待本次提交 SHA 回填。

## 4. 最终验证与同步

- [x] 4.1 运行本 change OpenSpec strict validate 与开源完整回归，确认导入/导出/CRUD route 未重复、Server 适配边界未被改动；strict validate 通过，完整回归 `475 passed, 92 skipped`。
- [x] 4.2 进行对抗性复核：检查 scope/user 字段、跨 scope 复用、压缩/解压/图片资源上限、成员 SHA/path、部分写入、operation grant 收束、路径泄露和任务副作用顺序；未发现 P1/P2。
- [x] 4.3 在开源仓库提交精确实现 commits `13605361752792926bf2672a7dbda3283bcdf232`、`8b39a0b` 与验证记录，收尾 SHA 待更新；Server 后续从本地精确 fetch 后普通 `--no-ff` merge，记录祖先关系、验证、回滚和未连接 PostgreSQL 事实。
