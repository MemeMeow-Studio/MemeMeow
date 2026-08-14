## Context

当前服务在 FastAPI lifespan 中创建一套进程级服务：`PathResolver`、`MetadataService`、`SearchService`、`PersistentTaskService` 和 `OpenCodeRunner`。图片文件是主资源；meme 语境保存为同目录 sidecar，搜索向量保存在一个全局 JSON 文件并整体加载到 Python 内存，任务保存为逐任务 JSON，同时在进程内维护完整记录和线程池。

本设计需要在保留现有图片目录安全边界、语境证据约束、任务去重和批次行为的前提下，建立 PostgreSQL 权威存储。公共核心必须能够运行在一个已绑定的数据范围内，但不得引入登录、用户表或闭源账户语义。参见 `proposal.md` 和本 change 的 delta specs。

另一个已完成但尚未归档的 `image-sidecar-metadata` change 定义了现有 sidecar 行为。本 change 接管其中的语义 schema、证据边界、指纹校验和人工字段保护，但有意替换其物理持久化决策；归档时不能让主规范同时要求 sidecar 和 PostgreSQL 两套权威来源。

## Goals / Non-Goals

**Goals:**

- 让 PostgreSQL 16 + pgvector 成为结构化业务数据和向量索引的唯一运行时来源。
- 让重型资源保持进程级共享，让 scope 绑定对象按请求或任务短暂创建，内存占用随并发量而非用户总数增长。
- 以不可变 `meme_id` 分离业务身份和可变文件路径。
- 在数据库、文件存储和外部 Worker 之间建立可恢复的一致性边界。
- 以一次性、实施期临时导入保留现有图片和可用语境，完成后删除导入代码，运行时不双写、不回退。

**Non-Goals:**

- 不实现用户注册、登录、会话、配额、订阅或多用户管理；开源运行时只有 `local` scope。
- 不实现表情包/Collection；该功能作为后续 change 建立在稳定 Meme 身份之上。
- 不把图片原始字节或供应商反向图片缓存写入 PostgreSQL。
- 不兼容 SQLite、MySQL 或旧 JSON 运行时，不建立通用多数据库方言层。
- 不把任务系统拆成独立微服务，也不在本期引入 Redis、Kafka、Celery 等额外队列。
- 不把旧数据迁移建设成正式产品能力，不迁移旧任务历史和旧搜索向量，也不长期维护迁移 CLI、状态表或兼容层。

## Decisions

### 1. 使用 SQLAlchemy 2、psycopg 3、Alembic 和 PostgreSQL 16

应用使用 SQLAlchemy 2 的同步 Unit of Work 和 repository，驱动使用 psycopg 3，schema 迁移由 Alembic 管理，向量列使用 pgvector。当前主要业务服务和后台任务都是同步实现，采用同步数据库栈可以直接复用线程执行模型，避免在同一 change 中同时重写异步任务架构。

FastAPI 的数据库型路由应运行在工作线程，或仅在明确的线程边界内调用同步 Unit of Work，避免阻塞事件循环。Engine 和连接池在 lifespan 中创建一次；每个 HTTP 请求、任务认领事务和任务执行单元创建独立 Session，结束后提交或回滚并关闭。

备选方案是 SQLAlchemy AsyncSession，但 OpenCode 和现有任务处理器仍是同步阻塞流程，会迫使实现维护两套 Session 入口或进行更大范围的 async 改造。直接使用 psycopg SQL 也可行，但会增加 schema 映射、事务约定和测试样板。

### 2. scope 是公共核心的唯一数据边界，不是用户模型

新增不可为空的 `ScopeContext(scope_id)`。开源适配层在请求入口和任务提交入口固定产生 `scope_id="local"`，不接受客户端传入的 scope。以后闭源层可以从可信登录会话解析 scope，但该映射不属于公共核心。

每个 Unit of Work 根据 `ScopeContext` 创建已经绑定的 repository：

```text
进程级 AppResources
├── PostgreSQL Engine / Pool
├── BlobStore 客户端
├── OpenCodeRunner
└── Worker 管理器

请求级 DataEnvironment
├── ScopeContext("local")
├── SQLAlchemy Session
├── MemeRepository（已绑定 scope）
├── SearchRepository（已绑定 scope）
└── TaskRepository（已绑定 scope）
```

repository 的公开方法不再接收任意 `scope_id` 参数，而是在构造时绑定 scope；所有 SELECT、UPDATE、DELETE 和关联校验都包含该边界。数据库约束使用复合唯一键或复合外键防止跨 scope 关联。未来闭源部署可额外启用 PostgreSQL Row-Level Security 作为纵深防御，但本 change 不依赖 RLS 才能正确隔离。

文件访问同样由 scope-bound `BlobStore` 绑定命名空间。`scopes` 保存内部生成、不可变且不可由客户端提交的 `storage_namespace`。`local` scope 特殊映射到当前 `image_root`，因此迁移不移动现有图片；未来其他 scope 映射到独立的 `<data_root>/scopes/<storage_namespace>/images` 根目录。业务 `storage_key` 只在其 scope 命名空间内有意义，客户端即使提交其他 scope 的前缀也不能改变 BlobStore 根目录。

备选方案是让所有服务方法显式传递 `user_id`，这会把身份模型扩散到公共核心；每用户创建完整服务树则会复制连接池、搜索缓存和线程池。绑定 scope 的轻量环境同时避免这两个问题。

### 3. 使用稳定 UUID Meme 身份，路径只是 storage key

`memes.id` 使用应用生成的随机 UUID，创建后不可修改。核心字段如下：

```text
scopes
  id text primary key
  storage_namespace uuid unique not null
  created_at timestamptz

memes
  id uuid primary key
  scope_id text not null
  storage_key text not null
  extension text not null
  size_bytes bigint not null
  sha256 char(64) not null
  metadata_schema_version integer not null
  context_status text not null
  meme_context jsonb not null
  provenance jsonb not null
  extensions jsonb not null
  revision bigint not null
  created_at / updated_at timestamptz
  unique(scope_id, storage_key)
  unique(scope_id, id)
```

`storage_key` 保存 scope 专属 BlobStore 根目录下的 POSIX 相对路径，并始终通过绑定 scope 的路径解析器转换成真实路径。`local` scope 的 BlobStore 根目录就是现有图片根目录；其他 scope 使用独立根目录。数据库值不能绕过路径穿越、绝对路径和符号链接检查。`revision` 用于乐观并发控制；语境任务写回时同时校验 revision 和图片 SHA，避免旧任务覆盖较新更新。

`meme_context`、`provenance` 和未知扩展字段使用 JSONB，保留当前 schema 的演进空间；需要强约束和高频过滤的身份、状态、指纹字段使用独立列。备选方案是把完整 sidecar 原样放进单个 JSONB 列，但会削弱唯一约束、状态查询和指纹校验；把每个语义数组完全规范化为多张表则会过早放大 schema 和迁移成本。

### 4. 数据库是业务权威，文件存储通过操作日志实现可恢复协调

图片仍保存在文件系统。上传、重命名和删除无法与 PostgreSQL 形成单一 ACID 事务，因此增加 `storage_operations`：

```text
storage_operations
  id uuid primary key
  scope_id text not null
  meme_id uuid nullable
  operation_type text not null
  operation_token uuid unique not null
  source_key / target_key / staging_key text
  before_sha256 / after_sha256 text
  before_size / after_size bigint
  status prepared | file_applied | completed | compensated | blocked
  error jsonb
  created_at / updated_at timestamptz
  unique(scope_id, meme_id) where status in (prepared, file_applied)
```

操作先锁定 `(scope_id, meme_id)` 并在数据库中记录唯一 `operation_token` 与 `prepared` 意图，再使用同一 BlobStore 内唯一 staging key 执行原子移动，随后记录 `file_applied`，最后在一个短事务中更新 Meme 记录并标记 `completed`。普通异常立即执行补偿；恢复器以 `FOR UPDATE SKIP LOCKED` 独占一条操作，任何时刻最多一个恢复者处理同一 Meme。未完成操作关联的 Meme 不进入正常列表或搜索。

恢复判定采用确定矩阵，而不是猜测：`prepared` 且源存在、staging/目标不存在时可以安全重试；源缺失且唯一 staging/目标的指纹匹配时转入 `file_applied` 并完成数据库提交；源和目标同时存在、唯一对象指纹不匹配或出现不可能组合时转为 `blocked` 并停止自动修改。`file_applied` 只允许完成对应数据库变更或按记录的 before 指纹补偿。所有文件动作在执行前后重新执行 no-follow 路径校验和 stat/SHA 校验；实现优先使用目录 fd 与不跟随符号链接的打开方式降低 TOCTOU 风险。

删除先将文件原子移动到受控隔离区，再提交数据库删除，提交后异步清理隔离文件；上传先写临时文件并验证，再进入意图流程。这样数据库继续决定业务可见性，同时保留文件恢复能力。

备选方案是仅靠 try/except 反向重命名，无法处理进程在两个提交点之间退出；将图片字节直接存入 PostgreSQL 可以获得单库事务，但会扩大数据库、备份和媒体服务成本，且不利于未来迁移对象存储。

### 5. pgvector 固定为 1024 维并使用不可变 generation 原子激活

本期固定使用 BGE-M3 兼容的 1024 维 embedding，数据库列定义为 `vector(1024)`，配置增加只读的 `EMBEDDING_DIMENSIONS=1024` 并在启动和首次响应时校验。允许替换同为 1024 维的模型；切换到其他维度必须通过新的 OpenSpec change 和 Alembic schema migration，不能在同一表中按 generation 动态改变维度。本期先使用 pgvector 精确余弦距离查询，不建立 ANN 索引，保证 scope/generation 过滤后的结果完整；数据规模证明需要时再独立引入 HNSW/IVFFlat 方案。

向量索引按 scope 和 embedding 模型分代：

```text
search_generations
  id uuid primary key
  scope_id text not null
  model text not null
  dimensions integer not null check (dimensions = 1024)
  status building | ready | active | failed | retired
  source_snapshot_hash text not null
  created_at / activated_at
  unique(scope_id, id)

meme_embeddings
  generation_id uuid not null
  scope_id text not null
  meme_id uuid not null
  embedding vector(1024) nullable while building
  semantic_document text not null
  semantic_document_hash text not null
  metadata_hash text not null
  image_sha256 text not null
  item_status pending | ready | failed
  primary key(scope_id, generation_id, meme_id)
  foreign key(scope_id, generation_id) -> search_generations(scope_id, id)
  foreign key(scope_id, meme_id) -> memes(scope_id, id)

search_heads
  scope_id text not null
  model text not null
  active_generation_id uuid nullable
  primary key(scope_id, model)
  foreign key(scope_id, active_generation_id) -> search_generations(scope_id, id)
```

除上述外键外，激活事务还必须验证 `search_heads.model = search_generations.model`，并通过 `(scope_id, model, id)` 复合唯一键/外键直接在数据库中约束这一点；不能只依赖 repository 的应用层检查。

生成任务不持有跨外部 API 调用的长事务。它先在短 `REPEATABLE READ` 事务中读取全部可索引 Meme，将 `(meme_id, revision, image_sha256, semantic_document_hash, metadata_hash)` 固化为 generation item，并计算有序 `source_snapshot_hash`；`pending`、`repair_required` 和空语义记录不进入集合。随后在事务外调用 embedding，并分批写回对应 item。

激活前的新事务重新计算当前可索引 Meme 集合与每条 revision/hash。只有它与 generation 的源集合完全一致、全部向量非空且为 1024 维、任务 claim 仍有效时，才在单个事务中更新 `search_heads` 并激活新 generation；期间发生新增、删除、重命名、指纹或语境变化都会使 generation 以 `source_changed` 失败并重新排队。失败 generation 永不参与查询，旧 active generation 在刷新期间继续服务，后续再清理 retired 数据。

查询必须同时限定 scope、active generation，并关联当前 `memes` 记录，从而立即排除已删除资源。相同分数使用 `meme_id` 排序。应用不再把全部 embedding 加载到 Python 内存。

备选方案是原地 upsert 单张向量，更新简单但难以保证全量刷新期间索引一致和失败回滚；使用外部向量数据库会增加部署组件并削弱开闭源环境统一。

### 6. PostgreSQL 同时承担持久任务队列和全局并发租约

保留当前业务任务类型和处理器，但把进程内事实迁移为数据库记录：

```text
tasks
  id text primary key
  scope_id text not null
  task_type text not null
  lane text not null
  payload jsonb not null
  dedupe_key text nullable
  status queued | running | succeeded | failed
  progress / message / result / error
  settings_version bigint
  lease_owner text nullable
  lease_expires_at timestamptz nullable
  claim_generation bigint not null
  attempt_count integer not null
  max_attempts integer not null
  available_at timestamptz not null
  created_at / started_at / completed_at
  unique(scope_id, id)

task_batches
  scope_id / batch_id / sealed / finalizer_state
  primary key(scope_id, batch_id)

task_batch_items
  scope_id / batch_id / task_id
  foreign key(scope_id, batch_id) -> task_batches
  foreign key(scope_id, task_id) -> tasks

task_lane_slots
  lane / slot_number / task_scope_id / task_id / lease_owner / lease_expires_at
  primary key(lane, slot_number)
  unique(task_scope_id, task_id) where task_id is not null
  foreign key(task_scope_id, task_id) -> tasks(scope_id, id)
```

`task_id` 保持字符串类型，以无损导入当前 UUID 字符串和任何既有合法非 UUID 标识。Worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 认领 `available_at` 已到期的 queued 任务或租约已过期的可重试任务；每次认领递增 `claim_generation`，并以心跳续租。所有进度、终态、Meme 更新和 generation 激活都必须携带 `(task_id, claim_generation)` 并确认租约仍有效，更新行数为零时丢弃过期 Worker 结果。

`task_lane_slots` 将 Agent 并发限制为数据库全局槽位，而不是每进程各自限制；提交任务时锁定 lane 状态并检查排队上限，实现跨进程背压。活动任务在 `(scope_id, task_type, dedupe_key)` 上建立 `WHERE status IN ('queued','running') AND dedupe_key IS NOT NULL` 的条件唯一索引。索引 generation 的 dedupe key 包含模型和源快照，语境任务包含 `meme_id` 与图片 SHA。

批次关系单独持久化。批量请求先在同一请求事务中写入全部成员并将 batch 封口；Worker 在同一数据库事务内锁定已封口 batch、插入带唯一 dedupe key 的索引任务并把 finalizer 从 pending 更新为 complete；两者不能分开提交。这样崩溃发生在事务前时可重试，事务后则任务与 finalizer 同时可见。任务列表继续只返回安全摘要，完整 payload 只在受控详情和 Worker 内部使用。

备选方案是继续让每个应用进程维护线程池和任务字典，这会重复任务、突破并发上限且无法可靠恢复；引入专用消息队列会增加本 change 不需要的运维组件。PostgreSQL 队列适合当前规模，未来吞吐成为瓶颈时可在不改变任务业务契约的前提下替换执行适配器。

### 7. 开发环境由 `database.sh` 准备 PostgreSQL，本地运行应用

开发模式只将 PostgreSQL 16 + pgvector 和一次性数据库准备命令放入 Docker Compose；MemeMeow API 与 Vue 继续由宿主机现有 `start.sh` 在 tmux 中启动。PostgreSQL 容器内部监听标准 `5432`，Compose 只将其映射到宿主机回环地址 `127.0.0.1:5434`，不向局域网或公网暴露。

容器内与宿主机使用不同连接地址：

```text
宿主机 MemeMeow  → postgresql+psycopg://...@127.0.0.1:5434/mememeow
数据库准备容器   → postgresql+psycopg://...@postgres:5432/mememeow
```

新增根目录 `database.sh`，只管理数据库基础设施，支持 `start|stop|restart|status|logs|migrate`。`start` 是用户统一入口，内部依次执行：

1. 校验 Docker Compose 和必需环境变量；
2. `docker compose up -d postgres` 并等待健康检查；
3. 运行一次性数据库准备容器执行 `alembic upgrade head`；
4. 运行应用初始化命令，幂等创建 `local` scope 和安装标记。

PostgreSQL 容器常驻；数据库准备容器完成后退出。重复执行 `database.sh start` 不重建数据库、不删除具名 volume，也不会重复创建 `local`。不增加 `install.sh`，避免“安装、数据库启动和升级”出现重叠入口。

现有 `start.sh` 继续只管理宿主机 API 和 Vue，不隐式启动数据库或执行 schema migration；它在启动前检查 `127.0.0.1:5434` 和数据库健康，未就绪时提示先执行 `./database.sh start`。这样数据库错误不会被隐藏在 tmux 后台进程中。

### 8. `local` scope 初始化是应用数据初始化，不是账户创建

Alembic 只负责 pgvector 扩展、表、约束和索引；独立 Python 应用初始化命令负责理解 MemeMeow 数据模型并执行：

```text
确认 Alembic revision 与当前代码一致
        ↓
INSERT local scope（ON CONFLICT DO NOTHING）
        ↓
写入或验证本地安装标记
```

`local` 表示当前开源实例唯一的数据范围，不是用户账户，不产生登录、密码或前端可见实体。所有开源业务请求仍由服务端内部绑定 `scope_id="local"`。初始化命令必须幂等；数据库 schema 未就绪或已有不一致安装状态时明确失败，不能猜测修复。

应用启动只检查 PostgreSQL 连接、pgvector、Alembic revision 和安装标记，不自动创建数据库、执行 Alembic 或初始化 scope。测试只覆盖 PostgreSQL，不维护 SQLite 方言；关键集成测试必须实际运行事务、向量查询、锁和租约行为。

### 9. 旧数据只做实施期临时导入

现有数据价值有限，因此不建立正式迁移 CLI、`migration_runs/items`、源数据锁、任务历史迁移、重试协议或旧文件清理产品。实施切换时编写一个临时工具，在停止旧服务后完成以下最小工作：

1. 扫描现有图片，为每张可读图片创建 `local` scope 的 `meme_id` 和数据库记录；
2. sidecar 合法且指纹匹配时导入其语境、来源和未知扩展字段；缺失、损坏或不匹配时将图片导入为 `repair_required`；
3. 忽略旧任务 JSON 和 `search-cache-v4.json`，不导入历史任务或旧向量；
4. 核对数据库 Meme 数量、唯一路径和图片 SHA，再从数据库语境重新生成首个 pgvector generation；
5. 确认新版本可用后删除临时导入工具，旧 sidecar、任务 JSON 和搜索缓存可直接手工删除。

临时导入不是用户可调用能力，也不进入长期运行路径。若切换尚未投入使用且执行失败，仅允许销毁并重建新数据库后重新运行；不提供恢复旧运行时、回滚到旧数据或在线迁移回滚流程，也不为低价值旧数据承担跨版本兼容或幂等恢复复杂度。切换完成后应用只读取 PostgreSQL 和图片文件。

### 10. 明确替代尚未归档的 sidecar 规划产物

`image-sidecar-metadata` 与 `json-only-embedding-input` 已经提供了当前代码所需的语义 schema、Agent 写回、任务和 JSON-only embedding 行为，但它们尚未归档，物理存储决策与本 change 冲突。本 change 的 delta specs 完整承接最终仍需保留的证据边界、字段白名单、不可用语境跳过和任务行为，并以 PostgreSQL 形式定义最终契约。

归档顺序必须是：先实施并归档本 change，使最终规格进入主规范；再将上述旧 change 作为历史实现记录使用 `--skip-specs` 归档，不得把其 sidecar、JSON 缓存或逐任务 JSON delta 再合并进主规范。实施期间同步在旧 change 的说明中记录 superseded-by 关系，防止维护者误按旧规划继续演进。

## Risks / Trade-offs

- [Risk] PostgreSQL 提高开源部署门槛 → 通过 `database.sh start` 封装固定版本容器、健康检查、schema 升级和幂等初始化，应用继续保留宿主机热重载体验。
- [Risk] 文件系统和数据库无法原子提交 → 使用持久 storage operation、隔离区、指纹校验和启动恢复器，并对每个中断点进行故障注入测试。
- [Risk] PostgreSQL 队列在高吞吐下产生锁竞争 → 采用短认领事务、`SKIP LOCKED`、租约和明确索引；当前规模不提前引入外部消息系统。
- [Risk] generation 模型会暂时保存两份或多份向量 → 激活后异步清理 retired generation，并为失败和历史 generation 设置保留上限。
- [Risk] scope 条件遗漏造成数据越界 → repository 构造时强制绑定 scope，使用复合约束，并用 Alice/Bob 风格的双 scope 集成测试覆盖列表、详情、媒体、搜索、任务和写操作。
- [Risk] scope 数据库隔离正确但物理文件仍冲突 → BlobStore 绑定不可变 storage namespace，`local` 独占现有根目录，双 scope 测试实际执行同逻辑路径的上传、读取、重命名和删除。
- [Risk] 临时导入工具不提供生产级恢复保证 → 旧数据价值有限，切换前停止旧服务并保留图片副本；失败时重建尚未启用的新数据库，成功后删除临时代码。
- [Risk] 已完成的 sidecar change 与本 change 发生规格冲突 → 在本 change 实施和归档前明确由 PostgreSQL `image-metadata` 规格取代旧物理存储决定，避免归档出相互矛盾的主规范。

## Migration Plan

1. 引入 PostgreSQL/pgvector Compose 服务、`database.sh`、数据库依赖和 Alembic 基线，不切换业务读写。
2. 建立 schema、scope-bound Unit of Work、repository 和数据库级集成测试。
3. 将 Meme 元数据及图片生命周期切换到数据库与 storage operation，旧格式读取仅保留在尚未执行的一次性临时导入工具中。
4. 将固定 1024 维搜索 generation 和查询切换到 pgvector，再迁移带 fencing claim 的任务队列、租约、批次和全局并发控制。
5. 停止旧服务并保留图片副本，运行临时工具导入图片和可用 sidecar，忽略旧任务与旧向量，核对数量和指纹。
6. 从数据库语境生成新的 pgvector 索引，启动只支持 PostgreSQL 的新版本并验证核心工作流。
7. 删除临时导入工具和不再使用的旧 JSON，继续使用 `database.sh` 管理数据库生命周期。
