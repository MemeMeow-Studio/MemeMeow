## 1. 公共协议与持久化

- [x] 1.1 新增带中文 docstring 的视觉 snapshot canonicalization、版本、hash 和摘要辅助模块，覆盖输入校验、稳定排序、深拷贝和空候选。
- [x] 1.2 为 Task 和 image processing attempt 增加 nullable snapshot JSONB/摘要字段与向前 Alembic 迁移，保留旧任务读取兼容。
- [x] 1.3 在 TaskRepository 增加 claim fenced 的 snapshot 写入、摘要读取和 hash 校验，拒绝旧 claim/跨 scope/损坏 snapshot。
- [x] 1.4 增加视觉 snapshot 单元和 PostgreSQL 集成测试，覆盖稳定 hash、空结果、后续 context 修改、跨 scope 和 hash 不匹配。

## 2. 任务前置执行

- [x] 2.1 为 PostgresTaskService 增加 Agent 前置 hook；在 grant commit/`external_started` 前执行匹配、snapshot 持久化和 attempt 绑定。
- [x] 2.2 让 resume 复用已验证 snapshot，缺失/变化/模型不一致使用稳定错误，禁止回退 callback 或重新匹配。
- [x] 2.3 更新任务错误分类、重试和结果投影，使预计算失败不进入 unknown execution，并保证空候选仍启动 Agent。
- [x] 2.4 在 pipeline 与 standalone 图片处理 Worker 中接入前置 hook，补充顺序、claim fencing 和失败不创建 grant 的测试。

## 3. Workspace 与 Agent

- [x] 3.1 扩展 `TrustedWorkspaceContext`/`ResolvedWorkspace`/provider 契约以携带 `candidate_root` 和只读 manifest。
- [x] 3.2 更新 external/edit 权限、executor 目录检查和 quota/清理接口，覆盖候选只读、symlink、跨 task 和 resume 复用。
- [x] 3.3 更新 research Skill/Runner，删除视觉 callback URL/token 注入，改为读取固定 manifest，保留 reverse-image 独立 capability。
- [x] 3.4 扩展 task DTO 只暴露候选数量、状态和 hash，增加无候选、前置失败和旧任务迁移测试。

## 4. 迁移与验证

- [x] 4.1 为旧 queued/running 语境任务提供 claim 时的一次性 protocol v2 迁移；旧已完成任务不重跑。
- [x] 4.2 在开源仓库运行定向视觉、task、workspace、executor 和 Skill 测试、compileall、`git diff --check` 与 OpenSpec strict validate。
- [x] 4.3 记录公共 commit SHA、变更范围、验证结果，等待审核后再在 Server 通过精确 SHA 普通 merge。
