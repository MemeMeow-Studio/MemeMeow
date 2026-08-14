## 1. PostgreSQL 基线与开发环境

- [x] 1.1 在项目依赖中加入 SQLAlchemy 2、psycopg 3、Alembic 和 pgvector Python 支持，并更新锁文件
- [x] 1.2 提供固定 PostgreSQL 16 + pgvector 版本的 Compose 服务、健康检查和具名数据卷，将容器 `5432` 仅映射到宿主机 `127.0.0.1:5434`
- [x] 1.3 提供一次性数据库准备容器，使用 `postgres:5432` 连接 PostgreSQL 并与宿主机 `127.0.0.1:5434` 配置明确分离
- [x] 1.4 在服务配置中加入必需的数据库连接、连接池、固定 `EMBEDDING_DIMENSIONS=1024` 和 Worker 租约参数，并保证配置状态接口不泄露数据库凭据
- [x] 1.5 初始化 Alembic 环境，建立只能前向升级的 schema revision 检查和 pgvector 扩展门禁
- [x] 1.6 实现幂等应用初始化命令，只负责校验 schema、创建 `local` scope 和安装标记，不创建账户或扫描旧数据
- [x] 1.7 新增 `database.sh`，支持 `start|stop|restart|status|logs|migrate`，让 `start` 依次启动 PostgreSQL、等待健康、执行 Alembic 和应用初始化且从不删除 volume
- [x] 1.8 更新现有 `start.sh`，在启动本地 API 与 Vue 前检查 `127.0.0.1:5434` 和数据库健康，失败时提示执行 `./database.sh start` 而不隐式启动或升级数据库
- [x] 1.9 建立 PostgreSQL 集成测试 fixture，支持每个测试隔离清理，并验证扩展、事务、锁和向量查询实际可用

## 2. 数据模型与 scope 数据环境

- [x] 2.1 定义带不可变 storage namespace 的 scopes、memes、storage_operations 和安装状态模型及数据库约束，不增加正式旧数据迁移状态表
- [x] 2.2 定义固定 `vector(1024)` 的 search_generations、search_heads 和 meme_embeddings 模型，明确所有 scope/generation/Meme 复合外键
- [x] 2.3 定义字符串 task ID、claim generation、tasks、task_batches、task_batch_items 和 task_lane_slots 模型，加入活动去重、槽位和租约所需索引
- [x] 2.4 编写首个 Alembic migration，创建全部表、枚举或检查约束、复合唯一键、复合外键和 pgvector 索引
- [x] 2.5 实现不可为空的 ScopeContext、同步 Unit of Work 和请求/任务级 DataEnvironment，开源适配层固定绑定 `local`
- [x] 2.6 实现按构造绑定 scope 的 Meme、Search 和 Task repository，禁止公开方法接受客户端提供的 scope 覆盖
- [x] 2.7 增加直接绕过 repository 的数据库负向测试，验证 embedding、head、batch 和 task item 的 scope 不一致写入被复合外键拒绝
- [x] 2.8 在 lifespan 中创建一次 Engine、连接池和共享重资源，并确保每次请求或任务结束后 Session 正确提交、回滚和关闭

## 3. Meme 元数据与文件一致性

- [x] 3.1 将现有 sidecar schema 映射为数据库 Meme 模型和领域对象，完整保留 meme_context、provenance、状态和未知扩展字段
- [x] 3.2 将 MetadataService 改为通过 scope-bound repository 读取和更新元数据，移除运行时 sidecar 读写和回退逻辑
- [x] 3.3 实现 revision 乐观锁、图片 SHA/大小复核和 `target_changed` 防护，避免过期任务覆盖较新内容
- [x] 3.4 保留视觉、研究和人工字段来源规则，验证自动流程不会覆盖人工确认字段，失败不会清空最近有效语境
- [x] 3.5 实现 storage_operations 状态机、文件暂存区和隔离区，以及启动时未完成操作恢复器
- [x] 3.6 实现 scope-bound BlobStore，`local` 映射现有图片根目录，其他 scope 使用不可变命名空间独立根目录并拒绝客户端前缀覆盖
- [x] 3.7 将上传流程改为先验证和暂存文件，再创建稳定 UUID Meme 和 pending 元数据，并补偿数据库或文件任一侧失败
- [x] 3.8 将重命名流程改为按 `meme_id` 操作，保持 Meme 身份不变并通过操作日志协调文件移动和 storage_key 更新
- [x] 3.9 将删除流程改为按 `meme_id` 隔离文件、提交数据库删除再清理文件，并支持中断恢复
- [x] 3.10 实现数据库记录、图片文件和指纹的完整性扫描，报告孤立文件、缺失文件、路径冲突和待修复记录
- [x] 3.11 明确 storage operation 合法状态转移、恢复矩阵、幂等 token、并发恢复锁和 no-follow 文件校验
- [x] 3.12 增加跨存储故障注入测试，覆盖上传、重命名、删除的每个提交点、双恢复器竞争、symlink/TOCTOU 和双 scope 同逻辑路径

## 4. pgvector 检索索引

- [x] 4.1 将 semantic_document 构造提取为稳定领域逻辑，保持字段白名单、去重、长度限制和空语义跳过行为
- [x] 4.2 实现按 scope 与 1024 维模型创建 building generation，在短事务中固化 Meme revision/hash 源集合后分批生成向量
- [x] 4.3 实现 generation 数量、固定维度、文档 hash、元数据 hash、图片 SHA 和源快照校验，激活前重算源集合并以 `source_changed` 拒绝过期 generation
- [x] 4.4 实现 search_heads 原子激活新 generation、失败 generation 隔离和 retired generation 清理策略
- [x] 4.5 将搜索查询改为 pgvector 相似度查询，强制限定 scope 和 active generation，并按分数及 `meme_id` 稳定排序
- [x] 4.6 在返回搜索结果前关联当前 Meme 记录并复核文件可访问性，排除已删除、待修复或失效候选
- [x] 4.7 移除 `search-cache-v4.json` 的运行时加载、完整 Python embedding 缓存和旧缓存失效路径
- [x] 4.8 增加索引刷新集成测试，覆盖首次未就绪、刷新期间旧 generation 可用、失败不激活、并发刷新互斥和跨 scope 隔离
- [x] 4.9 增加检索契约测试，覆盖稳定排序、去重、模型/维度不匹配、无可索引 Meme 和语义字段变化后的新 generation

## 5. PostgreSQL 持久任务队列

- [x] 5.1 将 TaskRecord、payload、结果、错误、进度和设置版本映射到数据库模型，保持列表不暴露 payload 的 API 边界
- [x] 5.2 实现任务提交事务和活动 dedupe_key 条件唯一约束，保留普通任务与图片语境任务的现有规范化去重语义
- [x] 5.3 实现基于 `FOR UPDATE SKIP LOCKED` 的任务认领、递增 claim generation、Worker 租约、心跳、available_at 重试和最大尝试失败状态
- [x] 5.4 实现 task_lane_slots 与数据库级排队计数，使 Agent 并发上限和背压在多个应用进程间共同生效
- [x] 5.5 将现有任务 handler 接入 scope-bound DataEnvironment，所有进度、终态和业务副作用以 claim generation fencing，任务 payload 只保存可序列化的 scope、Meme 和指纹标识
- [x] 5.6 在同一事务中锁定批次、插入唯一索引任务并提交 finalizer 状态，确保任务复用、重启和多 Worker 竞争下恰好触发一次刷新
- [x] 5.7 实现 Worker 启停和优雅关闭，确保停止认领、等待或释放当前租约，不再从 JSON 目录恢复任务
- [x] 5.8 增加多连接并发和崩溃窗口测试，覆盖单次认领、去重竞态、旧 Worker fencing、全局槽位、背压、finalizer 原子提交和跨 scope 任务不可见

## 6. API 与前端稳定标识切换

- [x] 6.1 将图片列表、元数据、媒体、重命名、删除、上传结果和语境任务 API 改为返回或接受稳定 `meme_id`
- [x] 6.2 保持客户端不可提交 scope，并在所有 HTTP 入口通过开源 scope resolver 固定构造 `local` DataEnvironment
- [x] 6.3 将受控媒体路由和搜索结果统一改为 `/media/{meme_id}`，保持 `POST /search` 的 `{results: string[]}` 结构，并让旧路径式请求返回明确错误
- [x] 6.4 更新任务摘要和详情媒体引用，使其通过任务 scope 与 `meme_id` 安全解析且不泄露原始 payload
- [x] 6.5 更新 Vue API 客户端和图片库、标注、搜索、任务界面，使用 `meme_id` 作为稳定 key 与命令参数，路径仅用于显示
- [x] 6.6 更新 API、前端和契约测试，覆盖重命名后组件状态保持、媒体访问、跨 scope 404、统一错误结构和既有工作流

## 7. 实施期临时旧数据导入

- [x] 7.1 编写一次性临时工具扫描现有图片并创建 `local` scope Meme，合法且指纹匹配的 sidecar 尽力导入，缺失、损坏或不匹配时标记为 `repair_required`
- [x] 7.2 明确忽略旧任务 JSON 和 `search-cache-v4.json`，不实现历史任务、旧向量、migration run 状态表或长期兼容读取
- [x] 7.3 停止旧服务后执行临时导入，核对图片数量、scope 内唯一路径和 SHA，并从数据库语境生成首个 1024 维 pgvector generation
- [x] 7.4 验证新版本核心工作流后删除临时导入工具，并确认应用运行时不包含任何旧 sidecar、任务 JSON 或搜索缓存读取路径

## 8. 启动门禁、运维与验收

- [x] 8.1 为 API 应用 lifespan 和现有 `start.sh` 实现启动健康门禁，校验数据库连接、pgvector、Alembic revision 和安装标记，失败时快速退出且不自动修改 schema；`database.sh start` 仍负责显式执行 Alembic
- [x] 8.2 测试 `database.sh start` 首次运行和重复运行，确认 PostgreSQL 数据卷、Alembic revision、`local` scope 与安装标记保持幂等
- [x] 8.3 更新 Docker、环境变量示例和开发文档，说明本地应用 + Docker PostgreSQL 的 `5434` 连接、`database.sh` 命令和具名 volume 备份恢复流程
- [x] 8.4 删除或隔离不再使用的 sidecar、搜索 JSON 和任务 JSON 运行时代码，确认全仓库不存在隐式回退或双写路径
- [x] 8.5 运行后端单元测试、PostgreSQL 集成测试、前端测试和生产构建，并修复所有既有契约回归
- [x] 8.6 在现有本地数据副本上演练临时图片导入、启动、搜索、任务恢复、重命名和删除，记录数量与指纹核对结果后删除临时工具
- [x] 8.7 使用至少两个 scope 执行对抗性隔离测试，并对数据库/文件故障点、Worker 崩溃和并发索引进行故障注入验收
- [x] 8.8 更新 OpenAPI 和用户文档中的稳定 Meme 标识及 PostgreSQL 部署要求，并明确本 change 不提供登录或多用户界面
- [x] 8.9 在旧 `image-sidecar-metadata` 与 `json-only-embedding-input` change 中记录 superseded-by 关系，并制定本 change 先归档、旧 change 后续 `--skip-specs` 归档的顺序
