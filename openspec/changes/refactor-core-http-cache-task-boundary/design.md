## Context

当前 `api.py:2031-2040` 定义 `generate_cache`，先读取当前 scope 的 search service，
服务缺失时返回 `503/service_unavailable`，随后向 task service 提交固定类型
`cache_generation` 的空 payload，并以 `202` 返回任务 id、类型和状态。

## Goals / Non-Goals

**Goals:**

- 把缓存任务 HTTP 编排移到不依赖 `api.py` 的公共模块。
- 通过 `service(request, name)` 和 `error(status, code, message)` callback 复用入口的
  scope-bound service 与错误格式。
- 保持 route metadata、status code、任务 payload/type 和服务 readiness 顺序。

**Non-Goals:**

- 不移动 TaskRepository、worker、cache generation 算法或任务查询/取消/重试路由。
- 不改变 operation policy、scope middleware、数据库事实、schema、前端或 Server adapter。

## Decisions

### 1. 只抽取 HTTP orchestration

新模块只检查 `service(request, "search")` 是否存在，再调用
`service(request, "tasks").submit("cache_generation", {})`；所有 scope、幂等和持久化语义
仍由既有 service 实现负责。

### 2. 保留入口 decorator 与兼容名称

`api.py` 继续在原位置声明 `@app.post("/generate-cache", status_code=202, tags=["tasks"])`，
并保留 `generate_cache` wrapper；新模块不注册自己的 route，避免改变模板和宿主 route 顺序。

## Dependency / Rollback

依赖方向为：

```text
backend.cache_task_http -> FastAPI
api.py -> backend.cache_task_http
```

回滚时恢复 `api.py` 原 handler，删除新模块、测试和 change artifacts；不涉及 Server 或公共
任务领域实现。

## Review Checklist

- 新模块不导入 `api.py` 或 `server_api`。
- `/generate-cache` 仍只有一个 POST route、202 status 和 tasks tag，GET 继续 405。
- search service 缺失时不调用 tasks service，稳定返回 `service_unavailable`。
- 成功响应只投影 task_id、task_type、status，不返回 task payload 或内部对象。
