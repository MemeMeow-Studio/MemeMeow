## 1. 基线与 OpenSpec

- [x] 1.1 记录四个任务 route 的 path/method/status/tag/order、参数、服务调用顺序和现有脏状态；确认 Server 工作区在实现阶段不被触碰。
- [x] 1.2 读取任务摘要、活跃度、公开 DTO、续跑和错误映射测试，冻结兼容与安全边界。

## 2. Task HTTP 模块实现

- [x] 2.1 新增带中文文件/函数 docstring 的 `backend/task_http.py`，迁移任务摘要、活跃度 reader 和四个任务 handler；模块不得导入 `api.py` 或 `server_api`。
- [x] 2.2 通过显式 service、error、processing repository 和 Agent cancel callback 复用 scope-bound 行为；保留图片处理 job 回退与错误映射。
- [x] 2.3 在 `api.py` 删除重复任务实现，保留 route decorator、`list_tasks`/`get_task`/`cancel_task`/`retry_task` 以及 `_activity_payload`/`_read_agent_activity`/`_task_summary` 兼容入口。

## 3. 契约测试与验证

- [x] 3.1 增加 route snapshot、module dependency、service scope、summary redaction、activity/resume、cancel/retry 和 legacy import 测试。
- [x] 3.2 运行任务/API/activity/public DTO/scope/security 相关测试与 compileall，按失败修复。
- [x] 3.3 更新本 change tasks 与开源验证记录，明确公共核心改动尚未同步 Server。

## 4. 最终验证与同步

- [x] 4.1 运行 OpenSpec strict validate、`git diff --check`、完整 pytest 和 PostgreSQL marker（无连接串时显式 skip）。
- [x] 4.2 进行对抗性复核：检查 route order、scope 回退、payload/path 脱敏、reader 异常、retry 错误映射和现有 active change 脏路径；修复所有 P1/P2 后重跑验证。
- [x] 4.3 在开源仓库提交精确实现 SHA 和验证记录 SHA，核验祖先关系；用户审核授权后才允许 Server 精确 fetch/普通 merge，并记录 Server merge SHA、变更范围和测试结果。实现 commit 为 `6924e8d1ea48262faa01ab0f2c734aec17181b97`（父提交 `77a3d0946479564abaec2202c8b15418412ed335`）；完整测试 `380 passed, 92 skipped`，PostgreSQL marker 门禁未选择测试（无 marker/连接串），compileall、OpenSpec strict validate 和 `git diff --check` 通过，Server 尚未同步。
