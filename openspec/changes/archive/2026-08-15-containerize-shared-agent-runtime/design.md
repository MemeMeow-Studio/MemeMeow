## Context

现有 `OpenCodeRunner` 在宿主机启动 CLI，使用 `data/opencode/workspace` 作为共享运行时；该目录中的 Skill 和 Node 依赖当前通过指向项目目录的软链接提供。图片研究任务附加宿主机图片绝对路径，并从 session 最后一条 assistant 文本解析 JSON。参见 proposal.md 与本 change 的 delta specs。

## Goals / Non-Goals

**Goals:**

- 让所有 Agent session 在同一个长期运行容器中执行，保护宿主机文件与凭据边界。
- 保持 Agent 的 Bash、网络、OCR、图片处理与通用脚本能力。
- 让任务结果通过可恢复、可校验的文件协议交付。
- 维持当前 API、任务队列、并发 slot 和模型配置表面。

**Non-Goals:**

- 不对 session 做彼此隔离，不限制 Agent 的网络目的地，也不防止图片发送至外部服务。
- 不为 Agent 增加命令白名单、出网代理或数据库直连能力。
- 不迁移或重置现有 OpenCode session、缓存和日志。

## Decisions

### 一个常驻容器与 `docker exec`

Compose 定义一个 `mememeow-agent-runtime` 长期运行的容器，宿主 Worker 用一个兼容 OpenCode CLI 参数的包装器执行 `docker exec`。OpenCode session 数据库仍位于 `/runtime`，因此不同任务共享容器与缓存，但保持独立 session ID。相比每任务 `docker run`，该方式避免重复启动和失去 session/缓存；相比宿主执行，文件系统边界可由挂载实现。

### 挂载边界与容器路径

| 容器路径 | 宿主来源 | 模式 | 用途 |
| --- | --- | --- | --- |
| `/runtime` | `data/opencode` | 读写 | session DB、日志、缓存、任务输出 |
| `/images` | `data/images` | 只读 | 图片附件和本地分析 |
| `/skills/research-meme-context` | `skills/research-meme-context` | 只读 | 项目研究 Skill |
| `/opt/mememeow/node_modules` | 镜像构建产物 | 只读 | OpenCode provider 依赖 |

镜像预装 Node/OpenCode、Python、`file`、ImageMagick、Tesseract（含中文与英文语言包）、`curl`、`jq`、常用 Unix 文本工具与用于 Skill 脚本的 Python 依赖。容器以非 root 用户运行；根文件系统可保持可写，以符合保留 Agent 自由使用临时工具的约束。不得挂载 Docker socket、项目根目录、用户目录、`.env` 或数据库连接配置。

现有 runtime 中指向宿主 Skill 与 `node_modules` 的软链接会失效。运行器改为在 `/runtime/workspace` 中创建指向容器内 `/skills/...` 与 `/opt/mememeow/node_modules` 的链接，图片路径由 Worker 在进入容器前从宿主 `data/images` 映射为 `/images/<相对路径>`。

### 环境变量与服务生命周期

模型 Base URL 与 API Key 由包装器以 `docker exec --env` 传入；不向容器挂载 `.env`。`database.sh` 继续只管理 PostgreSQL；新增或扩展启动脚本单独 build/start/check/stop Agent 容器。应用启动和语境任务提交都探测容器、挂载及容器内 OpenCode，不可用时返回稳定运行时错误。停止 API 时应终止其 `docker exec` 客户端；常驻容器保持运行，容器停止由运维脚本负责。

### 任务专属 JSON 文件交付

Worker 用 `task_id` 在 `/runtime/task-results/<task_id>/` 创建输出目录，并将唯一临时路径传入 prompt，例如 `result.json.tmp`。Agent 可自由使用该目录作为工作空间，但最终必须写入该文件。后端在 OpenCode 进程结束后读取有限大小的该文件，执行 JSON 解码、JSON Schema 与 Pydantic 校验；只有通过后才在后端内存中接受候选并写入 PostgreSQL。读取时不再从 session assistant 文本提取业务 JSON。

为避免被半写文件读取，Agent 使用同目录草稿文件后 `rename` 为 `result.json.tmp`；后端只读取该确定路径。任务结束后保留有限诊断与结果文件以支持重试与诊断，可由现有 runtime 清理策略统一管理。文件不存在、不可读、超限、JSON 无效和 schema 无效分别映射稳定错误码。

### 测试策略

单元测试覆盖路径映射、`docker exec` 参数、挂载/环境白名单、结果文件缺失、截断 JSON、schema 错误和成功接收。集成测试以本地 Docker 容器运行 stub OpenCode 或实际 CLI，证明两次任务共享容器且任务输出不冲突。保留真实图片→Agent→向量→搜索 E2E 作为发布前手工验收。

## Risks / Trade-offs

- [共享容器被 Agent 污染会影响后续 session] → 可写状态只在 `/runtime`；为每个任务提供独立结果目录，并提供运维脚本清理或重建容器。
- [容器镜像漏装 Agent 需要的命令] → 镜像安装通用工具集，并加入启动探针与典型 OCR/JSON/网络命令测试。
- [宿主与容器路径混用导致图片或结果找不到] → 只在边界包装器中转换路径，并对越界路径拒绝执行。
- [`docker exec` 超时遗留容器内进程] → 将 exec 进程置于独立进程组，超时先终止 exec 再在容器内按 session/进程标识清理；加入集成测试。
- [传入模型 API Key 后 Agent 可读取它] → 这是用户明确接受的例外；不得同时传入其他宿主机凭据。

## Migration Plan

1. 构建 Agent 镜像并启动共享容器，挂载当前 `data/opencode`，保留其 session、缓存与日志。
2. 部署包装器、路径映射和结果文件协议，先通过容器探针与测试。
3. 停止宿主机直接启动的 OpenCode 任务，重启 API/Worker 以使用容器运行时。
4. 以一次真实图片任务验证 OCR、Agent JSON、向量生成与搜索。
5. 回滚时停止 Agent 容器并将运行配置切回原宿主 CLI；数据库和图片不受本 change 改动。
