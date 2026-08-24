## Context

当前 `api.py:392-403` 定义存储预检摘要，`api.py:1893-1933` 定义 `/config` handler。
该 handler 还直接调用 `api.py` 内部的 `_request_scope` 和 `_service`，以选择当前请求的
scope、search cache 与 reverse-image service。`health` 同样复用存储摘要，因此摘要必须
随实现迁移并由 `api.py` 兼容导入。

## Goals / Non-Goals

**Goals:**

- 把 `/config` 状态投影和 storage preflight summary 移到不依赖 `api.py` 的公共核心模块。
- 通过显式 callback 注入复用入口已有的 scope/service 解析，不复制或削弱 scope fail-closed
  逻辑，也保留轻量 local 测试夹具的既有 fallback。
- 保持原 `GET /config` route metadata、响应字段、脱敏边界和 `api.config_status` import。
- 让 `api.py` 只保留薄兼容 wrapper，并保留 `_storage_preflight_summary`/常量 aliases。

**Non-Goals:**

- 不迁移通用 `_request_scope`、`_service`、`health`、search 或 lifespan。
- 不改变 Settings 模型、数据库/schema、scope middleware、runtime probe 算法或前端。
- 不增加新的 HTTP path、status、response key 或第三方依赖。

## Decisions

### 1. 新模块只拥有投影逻辑，scope/service 由 callback 注入

`backend.config_http.config_status()` 接受关键字 callback `request_scope` 与 `service`，
由 `api.py` wrapper 传入原有 helper。这样模块可以独立测试和复用投影逻辑，却不会反向
import `api.py`，也不会重新实现 scope 校验、local fallback 或 service 选择。

### 2. 存储摘要与 `/config` 共置并保留入口 aliases

`STORAGE_PREFLIGHT_BLOCKING_KEYS` 与 `_storage_preflight_summary` 迁移到新模块；`api.py`
显式 re-export 二者，让 `health` 和旧 Python 调用方继续读取同一对象。摘要只返回固定
状态与各类计数，不复制报告中的文件名、路径或诊断原文。

### 3. 路由 decorator 保留在 api.py 原位置

本 slice 只提取 handler implementation，不改变 FastAPI `APIRoute` 生成和模板路由顺序。
`api.config_status` wrapper 保持旧 handler 名称和签名，现有模块级 app、`create_app()` 和
Server merge 后的公共 local route 继续共享同一注册位置。

## Dependency / Rollback

依赖方向为：

```text
backend.config_http -> backend.scope / FastAPI
api.py -> backend.config_http
```

回滚时恢复 `api.py` 原 `_storage_preflight_summary` 与 `config_status` 实现，删除新模块、
测试和本 change artifacts；不涉及公共核心之外的 commit。

## Review Checklist

- 新模块源文本/AST 不包含 `import api` 或 `import server_api`。
- `/config` 仍为单个公开 GET route，路由位置、tags 和 handler 名称不变。
- scope 缺失仍由入口 callback fail-closed；无 services 时沿用原 app-state fallback。
- 输出不包含 key、凭据、宿主绝对路径、完整 runtime diagnostic 或 storage 文件名。
