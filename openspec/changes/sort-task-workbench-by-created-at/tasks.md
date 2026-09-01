## 1. OpenSpec 与边界

- [x] 1.1 创建 proposal、task-status/frontend-workbench delta spec、design 和任务清单，明确普通用户范围、创建时间倒序和 cursor 二元组。
- [x] 1.2 核对开源与 Server 对应实现、既有 scope/分页/前端混排边界，确认管理员列表和 Worker 调度不被修改。

## 2. 公共任务排序实现

- [x] 2.1 修改 PostgreSQL `TaskRepository.list` 的排序、cursor 条件和 docstring，使用 `created_at DESC, id DESC`。
- [x] 2.2 修改兼容内存任务服务的排序、cursor 行为和 docstring，保持筛选、页大小和快照语义。
- [x] 2.3 修改图片处理父 Job 列表按创建时间和 ID 倒序，保持 scope 查询和 snapshot 行为；Worker 恢复扫描使用独立的活动 Job 查询。

## 3. 普通用户工作台

- [x] 3.1 修改工作台普通任务与父 Job 的混排键为 `created_at`，状态和轮询更新不触发位置变化。
- [x] 3.2 将表头和两类行的时间显示改为“创建时间”，详情创建/完成时间保持不变。

## 4. 测试与验证

- [x] 4.1 增加后端 repository/兼容服务和 cursor 的创建时间排序回归测试。
- [x] 4.2 增加图片 Job 和前端混排、轮询位置稳定、创建时间文案回归测试。
- [x] 4.3 运行定向测试、OpenSpec strict validation、Python 编译、前端类型/测试和 `git diff --check`。

## 5. 开源提交门禁

- [x] 5.1 检查开源 diff 只包含本变更，提交精确实现 SHA 并记录验证结果。
- [ ] 5.2 向用户提供开源 commit、范围和测试结果；未经审核授权不 fetch/merge 到 Server。
