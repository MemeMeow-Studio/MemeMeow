## Context

`api.py` 中上传入口同时承担 Starlette multipart 读取、图片资源预检、scope metadata 访问、
operation policy 生命周期以及视觉/统一处理 Job 编排。已有图片查询、处理提交和变更边界
已经抽取；上传仍是最大的剩余图片写入入口，且合集导入还复用 multipart 读取 helper。

## Goals / Non-Goals

**Goals:**

- 将上传专属 parser、单文件受限读取、幂等结果和逐文件 HTTP 编排迁移到不依赖入口的公共模块。
- 让 scope service、错误投影、文件/图片预检、policy 生命周期和处理任务通过显式 callback
  注入，保留入口的路由元数据与兼容 helper 名称。
- 为 policy 拒绝、明确可补偿错误和 Server 适配层的差异保留宿主可配置的错误/释放策略。

**Non-Goals:**

- 不迁移合集导入/导出 route、图片处理实现、BlobStore/MetadataService、operation policy
  存储、scope middleware、数据库 schema、前端或配置字段。
- 不改变 URL、method、status、响应 key、multipart 字段、文件路径规则、symlink/race 防护或
  公开的批量部分成功语义。

## Decisions

### 1. Route 保留在入口，编排函数通过 callback 注入

`api.py` 保留 `@app.post("/images/upload")`、`upload_images` 名称和请求对象形状，只做一层
wrapper 并注入依赖。新模块不注册 FastAPI route，避免 canonical route 重复或 OpenAPI 顺序
漂移。相比让新模块读取 `request.app.state`，显式 callback 能把 scope 和 Server 适配层的
策略差异锁在宿主，同时允许单元测试使用轻量替身。

### 2. 把 parser helper 与上传边界一并移动并保留兼容别名

`_BoundedUploadMultipartParser`、`_parse_upload_form` 和 `_read_upload_content` 进入新模块。
入口重新导出这些旧名称，合集导入继续通过入口别名使用同一有界 parser，避免产生两个可能不
一致的请求体预算实现。新模块只依赖 Starlette、FastAPI 和 backend 稳定业务类型，不反向导入
`api.py`/`server_api`。

### 3. 保留逐文件副作用顺序，宿主控制 policy 投影

上传仍按“解析全请求边界 → 选项规范化 → 单文件读取/预检 → 三方幂等查找 → acquire →
durable upload → commit → 处理任务 → 检索失效”的顺序执行。默认开源宿主保留逐项 policy
拒绝结果；Server 通过 callback 将单文件拒绝映射为其既有 HTTP 错误，同时继续让多文件请求
保留部分成功。release 错误集合也由宿主注入，避免公共切片覆盖适配层更保守的未知暂存事实。

### 4. 兼容结果由统一 callback 组合

模块不构造当前 scope 的 task/processing 依赖，而接收 service provider、幂等处理状态构造器、
新图片 processing submitter、旧视觉 submitter、配置 provider 和检索 invalidator。模块只负责
排序和稳定结果投影；任务提交失败继续写入诊断字段，不回滚已经 durable 的文件事实。

## Risks / Trade-offs

- [callback 参数遗漏导致上传成功但任务状态漂移] → 保留旧 handler/helper 别名并用模块级契约
  测试检查 callback 调用顺序和兼容字段。
- [multipart helper 与合集导入预算不一致] → parser 只保留一个公共实现，继续从入口 re-export，
  运行上传边界和合集回归。
- [operation commit/release 失败被错误退款或误报] → durable 前后分段处理，release 集合由
  宿主显式传入；commit 异常只保留成功事实。
- [Server 当前 dirty 适配层冲突] → 开源先独立提交，Server 只 fetch 精确 SHA 并在 merge 时
  处理本切片相关 import/wrapper/常量冲突，不覆盖其它脏文件。

## Migration Plan

1. 在 MemeMeow 新增公共上传模块、契约测试及本 change artifacts，删除 `api.py` 中重复业务
   实现并保留兼容 wrapper。
2. 运行上传/API/scope/security 定向回归、开源完整回归、compileall、diff check 和 strict
   validate；提交独立实现 SHA 与验证/收尾 SHA。
3. 检查 MemeMeowServer 工作区状态后，从本地精确 SHA fetch，并通过普通 `--no-ff` merge；仅
   解决上传切片相关冲突，随后运行 Server 定向回归。
4. 回滚时恢复入口原实现并删除新模块、测试和 change artifacts；不执行 schema 或数据迁移。
