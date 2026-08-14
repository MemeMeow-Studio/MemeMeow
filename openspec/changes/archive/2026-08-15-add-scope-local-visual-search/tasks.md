## 1. 确定首发模型与运行基线

- [x] 1.1 在目标 CPU 容器上测量 DINOv2 ViT-B/14 的加载时间、峰值 RSS、单图 p50/p95、吞吐和真实 Meme Recall@K（加载约 7.0 秒，容器常驻约 593 MiB，p50 146.27 ms，p95 165.12 ms，吞吐 6.787 图/秒；重复图片 smoke Recall@1/3/5 均为 1.0）
- [x] 1.2 使用官方公开 checkpoint 完成 DINOv2 ViT-B/14 的来源、SHA-256 审核，固定 `dinov2_vitb14`、768 维输出和预处理版本
- [x] 1.3 固定 CPU FP32 正确性基线、线程、单实例并发、内存和图片输入限制；评估 BF16/量化是否仅作为后续优化

## 2. 建立视觉向量持久化

- [x] 2.1 新增 `meme_visual_embeddings` SQLAlchemy 模型和 Alembic 迁移，建立 scope/Meme 外键、图片 SHA、模型身份和向量字段
- [x] 2.2 实现视觉向量写入前的维度、finite、非零范数和 L2 normalize 校验
- [x] 2.3 实现 scope-bound 视觉向量 repository，支持按当前模型读取、幂等 upsert、SHA 校验和精确 cosine 查询
- [x] 2.4 为 Agent context provenance 增加可验证的 task_id、图片 SHA、模型、Skill hash 和完成时间摘要
- [x] 2.5 为删除、重命名、图片内容变化和 storage operation 编写向量失效及外键行为测试

## 3. 部署 CPU 视觉推理服务

- [x] 3.1 实现固定模型加载、静态图片 RGB 预处理、GIF 首帧处理和单图 embedding 内部接口
- [x] 3.2 实现模型未配置、权重不可读、图片解码失败、维度错误和无效向量的稳定错误
- [x] 3.3 新增视觉服务 Dockerfile/Compose 服务、只读权重挂载、CPU quota、线程配置和健康检查
- [x] 3.4 验证视觉服务不持有数据库凭据、不暴露宿主端口，且只有主后端能调用 embedding 接口

## 4. 接入上传与持久任务链

- [x] 4.1 注册 `visual_embedding_generation` handler，定义包含 scope、Meme、SHA、模型和预处理身份的可序列化 payload 与 dedupe key
- [x] 4.2 修改单图和批量上传，在图片事务成功后创建或复用视觉任务并返回视觉任务标识
- [x] 4.3 实现视觉任务事务：事务外调用推理服务，事务内复核当前 SHA、写入向量并创建或复用 Agent 任务
- [x] 4.4 确保视觉任务失败不创建 Agent 任务，后续任务提交失败时视觉任务不伪装成功
- [x] 4.5 实现单图链的阶段重试规则，避免视觉重试重复 Agent、Agent 重试重复视觉和模型升级自动级联
- [x] 4.6 将视觉任务和动态创建的 Agent 任务接入现有批次 finalizer，批量 Agent 终态后只提交一次文本 cache generation
- [x] 4.7 为既有图片增加显式视觉向量回填入口，并提供单项视觉/Agent/文本阶段重试操作

## 5. 实现 scope 受控的视觉匹配接口

- [x] 5.1 新增 `POST /internal/visual-search/match`，从运行中的 `meme_context_generation` task_id 推导 scope、查询 Meme 和视觉模型
- [x] 5.2 实现查询向量就绪校验、候选 Agent-ready/provenance/SHA/storage 过滤、默认排除自身和有界 top_k
- [x] 5.3 实现稳定排序、受控图片媒体引用、结构化 context JSON 和 `query_embedding_not_ready` 等稳定错误
- [x] 5.4 覆盖跨 scope、非运行任务、未研究候选、旧 SHA、模型不一致和重复请求的 API 测试

## 6. 接入 research meme Skill

- [x] 6.1 新增只输出统一 JSON 的视觉匹配薄 CLI，读取内部 URL 和 Agent task ID，不读取数据库、模型或任意 scope
- [x] 6.2 更新 Skill 文档和 Agent 配置，说明先读候选 JSON、再按需查看图片，并明确视觉相似度不是出处证据
- [x] 6.3 为 CLI 成功、稳定业务错误、HTTP 错误和缺少运行时环境变量编写测试
- [x] 6.4 在 Docker Agent Runtime 中验证 Skill 挂载、内部服务连通性和 `/images` 只读路径契约

## 7. 配置、状态和可观察性

- [x] 7.1 增加服务端视觉模型、权重、维度、预处理和 CPU 参数配置，禁止前端或 Agent 覆盖
- [x] 7.2 扩展配置状态接口，仅返回视觉可用性和非敏感模型元数据，不泄露权重路径、凭据或数据库连接
- [x] 7.3 扩展任务列表、详情和错误映射，使视觉任务和阶段失败可轮询、可诊断、可显式重试
- [x] 7.4 更新部署文档和环境变量示例，记录权重挂载、许可证、模型身份和回滚步骤

## 8. 集成验证

- [x] 8.1 使用真实 scope 数据验证上传后视觉任务、Agent 任务和文本索引的单图链及批量并发交错
- [x] 8.2 验证视觉服务重启、模型不可用、图片 SHA 变化、重复完成路径和显式阶段重试不会产生重复下游任务
- [x] 8.3 验证候选只来自同 scope 且 Agent-ready 的图片，并检查结果稳定排序、context JSON 和媒体引用
- [x] 8.4 运行完整后端、数据库、Skill CLI、Docker Compose 和回归测试，记录 CPU 延迟、内存及 Recall@K；完整测试与运行验证结果见交付记录
