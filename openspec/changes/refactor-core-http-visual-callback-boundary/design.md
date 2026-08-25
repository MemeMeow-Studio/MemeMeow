## Context

内部视觉匹配由运行中 Agent callback 调用。请求体只包含 task_id、可选 request_id、top_k 和 exclude_self；scope、target SHA、claim generation、attempt 和 operation 由 callback binding 与持久 task/fact 推导。handler 必须先记录 started fact，再调用独立视觉 service，结束时记录 completed/failed fact。

## Goals / Non-Goals

**Goals:**

- 把视觉匹配 request model 和 handler 移到不依赖 `api.py` 的公共模块。
- 通过显式 callback 注入 callback binding、路由注册、数据库资源、scope service 和错误工厂。
- 保持幂等 completed fact 直接返回、未知/跨 scope/旧 claim fail-closed、错误 code/status 和 callback request input digest。

**Non-Goals:**

- 不迁移反向图片 callback、callback registry/token 生成、middleware body guard 或 VisualSearchService。
- 不修改 callback fact schema、数据库迁移、任务状态机、Server adapter 或前端。
- 不允许客户端提交 scope、target SHA、claim generation、attempt 或物理路径。

## Decisions

### 1. Request model 随 callback HTTP 边界迁移

`VisualMatchRequest` 迁入新模块，`api.py` 继续导入并暴露旧名称；`extra="forbid"` 保持客户端不能覆盖内部绑定事实。

### 2. 安全事实通过 callback 注入

新模块接收 binding/registration/database/scope service/error callback；数据库事务和 scope service 的装配仍由入口持有，模块只负责顺序与公开错误投影。

### 3. Fact 生命周期顺序不变

先用 binding 复核持久 task，再创建 callback request fact；已完成 fact 直接返回；未完成 fact 在提交 started 事务后调用 service，成功/失败分别 finish。fact request_id 缺失或非字符串时 fail-closed，兼容旧夹具可回退到已生成 canonical id。

## Risks / Trade-offs

- [绑定校验被跳过] -> 测试覆盖缺失 binding、task_id 不匹配、非法 request_id、跨 scope 和旧 claim。
- [幂等 fact 状态错误] -> 测试 completed 直接返回、started 后失败收束和成功收束。
- [入口反向依赖] -> AST 依赖测试禁止新模块导入 `api`/`server_api`，旧 import snapshot 保持。

## Migration Plan

1. 在开源仓库新增模块、OpenSpec artifacts 和 callback 契约测试，迁移 handler wrapper。
2. 运行 callback/visual/database/scope/security 回归、compileall、strict validate 和完整非外部门禁，提交精确 SHA。
3. 按用户已批准的本地精确 SHA fetch 并普通 `--no-ff` merge 到 Server，运行 Server 定向安全回归。
4. 回滚时恢复 `api.py` 原模型/handler，删除新模块、测试和 change artifacts；不修改 callback fact schema。
