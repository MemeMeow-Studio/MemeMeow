## Context

反向图片检索由运行中 Agent 通过 multipart callback 调用。请求中的 `task_id`、上传整图、
可选 request id/digest 和检索参数只能在 token 声明的 scope 与持久任务目标上执行；物理
路径、scope、target SHA、claim generation 和 attempt 必须由服务端事实派生。

## Goals / Non-Goals

**Goals:**

- 将 callback handler 的安全编排移到不依赖 `api.py` 或 `server_api` 的公共模块。
- 通过显式 callback 注入可变应用依赖，保持旧 route/model/handler import 和执行顺序。
- 保持供应商无关结果、受控中心裁剪和 `ReverseImageError` 的 status/code 投影。

**Non-Goals:**

- 不迁移 `ReverseImageService`、缓存、供应商适配器、callback token middleware 或数据库 schema。
- 不改变 callback fact、usage event、任务状态机、Server adapter 或 frontend。
- 不允许客户端用表单字段选择 scope、路径或任务目标。

## Decisions

### 1. 表单声明留在入口

FastAPI 的 `Form`/`UploadFile` 声明和原 route decorator 留在 `api.py`，新模块接收已声明的
参数，避免 route metadata 和 OpenAPI 发生变化。

### 2. 安全事实通过 callback 注入

新模块接收 binding、registration、database、scope service 和 error provider；数据库环境由
token scope 打开，持久 task 与 Meme SHA 在任何业务 service 调用前复核。

### 3. 受控裁剪只使用已证明整图

上传内容先计算 SHA 并与目标 Meme 比较，只有匹配后才允许调用现有确定性中心裁剪函数。
客户端不能提交裁剪后的图片来替换任务目标。

## Risks / Trade-offs

- [目标校验被绕过] -> 测试缺失 binding、registration、task mismatch、旧 claim、跨 scope 和
  source SHA mismatch，且断言 reverse-image service 未被调用。
- [错误/请求绑定漂移] -> 测试 body/header request id 冲突、非法 digest、provider error 和
  受控裁剪参数转发。
- [入口反向依赖] -> AST 测试禁止新模块导入 `api`/`server_api`，旧 handler 名称保持可导入。

## Migration Plan

1. 在开源仓库新增模块、契约测试和 OpenSpec artifacts，保留 `api.py` 薄 wrapper。
2. 运行 reverse-image/callback/scope/API 安全回归、compileall、strict validate 与 diff check，
   提交精确实现及验证记录 SHA。
3. 按已批准的精确 SHA fetch 并普通 `--no-ff` merge 到 Server，再运行 Server 定向回归。
4. 回滚时恢复 `api.py` 原 handler，删除新模块、测试和本 change artifacts，不修改业务 schema。
