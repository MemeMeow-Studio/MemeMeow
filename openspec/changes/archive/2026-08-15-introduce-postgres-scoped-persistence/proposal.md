## Why

当前 MemeMeow 将图片语境、检索向量和任务状态分别持久化为 sidecar JSON、全局搜索缓存和逐任务 JSON，图片路径同时承担资源身份。这种分散存储难以保证跨资源一致性，也无法在不污染公共核心的前提下为闭源版本提供可靠的数据范围隔离。现在需要将结构化业务数据统一迁移到 PostgreSQL，并让公共核心始终在一个显式绑定的数据范围内运行。

## What Changes

- 引入 PostgreSQL 16 和 pgvector，作为图片记录、结构化语境、检索向量和任务状态的唯一权威结构化存储。
- 为每张图片引入不可变 `meme_id`，将可变的相对路径降为文件系统或对象存储中的 `storage_key`；图片原始字节继续保存在受控文件存储中，不写入 PostgreSQL。
- 引入内部 `scope_id` 数据边界。公共核心的所有持久化访问都通过已绑定 scope 的 repository 执行；开源版本固定使用 `scope_id="local"`，不增加登录、用户表或账户管理能力。
- 将 meme 语境、来源、状态和未知扩展字段保存为 PostgreSQL 记录，在保留现有证据边界、人工字段保护和图片指纹校验行为的同时，取消运行时 sidecar JSON 读写。
- 将搜索 embedding 和索引状态迁移到 pgvector，按 scope 生成、刷新和查询；不再将完整 embedding 缓存常驻 Python 内存，也不再使用 `search-cache-v4.json`。
- 将任务记录、去重键、批次关系和执行认领状态迁移到 PostgreSQL，保留现有任务状态、恢复、背压与批次收束语义，并允许多个进程安全认领任务。
- 开源开发模式由 Docker Compose 运行 PostgreSQL 与一次性数据库准备容器，MemeMeow API 和 Vue 继续在宿主机运行；PostgreSQL 只监听宿主机 `127.0.0.1:5434`，容器内部使用标准端口 `5432`。
- 新增 `database.sh`，统一提供可重复执行的 PostgreSQL 启停、健康检查、Alembic schema 升级和本地实例初始化；现有 `start.sh` 继续只启动宿主机 API 与 Vue，并在数据库未就绪时给出明确提示。不新增职责重叠的 `install.sh`。
- 开源版初始化只幂等创建内部 `scope_id="local"` 和安装标记；它不是用户账户，也不向客户端暴露。
- 实施切换时使用临时工具尽力导入现有图片和合法 sidecar，核对完成后删除该工具；旧任务 JSON 和旧搜索向量直接丢弃，不建设正式迁移产品、兼容层或长期迁移状态表。
- **BREAKING**：部署必须提供 PostgreSQL 16 与 pgvector；切换后现有 sidecar、搜索缓存和任务 JSON 不再被应用读取。
- **BREAKING**：图片列表和内部业务操作改用稳定 `meme_id` 标识资源；相对路径仅作为展示、物理定位和受控媒体访问属性，不再作为数据库主键。

## Capabilities

### New Capabilities

- `scoped-persistence`: 定义 PostgreSQL 权威存储、绑定 `scope_id` 的数据访问边界、稳定资源身份和无旧格式兼容的运行时契约。
- `image-metadata`: 定义 PostgreSQL 中版本化 meme 语境、图片指纹、证据边界、字段来源和生命周期一致性；取代已完成但尚未归档的 `image-sidecar-metadata` change 所定义的 sidecar 存储形式。

### Modified Capabilities

- `image-ingestion`: 上传成功后必须原子地建立稳定 Meme 记录和初始语境，并返回稳定资源标识。
- `image-library`: 图片列表、重命名、删除和媒体访问必须以 scope 内的稳定 Meme 记录为权威来源。
- `image-labeling`: 标注与显式重命名流程必须使用稳定 `meme_id`，重命名后资源身份保持不变。
- `meme-search`: 检索索引改为按 scope 管理的 pgvector 数据，并保留稳定排序、去重和刷新期间旧索引可用语义。
- `configuration-and-cache`: 搜索缓存生命周期改为 PostgreSQL 中可观察、互斥且按 scope 隔离的索引生成状态。
- `task-status`: 任务状态改为 PostgreSQL 持久化、可恢复且可安全认领，不再采用不可恢复的内存任务语义。
- `public-api-compatibility`: 受控媒体 URL 改为稳定的 `/media/{meme_id}`，搜索响应仍保持 `{ "results": string[] }`。

## Impact

- 主要影响 `backend/metadata.py`、`backend/search.py`、`backend/tasks.py`、`backend/paths.py`、`api.py` 及其测试，需要新增 PostgreSQL 数据模型、schema 管理、repository、事务边界和实施期临时导入工具。
- 新增 PostgreSQL 驱动、数据库访问层、Alembic 和 pgvector 依赖；开源开发环境新增 PostgreSQL Compose 服务、一次性数据库准备容器和 `database.sh`。
- 图片文件仍位于受控图片根目录，路径穿越和符号链接防护继续保留；数据库只保存相对 `storage_key`、内容指纹和结构化数据。
- 反向图片供应商缓存仍是可丢弃的独立派生缓存，不纳入本次业务持久化迁移。
- `image-sidecar-metadata` change 的语义字段和行为约束由本 change 的最终 `image-metadata` 规格接管；旧 change 不应再按原 sidecar 设计归档到主规范。
