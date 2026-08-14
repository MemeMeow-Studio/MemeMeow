## Context

当前 PostgreSQL 是 Meme、结构化语境、任务和文本向量的运行时事实来源；文本检索使用固定 1024 维、按 generation 激活的索引。上传成功后会直接提交 Agent 语境任务，Agent 可通过任务限定的内部接口使用外部反向图片能力，但不能利用当前 scope 中已经研究完成的相似 Meme。

本变更涉及上传链路、持久任务、PostgreSQL 向量数据、Agent Skill 和 Docker 部署。具体动机见 [proposal.md](proposal.md)，可观察行为见本 change 的 delta specs。活动模型固定使用公开 DINOv2 ViT-B/14（`dinov2_vitb14`，约 86M 参数、768 维输出），因此视觉索引不复用当前文本索引的 1024 维。

## Goals / Non-Goals

**Goals:**

- 让每张新图片先产生可重试、可版本校验的视觉向量，再自动进入 Agent 研究。
- 让不同图片的视觉和 Agent 阶段并发交错，不建立全批次阶段屏障；DINOv2 推理本身默认单实例限并发。
- 让 Agent 只通过当前运行任务检索同 scope、已经成功研究的视觉近邻及其 JSON。
- 让视觉推理、向量持久化、匹配和 Agent 调用保持单一职责与最小权限。
- 让失败和显式重试停留在当前阶段，避免模型升级或任务重放造成全链路级联。

**Non-Goals:**

- 不把视觉检索合并到现有自然语言 `/search`，也不改变文本 embedding 模型和 1024 维索引。
- 不支持未入库二进制图片的即时向量生成或匹配；查询图必须是当前任务关联的 Meme。
- 不实现跨 scope、共享合集授权或全局视觉搜索。
- 不在第一版引入 ANN、FAISS、Chroma、多模型融合、分数校准或视觉模型训练。
- 不为视觉成功但 Agent 漏交增加后台补交扫描；一致性由正常事务边界和显式重试承担。
- 不在第一版对 GIF 做多帧采样或向量聚合。

## Decisions

### 1. 视觉推理使用独立 CPU 容器，任务和数据库仍由主后端负责

新增内部视觉推理服务，只负责固定预处理、模型常驻加载和单图向量计算。服务不持有 PostgreSQL 凭据，不读取 Agent 任务，也不提供给 Agent 的调用地址；主后端的 `visual_embedding_generation` 处理器读取 scope-bound 图片、调用推理服务、校验返回向量并写入数据库。

模型代码与 CPU 推理依赖放在独立镜像，权重通过只读 `/models` volume 提供并在加载时校验。DINOv2 ViT-B/14 约 86M 参数，官方 FP32 checkpoint 约 346 MB，运行时仍需要模型副本、激活和 Python 开销；因此服务至少暴露内部健康检查和受控 embedding 入口，Compose 不向宿主机发布推理端口，部署文档必须记录实测峰值 RSS。线程数、CPU quota、请求大小和单服务并发由服务端配置，第一版以单进程、单请求模型实例避免重复占用内存。

加载器固定使用 Meta 官方 DINOv2 仓库提交 `7764ea0f912e53c92e82eb78a2a1631e92725fc8`，
从 `dinov2.hub.backbones.dinov2_vitb14(pretrained=False)` 构造 ViT-B/14，再以
`torch.load(..., weights_only=True)` 严格读取官方
`dinov2_vitb14_pretrain.pth` state dict；不再要求部署方导出 TorchScript，也不允许服务
联网下载权重。公开 checkpoint URL、DINOv2 License 和部署审核步骤记录在
`docs/visual-model-baseline.md`。

CPU 推理先以 FP32 作为正确性基线；只有目标 CPU 对 BF16/其他低精度路径有稳定支持且 Recall@K 无明显回退时，才将其作为可选优化，不把量化或低精度作为首发前置条件。

备选方案是把 PyTorch 和模型加载进 FastAPI 主容器。它减少一个服务，但会显著扩大主镜像，使 Web Worker 数量与模型副本数耦合，并让 CPU 推理影响普通 API 生命周期，因此不采用。让视觉容器直接消费数据库任务则需要向新容器授予数据库和 BlobStore 权限，也不符合最小权限。

### 2. 使用独立视觉向量表，不复用文本 generation

新增 `meme_visual_embeddings`，概念字段为：

```text
scope_id
meme_id
model
dimensions
preprocess_version
image_sha256
embedding
created_at
updated_at
primary key(scope_id, meme_id, model, preprocess_version)
foreign key(scope_id, meme_id) -> memes(scope_id, id) on delete cascade
```

表中只有成功产物；`pending/running/failed` 由 `tasks` 保存，避免两套状态机。写入前验证向量维度、所有元素 finite、范数非零，并统一做 L2 normalize。查询始终同时限定 `scope_id`、`model`、`dimensions` 和 `preprocess_version`，再执行精确 cosine 排序。

活动模型固定为 ViT-B/14，因此 Alembic 迁移把当前列声明为 `vector(768)`，并保留
0008 创建的 `vector(1280)` DINOv3 表供显式回切。未来采用不同维度模型时必须通过新的迁移建立独立列或表，不能修改现有列并混合历史向量。第一版不建 HNSW；个人图片库先使用精确查询，只有实际规模和 p95 延迟证明需要时再增加按模型隔离的 ANN 索引。

备选方案是复用 `meme_embeddings`。该表现有约束要求 1024 维文本、`semantic_document` 和文本 metadata hash；强行复用会让视觉模型选择受数据库偶然形状支配，并混淆两种索引生命周期，因此拒绝。

### 3. 单图任务链由一个事务衔接，不建立全批次阶段屏障

上传事务提交 Meme 后创建或复用：

```text
visual_embedding_generation
dedupe = visual:{meme_id}:{image_sha256}:{model}:{preprocess_version}
```

视觉处理器在数据库事务外调用推理服务。取得向量后开启短事务，锁定当前 Meme 并复核 SHA，在同一事务中：

1. upsert 当前模型的视觉向量；
2. 检查当前 SHA 是否已有有效 Agent provenance；
3. 若没有，则创建或复用对应的 `meme_context_generation` 任务；
4. 关联上传批次信息并提交事务。

只有该事务成功，视觉任务才报告成功。这样进程在事务前崩溃不会留下产物，事务后崩溃时向量与 queued Agent 任务已经同时持久化，现有任务启动恢复可以继续认领，无需增加专用补交扫描。若后续任务无法创建，事务回滚并让视觉任务进入可重试失败。

不同图片没有相互依赖：图片 A 的视觉任务成功后可以立即启动 Agent A，不等待同批图片 B。视觉任务重复完成时先检查有效 Agent provenance 和同输入 Agent 任务，避免活动任务去重只覆盖 `queued/running` 而在已成功后重复研究。

自动链路的 Agent 输入指纹继续包含 `meme_id`、图片 SHA、Agent 模型、Skill hash 和反向图片策略。视觉模型升级只创建新视觉向量，不自动重跑已有 Agent；Agent 重试复用有效视觉向量；文本索引重试只处理文本 generation。图片 SHA 变化被视为新版本，不是旧任务重试。

### 4. 批量上传只在文本索引收束处使用批次 finalizer

视觉和 Agent 阶段按单图链并发。每个视觉任务继承上传批次 ID，成功创建的 Agent 任务加入对应的 Agent 批次；当该上传批次的视觉任务全部进入终态后，系统封口 Agent 批次，使其不再接受成员，但不等待该时刻才启动 Agent。

Agent 批次全部进入终态后，现有 finalizer 创建或复用一次 `cache_generation`。单图上传使用单项批次或在 Agent 成功后直接创建同一去重的文本索引任务。失败 Agent 不阻塞成功语境进入下一次文本索引，但自身不产生文本 embedding。

备选方案是每个 Agent 成功都立即完整重建文本 generation。它会放大批量上传成本并产生并发 generation，因此继续使用已有批次收束。全局“所有视觉完成后才启动所有 Agent”的阶段屏障会产生队头阻塞，也没有业务依赖，因此不采用。

### 5. 查询资格和候选资格使用不同条件

查询 Meme 只需具备当前视觉模型、维度、预处理和图片 SHA 对应的有效向量。候选 Meme 还必须满足：

- 与查询任务属于同一 `scope_id`；
- Meme 和 BlobStore 对象仍可访问，且不存在活动 storage operation；
- 候选视觉向量对应当前图片 SHA；
- `context_status="ready"`；
- provenance 中存在当前图片 SHA 对应的成功 research Agent 记录。

Agent 成功写回时在现有 provenance 中增加稳定的 `agent_context` 摘要：

```json
{
  "task_id": "...",
  "image_sha256": "...",
  "model": "...",
  "skill_hash": "...",
  "completed_at": "..."
}
```

人工修正已有 Agent JSON 时保留该摘要，因此“曾由 Agent 成功研究”不依赖当前顶层 `producer` 是否仍为 `research`。图片内容变化时摘要的 SHA 不再匹配，候选资格自然失效。

同一批次中较早完成 Agent 的图片可以成为稍后任务的候选；第一版接受这种最终一致性，不增加候选快照或排除当前批次。若真实评测发现错误传播，再以独立变更增加快照边界。

### 6. Agent 只调用 task-scoped 的内部匹配接口

主后端新增 `POST /internal/visual-search/match`。请求只包含当前 `task_id`、`top_k` 和可选的 `exclude_self`；接口只接受处于 `running` 的 `meme_context_generation`，并从任务记录取得 `scope_id`、查询 `meme_id` 和冻结的视觉模型身份。调用方不提交 scope、任意 Meme ID 或查询向量。

接口返回供应商无关 JSON：

```json
{
  "query_meme_id": "...",
  "model": "...",
  "preprocess_version": "...",
  "results": [
    {
      "rank": 1,
      "score": 0.86,
      "meme_id": "...",
      "image_path": "/images/example.webp",
      "context": {}
    }
  ]
}
```

结果限制为有界 `top_k`，按 cosine 相似度降序、`meme_id` 升序稳定排列。`score` 只表示同一向量空间内的排序值。查询向量不存在返回 `query_embedding_not_ready`；任务不合法、模型身份冲突和图片不可访问使用稳定错误，均不回退到即时推理。

Skill 新增薄 CLI，例如 `local_visual_match.py --top-k 20`。它读取 Runner 注入的内部 URL 和 `MEMEMEOW_AGENT_TASK_ID`，输出 JSON 到 stdout、稳定错误到 stderr，不读取数据库、权重、图片二进制或任意 scope。Runner 继续只向 Agent 暴露 `/images` 只读路径和内部能力地址。

### 7. Skill 把视觉近邻当作候选证据，不当作事实

更新 research workflow：完成首轮观察后可以查询本地近邻，先阅读返回 JSON，再只打开少量需要核验的图片。已有候选的 title、summary、visible text、references、meaning 和 uncertainties 可用于形成后续检索锚点，但视觉相似度不得单独证明身份、模板、出处或当前语用。

本地视觉匹配不替代 Google Lens。前者检索用户 scope 中已研究语料，后者在任务策略允许时发现外部候选；两者保持独立接口、错误和证据边界。

### 8. 第一版 GIF 只编码第一帧

推理服务使用 Pillow 解码并统一转为 RGB。静态图片按模型固定预处理；GIF 只读取第一帧，模型身份的 `preprocess_version` 必须包含该策略。损坏、空帧、尺寸超限或解码失败以稳定输入错误结束视觉任务。

多帧采样能够改善动画 Meme，但会增加解码、推理和聚合策略，并让向量身份更复杂。第一版以确定性和可测试性优先，后续通过真实 GIF 召回评测决定是否升级。

## Risks / Trade-offs

- [Risk] DINOv2 权重许可、下载或离线分发条件不满足部署要求。→ 代码镜像不内嵌权重，使用官方公开 URL 并记录权重 SHA；无法满足时停止实现，不偷偷替换模型。
- [Risk] DINOv2 ViT-B/14 CPU 推理速度或内存无法满足上传吞吐。→ 模型常驻、限制为单实例推理并测量加载时间、峰值 RSS、batch=1 延迟和真实 Meme Recall@K；上传本身不等待推理，必要时仅增加排队容量而不增加模型副本。
- [Risk] 视觉服务成功响应包含 NaN、零向量或错误维度。→ 后端在数据库写入前执行 finite、范数和维度校验，失败只影响当前视觉任务。
- [Risk] Agent-ready 候选传播已有错误语境。→ Skill 把近邻当作候选而非事实，保留 uncertainties 和视觉核验步骤；第一版不让未研究图片进入候选库。
- [Risk] 同批 Agent 完成顺序影响可见候选集合。→ 第一版接受最终一致性以避免快照系统；通过测试观察错误传播，必要时再排除当前批次。
- [Risk] 事务外推理期间图片被替换或删除。→ 写入事务重新锁定 Meme 并校验 SHA，旧结果不得写回。
- [Risk] DINOv2 权重或源码许可限制公开镜像分发。→ 代码镜像不内嵌权重，权重只读挂载；活动模型使用官方公开 URL 并记录 SHA。
- [Trade-off] 不做专用崩溃补交扫描会减少自愈路径。→ 向量写入和 Agent 任务创建置于同一事务，正常失败依赖现有任务重试；仍有异常时由用户显式重试，不增加后台协调器。
- [Trade-off] 精确 cosine 查询随图库规模线性增长，ViT-B/14 的 768 维向量仍会带来存储和排序成本。→ 当前个人图库优先简单正确；以实测查询规模决定是否增加按模型隔离的 ANN。

## Migration Plan

1. 核验公开 DINOv2 ViT-B/14 权重获取与许可、记录真实 SHA，固定 768 维输出、预处理版本和 CPU 资源基线；在目标机器上完成 FP32 正确性和性能 benchmark。
2. 通过 `0009_dinov2_vitb14_visual_search` 激活 768 维 DINOv2 表，并保留 0008 的 1280 维 DINOv3 表；迁移不删除向量或图片，显式降级可回切 DINOv3。
3. 部署只读权重挂载的 CPU 推理服务和健康检查，先保持上传链路未启用。
4. 增加后端视觉任务处理器、输入校验、事务内向量写入与 Agent 任务创建，并为现有图片提供显式批量回填入口。
5. 切换上传链路为视觉任务起点，更新任务列表、单项重试和批次文本索引收束。
6. 发布内部匹配接口、Skill 薄客户端和 research workflow，验证 Agent 容器没有数据库凭据、模型权重和推理服务地址。
7. 对现有图片分批生成视觉向量；已有 Agent-ready 图片在向量就绪后成为候选，不自动重跑其 Agent 语境。
8. 回滚时停止提交新视觉任务并恢复上传后直接提交 Agent 的旧路径；保留新表和已生成向量供再次前滚，旧文本搜索继续工作。
