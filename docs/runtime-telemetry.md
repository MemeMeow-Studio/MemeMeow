# 公共运行观测

应用与 executor 使用 OpenTelemetry Python SDK 1.45.1，instrumentation 使用 0.66b1。配置由宿主提供，默认关闭。

## 配置与生命周期

- `MEMEMEOW_TELEMETRY_ENABLED`：只接受 `true` 或 `false`。
- `MEMEMEOW_TELEMETRY_ENDPOINT`：Collector OTLP HTTP 基础地址，不包含凭据、查询参数或 fragment。
- `MEMEMEOW_TELEMETRY_ENVIRONMENT`：部署环境，默认 `development`。
- `MEMEMEOW_TELEMETRY_AUTH_FILE`：可选的受保护文件，包含单行 Basic Authorization；配置后供三种 OTLP 信号共同使用。

API SDK 随 FastAPI lifespan 启动和关闭；executor SDK 随主进程启动和关闭。导出使用官方 HTTP exporter 与后台 SDK，trace 队列最多 2048 条，每批 128 条，导出超时 5 秒。trace 全量采集，metrics 每 10 秒独立导出。超过 SDK 属性或事件数量限制时，SDK 保留丢弃数量；文本属性不设置额外长度上限。Collector 与后端的接收限制需要由宿主配置并验证。

## 来源与单位

OTLP 使用独立 HTTP Session 直接连接宿主声明的 Collector；业务出口代理与 Agent 子进程环境保持原有规则。秒数 histogram 通过官方 View 设置 5 毫秒至 300 秒边界，包含 200 毫秒数据库等待与三秒搜索阈值。

FastAPI、HTTPX、HTTPX2、requests、urllib 和 SQLAlchemy 使用对应官方 instrumentation。HTTP 请求的路由由 FastAPI instrumentation 识别，SQL 执行与连接池等待分别测量。线程上下文使用官方 threading instrumentation。

进程启动时分别启用 `HTTPXClientInstrumentor` 和 `HTTPX2ClientInstrumentor`，自动采集对应库的同步及异步客户端。`instruments-any` 依赖确保两个客户端库可用。OpenAI SDK 使用默认客户端与连接参数，命名请求的超时与重试参数保持有效。

`runtime_operations` 和 `runtime_operation_duration` 的单位由 `unit` 标签声明。Python Provider 的客户端操作使用 `python_provider_call`，底层 HTTP instrumentation 提供各次实际网络请求，SDK 内部重试保留在该 HTTP 来源中。缓存未调用客户端时不增加 Provider 操作指标。Task 指标在持久提交成功后增加，重新排队使用独立状态。

`task_terminals` 使用所属 Session 工厂的标准事件观察已有 Task 更新；SQL fencing 更新使用同一个事务快照入口。保存点只合入父事务，最外层提交成功后计数，回滚不计数。标签包含 `task_type`、`result` 和稳定 `reason`；业务 `failed` 且原因为 `task_cancelled` 时结果类别为 `cancelled`。队列扫描按串行内存快照更新 `task_queued`，清零已消失类别，网络导出仍由 SDK 执行。

executor 使用独立 attempt trace，并通过 W3C 上下文的 Span Link 关联 Worker。callback 薄客户端传递当前 attempt 的 traceparent；追踪字段不参与业务授权。调查字段保留 Task、scope、attempt、session 和业务图片相对路径，metrics 不使用这些标识作为标签。

## OpenCode SQLite

`executor/sqlite_telemetry.py` 使用当前执行路径和 session 绑定读取 `session`、`message` 与 `part`。连接为只读，等待上限 250ms，查询使用短事务与进度检查。主 session 与子 session 的目录必须符合当前执行绑定；读取不修改数据库。

后台每两秒读取，结束时读取一次最终内容。消息、步骤和工具分别统计；记录使用来源 ID 和保存内容识别更新，连续采集期间同一终态只统计一次。初次读取及错误恢复后的已有终态作为基线，只提供调查。记录时间不足以确认 attempt 时保留 Task/session，标明 `undetermined`。

读取错误、恢复与已知中断区间进入 trace 和结构化日志；项目来源 metrics 记录成功时间、读取错误、活动来源及活动错误数量。组件接收、发送和队列状态由 Collector 自身指标提供。

`emit_event` 从已有内存快照发送结构化日志，使用官方日志 SDK 与 OTLP exporter，独立队列最多 2048 条、每批 128 条、发送超时 5 秒。日志及 trace 导出错误分别保留实际原因与最后成功、错误时间。SDK 关闭后再次启动会明确报告进程生命周期错误。

## 内容保护

专用 trace 内容允许已取得的业务文本，结构化认证字段、已知环境凭据值和认证文本经过过滤。自动 instrumentation 的属性、事件与状态在 exporter 边界再次处理。二进制内容只保存大小或业务文件关联。公开日志和用户响应保持各自原有内容规则。

认证字段包括 `accessToken`、`refreshToken`、`sessionToken` 等 camelCase 名称；消息 `tokens.input`、`tokens.output` 用量保持可查询。

## 验证

`tests/test_runtime_telemetry.py` 使用真实 SDK 和内存 exporter 验证内容保护与 W3C 解析。网络导出、真实 OpenCode 数据、数据库等待及跨进程关联需要在宿主既有开发环境执行，不能使用内存测试结果声称这些项目已经通过。
