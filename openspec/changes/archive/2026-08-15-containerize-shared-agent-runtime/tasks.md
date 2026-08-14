## 1. Agent 镜像与共享容器

- [x] 1.1 新增 Agent Dockerfile，安装 OpenCode、Node、Python、file、ImageMagick、Tesseract 中英文语言包、curl、jq、常用文本工具和 Skill 脚本依赖
- [x] 1.2 在 Compose 中新增长期运行的共享 Agent 服务，以非 root 用户运行并保持网络访问
- [x] 1.3 配置 `data/opencode` 读写挂载，以及 `data/images` 和 `skills/research-meme-context` 只读挂载，确认未挂载项目根目录、`.env`、数据库凭据和 Docker socket
- [x] 1.4 新增 Agent 容器 build/start/check/stop 运维命令，并验证重复启动幂等

## 2. 宿主与容器执行边界

- [x] 2.1 新增兼容当前 OpenCode CLI 调用的 `docker exec` 包装器，并仅传入模型 Base URL、API Key 和必要运行变量
- [x] 2.2 实现宿主图片路径到 `/images/<相对路径>` 的安全映射，拒绝图片根目录之外的路径
- [x] 2.3 调整 runtime 初始化，使 workspace Skill 和 Node 依赖链接到容器内固定路径，不依赖宿主绝对软链接
- [x] 2.4 实现共享容器、挂载目录和容器内 OpenCode/常用工具探针，并接入应用启动和配置状态
- [x] 2.5 确保超时、服务停止和任务取消会终止对应容器内 OpenCode 进程，不停止共享容器或其他 session

## 3. 任务结果文件协议

- [x] 3.1 为每个语境任务创建 `/runtime/task-results/<task_id>/` 独立目录和确定的临时结果路径
- [x] 3.2 更新 Agent prompt，要求先在同目录生成草稿、校验 JSON，再原子重命名到指定临时结果路径
- [x] 3.3 后端从有限大小的结果文件读取候选，执行 JSON、JSON Schema 和业务字段校验，不再解析最后 assistant 文本作为业务结果
- [x] 3.4 为结果文件缺失、不可读、超限、JSON 无效和 schema 无效定义稳定错误码，并持久化到任务与 Meme provenance
- [x] 3.5 定义并实现任务产物保留/清理策略，确保并发和重试不会覆盖其他任务结果

## 4. 自动验证

- [x] 4.1 添加路径映射、包装器参数、环境变量白名单和运行时探针单元测试
- [x] 4.2 添加结果文件成功、缺失、截断、超限、schema 错误及并发任务目录隔离测试
- [x] 4.3 添加共享容器集成测试，验证两个任务使用同一容器、不同 session 且输出互不冲突
- [x] 4.4 添加安全边界测试，证明 Agent 可读取图片与 Skill、可使用网络和 OCR，但无法访问未挂载宿主路径、宿主 `.env` 或 Docker socket
- [x] 4.5 运行后端全量测试、PostgreSQL 集成测试、静态编译、`git diff --check` 和 OpenSpec strict validation

## 5. 真实链路验收与文档

- [x] 5.1 使用真实图片验证上传、容器内视觉/OCR/网络研究、结果文件校验、PostgreSQL 语境写入、1024 维向量生成和语义搜索
- [x] 5.2 更新安装与启动文档，说明 Agent 镜像构建、共享容器生命周期、挂载边界、预装工具和故障诊断
- [x] 5.3 验证现有 `data/opencode` session、缓存和日志在迁移后仍可用，并记录回滚步骤
