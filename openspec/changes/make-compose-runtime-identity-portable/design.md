## Context

见 `proposal.md`。当前 API 镜像没有 `USER` 指令，默认 root 写入 bind-mounted 图片；Agent 镜像固定以 UID/GID 1003 运行，且图片只读挂载。BlobStore 暂存文件显式为 `0600`，后续以硬链接移动，因而权限与所有者会被保留。现有 Compose 使用 named volume 保存 Agent runtime 和 executor token。

## Goals / Non-Goals

**Goals:**

- 消除 API root 写图与 Agent 非 root 读图之间的权限冲突。
- 让 bind mount 部署自动适配启动服务的宿主机数值身份。
- 使 API、Agent、图片目录与历史图片的权限规则可验证、可恢复且 fail-closed。
- 保持 Agent 图片挂载只读和长期共享 Agent 容器模型。

**Non-Goals:**

- 不实现每任务 Agent 容器、按任务图片挂载或新的 Agent 输入协议。
- 不改变视觉服务的运行身份、HTTP API、账户/scope 解析或任务编排。
- 不扫描或修改图片存储根以外的宿主机文件。

## Decisions

### 运行时身份由启动入口导出，而非在镜像构建时猜测

`start.sh` 在未显式设置时读取自身的 `id -u` 与 `id -g`，校验后导出 `MEMEMEOW_RUNTIME_UID/GID`。Compose 以必填插值使用该值运行 API 和 Agent；直接 Compose 部署必须在环境或 `.env` 中显式设置相同变量。

镜像仍声明一个固定的非 root 默认用户，作为脱离 Compose 直接运行时的安全兜底。Compose 的数值 `user` 覆盖仅服务于 bind mount 对宿主机 UID/GID 的语义。选择此方案而不固定 1003，是因为 bind mount 权限只认识数字身份，固定值不能跨宿主机可靠工作。

### 使用专用初始化服务，而不是让业务服务以 root 自修复

新增一次性 `runtime-init` Compose 服务，以 root 身份仅挂载图片根、Agent runtime volume 和 executor token volume。它验证 UID/GID，并以不跟随符号链接的目录遍历创建或归一化受控目录与文件：目录为 `0700`，普通图片与运行时文件为 `0600`，所有者为目标 UID/GID。遇到符号链接、特殊节点或 `st_nlink != 1` 的普通文件即失败。

API 和 Agent 都依赖该服务 `service_completed_successfully`，随后以目标 UID/GID 常驻。该做法把唯一必要的 root 操作缩小到可审计、无网络、无数据库、无 Docker socket 的短暂初始化过程；不采用业务服务启动时 `chown`，避免服务进程长期持有 root 权限。

### 动态 Agent 身份的可写状态统一放在初始化的 runtime volume

Agent 的 `HOME`、工作区、结果目录与 executor token 均位于初始化服务处理的挂载 volume 中。镜像内代码、Skill 和依赖保持只读；因此 Compose 覆盖到任意有效 UID/GID 时，不会依赖镜像内原 UID 的 home 目录所有权。

### BlobStore 负责保持权限，而初始化服务处理历史所有权

BlobStore 保留 `0600` 的独占暂存创建和同文件系统原子移动，确保新对象继承正确的运行身份和模式。配置与 BlobStore 启动时验证存储根不是符号链接且当前运行身份具有所需访问权限。初始化服务只改变受控存储内的所有权和权限，不读取或改写图片字节；历史对象迁移通过迁移前后摘要验证覆盖。

## Risks / Trade-offs

- [部署者以 root 执行启动入口] → 入口显式拒绝 root UID/GID，并说明应使用专用服务账户。
- [直接 Compose 部署漏配身份] → Compose 使用必填变量，使错误发生在服务创建前。
- [动态 UID 无法写入既有 named volume] → 初始化服务先调整 runtime 与 token volume 的受控目录所有权。
- [恶意链接诱导 root 初始化修改根外文件] → 不跟随符号链接，拒绝特殊节点和多链接文件，并将遍历限制为显式挂载根。
- [历史图片文件权限错误] → 仅在图片存储根内归一化；用集成测试在迁移前后比较 SHA-256 与大小。
- [多 scope Agent 输入路径尚未映射] → 保持当前行为；该问题由后续受控 Agent 输入提供者解决，不通过扩大挂载范围绕过。

## Migration Plan

1. 发布包含启动入口、镜像、初始化服务和存储权限检查的版本。
2. 停止现有服务，以非 root 服务账户运行 `start.sh start`；入口传递该账户的 UID/GID。
3. 初始化服务归一化图片存储和 named volume；部署验证已登记图片摘要不变。
4. 启动 API、视觉服务与 Agent，上传一张测试图片并以 Agent 身份读取，随后执行一次 Agent 任务。
5. 回滚时停止新服务并恢复代码版本；权限归一化不会改写图片字节，但旧版本若仍以 root 写入新图会重新产生不兼容文件，因此回滚仅用于紧急恢复，不应作为长期运行状态。
