## Context

合集 CRUD 已位于 `backend/collection_http.py`，而 `/collections/import` 仍直接依赖入口中的 multipart helper、scope environment、metadata service、operation policy、图片处理 Worker 和检索失效。导入包安全约束集中在 `backend/collection_packages.py`，Server 的导出边界独立位于适配层；本设计只移动导入的 HTTP 编排，不改变这些领域组件的所有权。

## Goals / Non-Goals

**Goals:**

- 让公共导入模块只接收已由入口装配的可变依赖，不导入 `api.py` 或 `server_api`。
- 保持 `/collections/import` 的 route metadata、multipart 字段、稳定错误、scope 绑定、资源预算、成员 SHA/path 事实和副作用顺序。
- 保留 `api.import_collection`、`api._collection_package_error` 等旧入口兼容名称，并确保 canonical import/export route 各只注册一次。
- 让逐成员失败仍被隔离，导入成功或已 durable 写入后的任务告警继续通过稳定结果字段返回。

**Non-Goals:**

- 不移动或修改 `backend/collection_packages.py` 的 manifest/ZIP/image 校验算法、metadata service、CollectionRepository、operation policy 或图片处理状态机。
- 不迁移合集 CRUD 或 Server 受限导出，不改变数据库 schema、迁移、前端和 Server middleware。
- 不新增客户端 scope/user 选择能力，不把 Server quota 或资源限制复制到公共核心模块。

## Decisions

### 1. 入口保留 route metadata，模块只暴露 handler

`api.py` 继续声明唯一的 `POST /collections/import` route，并把 `Request` 及所有宿主依赖 callback 传给新模块。这样 OpenAPI、路由顺序和 Server 导出覆盖事实不漂移；将 decorator 搬到模块的收益不足且会增加重复注册风险。旧 handler 通过同名薄 wrapper 保留，而不是让新模块反向导入入口。

### 2. 通过显式 callback 注入 scope 与副作用边界

新模块接收 environment、metadata service、settings、operation acquire/commit/release、processing worker/config、视觉任务提交、错误映射和检索失效 callback。模块不读取入口私有函数，也不从 `app.state` 猜测另一个 scope。这样真实 HTTP 请求的 scope 仍由 middleware 冻结，local 测试也可以提供轻量替身。

### 3. 保留并显式记录导入副作用顺序

处理顺序固定为：完整 multipart/ZIP 预检 → 当前 scope 合集名称冲突检查 → 创建合集并快照已存在图片 → 逐成员解析文件名；复用成员只建立合集关系，新增成员先 acquire upload operation，再 durable 写入图片/Meme，随后 commit（commit 不成功时不虚假回滚已完成写入），再建立关系，最后提交视觉或统一处理 Job。所有新成员完成后才失效检索缓存。任何已发生 durable 副作用后的任务或 policy 收束故障只作为逐项稳定告警，不把成功事实误报为失败。

### 4. ZIP 资源和身份验证继续由已有领域 helper 负责

导入模块复用受界限的 multipart parser/reader 与 `preflight_archive`，最大压缩字节、单成员/总解压字节、成员数量、图片帧/像素、路径和 SHA 校验均不在 HTTP 模块中重写。`resolve_import_filename` 只接受 manifest 已验证的导出文件名和 SHA；现有记录先通过 scope-bound BlobStore 的 identity 校验，避免文件被外部替换后错误复用。

### 5. 错误投影集中在导入边界并保留旧 helper

`CollectionPackageError` 到 status/error/message 的映射由新模块实现并导出，入口的 `_collection_package_error` 继续作为兼容 wrapper，导出路径仍可使用同一映射。数据库、metadata、系统和任务异常保持现有逐项稳定 code，原始路径和 traceback 不进入公开响应。

## Risks / Trade-offs

- [回调参数过多导致签名复杂] → 只注入当前路由确实使用的能力；每个 callback 在契约测试中用替身记录调用顺序，避免把环境或入口对象整体泄漏到新模块。
- [预检和 multipart 预算发生漂移] → 复用既有 parser/reader 和 collection package 常量，增加压缩、成员、SHA/path 与 content-length/chunked 相关测试。
- [导入后处理任务不可用] → 保留 durable Meme/合集关系，再以逐项 `metadata_job_error`/`processing_job_error` 或视觉错误字段返回；成功写入不被错误地回滚或隐藏。
- [Server 导出或旧调用重复注册] → 新模块不声明 route，静态 AST 检查拒绝 `api`/`server_api` import，route snapshot 排除导入/导出以外的 CRUD 重复并分别断言两条特殊 route 仅一次。

## Migration Plan

1. 在开源仓库新增导入 HTTP 模块、契约测试和本 change artifacts，让 `api.py` 使用薄 wrapper。
2. 运行导入/API/scope/security 定向测试、开源完整回归、compileall、OpenSpec strict validate 和 diff check，提交实现与独立验证记录。
3. 在 Server 先确认工作区只含既有用户/active change 脏文件，从本地开源仓库 fetch 精确完成 SHA，核验祖先关系后普通 `--no-ff` merge；不访问 upstream、不 push。
4. 运行 Server 导入/API/合集/scope/security 及相关回归，更新 `docs/refactor-plan.md` 记录来源实现/验证/收尾 SHA、Server merge SHA、祖先关系、范围、验证、skip 和回滚方式。

回滚时从对应 Server merge 恢复 `api.py` 原导入实现并删除 `backend/collection_import_http.py` 及其契约测试/本 change；合集 CRUD、Server 导出、数据库 schema 和图片包协议无需回滚。
