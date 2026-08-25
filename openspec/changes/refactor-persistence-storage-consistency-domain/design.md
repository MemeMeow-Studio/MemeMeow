## Context

上一组持久化 change 已把模型、engine、事务单元、资源装配和多个 Repository 移到
`backend.persistence`，但 `backend/database.py` 仍保留约九百行文件存储和协调逻辑。
`StorageOperation` 已记录上传、重命名、删除的 durable intent、字节指纹、CAS revision、
任务 claim 和标题指纹；`BlobStore` 负责 scope-bound 路径与 fsync；`StorageCoordinator`
负责跨数据库/文件系统的短事务、文件副作用、恢复和完整性扫描。本 change 必须移动实现而
不是复制实现，保持 local 根目录、非 local namespace、operation 状态矩阵、恢复器和旧 import
的事实。

## Goals / Non-Goals

**Goals:**

- 让 `backend.persistence.storage` 成为 BlobStore、StorageCoordinator 和相关辅助逻辑的单一实现来源。
- 让 resources 直接依赖 canonical storage，兼容 facade 仅显式 re-export；新模块不反向导入 `backend.database`。
- 锁定文件路径安全、原子不覆盖移动、fsync、数据库 durable fact、恢复/补偿、未知执行阻断及自动重命名 claim fencing 的既有语义。
- 通过契约、失败/恢复和静态边界测试证明没有第二套实现，并记录开源/Server 同步事实。

**Non-Goals:**

- 不修改 Alembic revision、数据库 schema、`StorageOperation` 字段/约束、公开 HTTP 协议或业务错误投影。
- 不迁移业务调用方的 `backend.database` import，不删除旧路径，不引入新存储后端或异步事务框架。
- 不修改 operation policy 的计费规则；只验证该域在 durable/unknown 边界不虚假 release 已提交 grant，相关 lease/slot 释放继续由各自 owner 负责。
- 不触碰 frontend、Server 用户控制或其它 active change 的脏文件。

## Decisions

### 1. Canonical 模块放在 persistence/storage.py

文件存储与数据库协调都属于持久化一致性域，放在同一 `storage.py` 可继续保留
BlobStore 与 Coordinator 的紧密不变量，同时与 models/engine/resources 的既有目录结构
一致。按 `blob.py` 与 `coordinator.py` 再拆会把一套恢复状态机分散到两个 review 边界；
保留在 `backend.database` 则无法形成 facade。两者都不采用。

### 2. 依赖方向只向下，资源模块从 canonical storage 装配

`storage.py` 只依赖 paths、storage security、engine 错误码、models 和 SQLAlchemy；对
`DatabaseResources` 仅用 `TYPE_CHECKING` 字符串注解。`resources.py` 顶层导入 BlobStore，
调用点直接使用 StorageCoordinator，避免 storage 反向导入 facade 形成循环。`database.py`
显式导入并 re-export 旧名称，所有旧调用方继续拿到同一个 Python 类对象。

### 3. 原样保留跨存储状态机和 fail-closed 规则

实现移动时不重写 SQL 或状态判断。上传继续先写 durable Meme/operation 再移动暂存文件；
rename/delete 继续先记录 intent，文件动作后复核身份并 finalize。任何无法证明副作用未发生
或无法与 claim/revision/sha 绑定的情况保持 blocked/unknown_execution；恢复器只在事实矩阵
唯一时提交或补偿。隔离文件不能进入公开路径，清理失败保留 operation 事实供恢复扫描。

### 4. 测试先锁对象身份再测失败矩阵

契约测试比较 canonical 与 facade 的类身份、AST 依赖和模块职责；BlobStore 测试覆盖路径
越界、符号链接、暂存和不覆盖移动；协调器测试覆盖 durable operation、rename/delete
恢复、ambiguous/blocked、CAS lease 过期及未知执行。PostgreSQL 集成测试按现有 marker
执行；未配置真实 PostgreSQL 时明确 skip，不把 SQLite 或 mock 当作 PostgreSQL 证明。

## Risks / Trade-offs

- [facade 漏导旧符号或产生导入循环] → 显式导出清单、对象身份测试、compileall，并让 resources 延迟导入只保留必要的 Repository 组合。
- [移动代码时改变恢复语义] → 以当前实现逐段移动，不改 SQL/状态分支；定向失败/恢复测试和已有 PostgreSQL 测试覆盖关键矩阵。
- [文件副作用与数据库事实不一致] → 继续用 operation durable intent、SHA/size、CAS revision、claim owner 和 blocked 状态；未知执行不自动补偿。
- [本地 Server 脏改动被覆盖] → 同步前只检查并确认目标文件无重叠脏改动；Server 侧只普通 merge 精确开源收尾 SHA，保留其它用户改动。
- [真实 PostgreSQL/Compose 不可用] → 验证记录明确列出 skip/未运行门禁，不能宣称已验证。

## Migration Plan

1. 在开源仓库创建本聚合 change artifacts，移动 canonical storage 实现、更新 resources/facade，补充契约和失败/恢复测试。
2. 运行目标测试、开源完整回归、OpenSpec strict、compileall、`git diff --check`，记录 PostgreSQL/Compose 是否真实运行。
3. 在开源仓库以聚合域提交实现、验证和收尾记录，固定精确 SHA；用户审核/授权后，从本地精确 fetch 该收尾 SHA，在 Server `main` 以普通 `--no-ff` merge。
4. Server 侧运行定向回归、compileall、diff check 和 strict validate，记录 merge SHA、祖先关系、变更范围、审核状态及未运行门禁。
5. 回滚时先恢复 facade 对应实现，再删除 canonical storage、契约测试和 change artifacts；不回滚 schema/migration 或数据。任何恢复失败保留 operation 的 blocked 事实，交给人工处理。

## Open Questions

无。实现路径、兼容边界和验证门禁均由现有代码与项目同步规则确定。
