## Purpose

为持久化模型声明提供单一、可审查且保持历史导入兼容的边界，使数据库模型拆分可以独立回滚而不改变现有 schema 事实或业务调用协议。

## ADDED Requirements

### Requirement: ORM declarations have one metadata source

系统 MUST 只使用一个声明式 Base 和一个 SQLAlchemy metadata 注册全部现有业务表；模型模块 MUST 包含 scope、图片、合集、搜索、任务、callback、operation grant、图片处理和迁移状态等现有 ORM 声明，并保持现有表名、列、约束和索引事实。

#### Scenario: Metadata keeps the existing table set

- **WHEN** 应用导入持久化模型并读取 Base metadata
- **THEN** metadata 包含现有业务表集合，且固定文本/视觉 embedding 维度与关键唯一约束、调度索引保持不变

#### Scenario: No duplicate model identity is created

- **WHEN** 调用方分别从 `backend.persistence.models` 和 `backend.database` 导入同一 ORM 类
- **THEN** 两个路径返回同一个 Python 类对象和同一个 Base metadata，而不是两套声明

### Requirement: Legacy database imports remain compatible

`backend.database` MUST 继续导出既有模型类、`Base`、`ScopeContext`、模型维度常量、`utcnow` 和 optional control table 集合；既有 Repository、migration、资源装配和业务模块无需改变导入路径即可运行。

#### Scenario: Existing model imports still resolve

- **WHEN** 现有业务模块从 `backend.database` 导入模型、ScopeContext 或维度常量
- **THEN** 导入成功且对象与持久化模型模块中的对应对象相同

#### Scenario: Alembic continues to use the same metadata

- **WHEN** Alembic 环境从 `backend.database` 读取 Base
- **THEN** 它读取到模型模块的同一 metadata，migration head 和 schema 版本事实不因模块移动而变化

### Requirement: Model module has a one-way dependency boundary

模型模块 MUST 不导入 `backend.database`、HTTP 入口、Repository、BlobStore、StorageCoordinator 或资源装配模块；模型声明只能依赖其所需的 SQLAlchemy 类型和稳定基础值。

#### Scenario: Model module can be inspected without database facade code

- **WHEN** 静态检查 `backend/persistence/models.py` 的导入边界
- **THEN** 不存在反向 `backend.database` 导入，且模型文件不包含 Repository/文件存储装配实现
