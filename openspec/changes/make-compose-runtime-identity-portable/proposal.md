## Why

当前 API 容器因未指定运行用户而默认以 root 写入图片；Agent 已以非 root 身份读取只读挂载，导致新上传的 `root:root 0600` 图片无法被 Agent 使用。固定 UID 也不能适配不同宿主机的部署用户，必须建立明确、可验证的运行时身份和存储权限契约。

## What Changes

- 新增由部署环境提供的 API 与 Agent 共享运行时 UID/GID 契约，并拒绝 root 或非法身份配置。
- 让启动入口自动采用启动服务用户的 UID/GID，同时要求绕过入口的 Compose 部署显式提供该配置。
- 增加短生命周期的存储初始化步骤，为图片 bind mount 和 Agent 必需 named volume 安全地准备所有权与权限。
- 让 API 镜像和 Agent 镜像均具有非 root 默认运行身份；正常 Compose 部署覆盖为配置的运行时身份。
- 固化图片存储目录与普通图片文件的权限，并为已有受控图片提供校验后的迁移路径。
- **BREAKING**：直接运行 Compose 而未设置运行时 UID/GID 将被拒绝，部署方必须使用 `start.sh` 或显式提供部署身份。

## Capabilities

### New Capabilities

- `portable-runtime-identity`: 定义可移植的非 root Compose 运行身份、受控存储初始化和历史图片权限迁移。

### Modified Capabilities

- `agent-runtime-isolation`: 要求共享 Agent 容器以显式非 root 身份运行，并只在初始化完成后接收任务。

## Impact

- 受影响代码：`Dockerfile`、`docker/agent/Dockerfile`、`docker-compose.yml`、`start.sh`、存储配置与 BlobStore。
- 受影响部署资源：图片 bind mount、Agent runtime volume、executor token volume 和部署文档。
- 不改变 HTTP API、任务编排或 Agent 的长期共享容器模型；任务级输入沙箱不在本变更范围内。
