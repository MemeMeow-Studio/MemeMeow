## 1. OpenSpec 与配置契约

- [x] 1.1 校验 proposal、delta specs 和 design 的 capability 路径与中文格式
- [x] 1.2 在 Settings 中增加服务端上传边界字段、别名、默认值和严格校验
- [x] 1.3 在 `/config` 和共享前端类型中暴露文件数、并发数和可选总字节预算
- [x] 1.4 更新 API 文档/契约测试，确认默认 disabled 且不出现 64 MiB 开源假设

## 2. 后端有界上传与幂等认领

- [x] 2.1 为 multipart 入口增加最多 20 文件的解析/请求级拒绝和稳定错误映射
- [x] 2.2 实现不依赖 Content-Length 的 spool 分块读取，执行单文件上限和可选总请求字节预算
- [x] 2.3 保持现有逐文件校验、storage operation 和部分成功行为
- [x] 2.4 增加当前 scope 下数据库记录、文件大小和 SHA-256 一致性查找
- [x] 2.5 在一致事实上复用既有 Meme/processing job；为冲突、孤立文件和事实不一致返回稳定错误
- [x] 2.6 编写后端边界、chunked/无 Content-Length、预算、幂等、冲突和 scope 隔离测试

## 3. 前端批量调度器

- [x] 3.1 实现按最多 20 文件与可选总字节预算切片的纯函数，覆盖单个文件超预算不死循环
- [x] 3.2 扩展请求错误元数据和上传 API 的 AbortSignal/429 Retry-After 契约
- [x] 3.3 实现最多 2 并发、批次进度、逐项状态、暂停/继续、取消和仅失败重试
- [x] 3.4 更新 UploadWorkspace 视图，提供部分成功汇总、错误可恢复动作和轻量列表渲染
- [x] 3.5 编写 >20/上千文件、并发、预算、暂停、取消、失败重试、429 和部分成功 Vitest
- [x] 3.6 运行前端 typecheck/build、相关单元测试和必要 E2E

## 4. 验证与交付

- [x] 4.1 运行 OpenSpec strict validate 并确认全部 tasks 完成
- [x] 4.2 对本次 diff 做安全、并发、权限、事务、回滚和测试覆盖 review，修复 P1/P2
- [x] 4.3 运行后端相关 pytest、`git diff --check` 和前端验证命令
- [x] 4.4 只暂存本 change 与实现/测试文件，确认预存用户脏文件不在 commit
- [x] 4.5 创建边界清晰的开源 Git commit，记录精确 SHA 后停止，不 archive/push/sync
