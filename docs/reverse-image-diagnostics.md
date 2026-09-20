# 反向图片调用诊断

`ReverseImageService.search_async()` 每次退出时，通过 Loguru 输出一条
`reverse_image_completed` 事件。同步 `search()` 使用相同路径。消息包含 JSON 字段，
输出位置与轮转由宿主现有的 Loguru 配置负责。

## 阶段耗时

`duration_ms` 是此次服务调用的总耗时。下列字段采用单调时钟，单位为毫秒，重复进入
同一阶段时累计耗时；未进入的阶段省略。各阶段时间互不重叠，其总和与 `duration_ms`
相等，允许小数舍入误差。

- `validation_ms`：图片、任务、callback、已有 usage 的校验与读取，以及缓存锁内的再次校验。
- `lock_wait_ms`：取得缓存文件锁之前的等待时间。
- `cache_read_ms`：读取缓存及判断是否可以使用缓存的时间。
- `quota_ms`：申请额度、确认额度状态和提交计量的时间。
- `provider_ms`：供应商调用及等待其明确结束的时间。
- `persist_ms`：缓存命中后的结果记录、调用前开始标记、供应商结果处理及保存。

阶段按领域服务中的操作边界计时。`persist_ms` 包含结果处理和事务提交；
`validation_ms` 包含相应的数据库访问。它们均不代表独立的数据库查询耗时。

## 状态与错误

- `provider_called`：本次服务调用是否进入供应商调用阶段。
- `provider_outcome`：本次供应商调用得到的结果，仅在有相关事实时记录。
- `replay`：是否返回已有 usage 记录。
- `recorded_provider_called`、`recorded_outcome`：返回的 usage 中保存的历史事实。
- `cache_status`：缓存状态，成功、失败和取消事件在已经知道状态时均保留。
- `outcome`：本次服务退出状态；供应商成功后保存结果失败仍记录失败。
- `error_stage`、`error_code`、`error_type`：最早记录的错误阶段、稳定错误码及异常类别。
- `cause_code`、`cause_type`：异常直接原因中可用的稳定错误码及类别。
- `final_error_stage`、`final_error_code`、`final_error_type`：最终向调用方传播的错误。
- `sqlstate`、`constraint_name`：数据库错误中可用的 SQLSTATE 与约束名称。
- `cancel_requested`、`cancel_stage`、`cancel_wait_ms`：是否收到取消、首次取消阶段及之后等待的时间。

`cancel_wait_ms` 与阶段耗时存在重叠，不参与阶段时间求和。供应商已经开始时，服务仍
按既有规则等待明确结果并保存计量事实，随后传播取消。日志保留本次供应商结果及
最终取消状态，不能用其中一个代替另一个。

日志不输出异常正文、SQL、SQL 参数、图片内容、原始路径、URL、请求参数或凭据。

## 关联与依赖

宿主可以在请求入口使用 `logger.contextualize(trace_id=...)` 设置随机生成的
32 位十六进制 `trace_id`。Loguru 自动把上下文传递给异步子任务；Starlette 的线程池
同样传递上下文。自行创建的后台线程应在创建时保存关联信息，并在输出时显式绑定。

宿主可提供既有的 `task_digest`、`scope_digest`、`attempt_digest`、
`executor_attempt_digest`。公共日志模块仅接受 `digest:` 加 16 位十六进制的关联值，
不负责读取任务、验证 callback 或生成摘要。没有上下文时，事件仍正常记录。

`OperationDiagnostics` 只由业务模块局部持有。它不依赖 HTTP、供应商或数据库模块，
业务判断不读取其计时字段。公共服务的请求参数和返回结构保持原有形式。

## 验证

`tests/test_operation_diagnostics.py` 使用真实 Loguru、文件锁、异步取消和线程池，
验证计时、异常传播、消息内容和并发关联。

`tests/test_reverse_image_diagnostics_integration.py` 要求显式设置开发环境的
`MEMEMEOW_DIAGNOSTICS_DATABASE_URL`。测试在现有 PostgreSQL 中创建带
`diagnostics_test_` 前缀的独立 schema，使用当前 ORM 表定义和真实服务验证缓存、
usage 重放、取消和唯一约束错误。不启动额外服务，不改动既有业务表，测试 schema
保留供检查。该测试验证服务和 ORM 路径，不替代宿主启动或数据库迁移验收。

pytest 的 `--basetemp` 应指向工作区内已忽略的测试目录。
