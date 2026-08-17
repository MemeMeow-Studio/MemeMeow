# 图片处理与搜索向量迁移

本文说明现有图片如何进入逐图处理链，以及搜索从旧的全库 `cache_generation` 切换到
单图文本 embedding 的运维边界。迁移工具只接受服务端数据库和配置，不把全库图片 ID
放进任务 payload，也不把 provider secret 写入参数、日志或结果。

## 前置条件

- 使用项目的 `uv` 环境，并连接可用的 PostgreSQL/pgvector 数据库。
- 目标 `scope_id` 已存在，且维护者已确认该 scope 的图片根和 embedding 配置可用。
- 迁移前保留数据库备份；迁移不会删除旧 generation、旧向量或图片文件。

## 分页回填

先只创建逐图 job，适合检查规模和处理队列：

```bash
uv run python scripts/backfill_image_processing.py \
  --scope-id local --page-size 100 --seed-only
```

默认模式会在分页 seed 的同时为可索引语境生成单图文本向量：

```bash
uv run python scripts/backfill_image_processing.py \
  --scope-id local --page-size 100
```

脚本按 `(storage_key, meme_id)` keyset 分页，每页处理后持久化进度；重复执行会复用同一
图片版本的 job 和向量，不需要也不允许构造包含全库 ID 的单个任务。当前 scope 中有存储
操作处于 `prepared` 或 `file_applied` 的图片会暂时跳过，避免把未完成的文件副作用当作
迁移事实。

## 搜索来源切换

迁移开始时服务端创建新的 `SearchMigrationState` epoch，并冻结可验证的旧 generation
引用。回填期间一次查询只使用旧 generation 的逐条校验结果；不会把旧 generation 和
新单图向量混在同一次查询中。每条旧记录都必须匹配当前 scope、图片 SHA、metadata hash、
模型、维度、语境状态和可访问的业务存储键。

确认所有图片都成功回填后，才使用显式切换：

```bash
uv run python scripts/backfill_image_processing.py \
  --scope-id local --page-size 100 --switch
```

只有本次运行没有失败、处理数量完整且不是 `--seed-only` 时才会切换到
`incremental_only`。失败或数量不完整不会切换来源；修复配置或图片后重新执行即可。
切换后日常搜索只读取当前 scope 中与图片 SHA、metadata hash 和 embedding 模型匹配的
单图向量，旧 generation 仍保留供审计或受控回滚检查。

## 失败、未知执行与回滚

图片处理阶段的外部执行结果无法确认时以 `unknown_execution` 收束，不会因为迁移重启而
自动重放 Agent 或 provider 调用。通过处理任务页面或受控 API 显式重试会创建新的 job
revision 和必要的叶子 Task；旧任务、attempt 和 operation 事实保留。

回滚时先停止新 job 的认领和迁移命令，保留新表、向量、job 和迁移 epoch。只有在确认旧
generation 仍与当前图片和语境逐条匹配后，才允许由部署方执行受控回滚；不要手工删除表、
清空迁移状态或把旧 generation 重新标记为当前来源。没有可验证旧来源时，应保持搜索未就绪，
而不是返回可能过期的结果。

当前环境未提供可用 PostgreSQL/Docker，因此本地验收覆盖了脚本参数、分页逻辑和契约测试，
真实数据库建表、双 scope 外键以及生产量级回填仍需在 staging 数据库执行。
