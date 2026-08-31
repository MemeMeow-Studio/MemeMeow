## 设计概览

视觉匹配是 Agent 输入准备的一部分，不是 Agent 的外部工具。任务 Worker 在 claim 后、grant commit 和 `external_started` 之前读取当前任务绑定的 Meme 与视觉向量，调用现有 scope-bound `VisualEmbeddingRepository.match`，并把结果规范化成版本化 snapshot。匹配和 snapshot 持久化必须受同一 claim generation 保护；任何异常都在外部执行窗口之前收束为普通稳定失败。

## Snapshot 契约

Task 保存 `visual_match_snapshot` JSONB，内容使用固定 `protocol_version=2`：

```json
{
  "protocol_version": 2,
  "query": {
    "meme_id": "...",
    "image_sha256": "...",
    "model": "dinov2_vitb14",
    "dimensions": 768,
    "preprocess_version": "..."
  },
  "matched_at": "2026-08-31T00:00:00+00:00",
  "snapshot_sha256": "...",
  "candidates": [
    {
      "rank": 1,
      "meme_id": "...",
      "image_sha256": "...",
      "size_bytes": 123,
      "score": 0.86,
      "relative_path": "candidate-01.png",
      "context": {}
    }
  ]
}
```

`context` 在匹配事务中深拷贝，之后的 Meme 修改不能改变已保存 snapshot。哈希对去除哈希字段后的 canonical JSON 计算，候选按 score 降序、Meme UUID 升序排列，数量固定由服务端配置限制。空候选是合法成功状态。

Task 的公共投影只返回 `protocol_version`、`snapshot_sha256`、`matched_at` 和 `candidate_count`；候选 context 只可通过受控 workspace manifest 交给 Agent。attempt 保存同一 hash、版本和候选数量，恢复校验 hash 后复用。

## 执行顺序与 fencing

1. Worker claim 并校验任务 scope、目标 Meme 和当前图片 SHA。
2. 若任务已经有相同输入的合法 snapshot，校验并复用；否则执行一次后端匹配并生成 snapshot。
3. 在 claim fenced 的短事务中写入 Task snapshot 和 attempt 摘要。
4. 仅当 snapshot 准备成功后提交 Agent grant，标记 `external_started`，再调用 OpenCode。

查询向量未就绪使用 `query_embedding_not_ready`；Task/Meme/SHA 或模型身份不匹配使用稳定错误；候选图片无法按 SHA/size 安全物化使用 `visual_candidate_materialization_failed`。这些错误不得进入 `unknown_execution`，也不启动外部进程。旧任务没有 snapshot 时只允许在 claim 阶段按当前 protocol 生成一次；旧 callback 路径不作为回退。

## Workspace 与权限

`ResolvedWorkspace` 增加 `candidate_root`。它与 `task_scratch_root` 同级，由宿主 provider 为 task 创建，候选文件和 manifest 由服务端原子生成并设置为 Agent 只读。OpenCode `external_directory` 只允许读取 candidate root，`edit` 永远拒绝 candidate root；`images_root` 仍承载当前目标图片。候选 manifest 使用 task-relative 文件名，绝不写绝对路径、storage key、URL 或 scope ID。

Server 物化属于适配层：按 snapshot 中的候选 Meme ID、SHA 和 size 从同 scope BlobStore 复制到 candidate root，逐级拒绝符号链接并在复制后重新校验身份。resume 只重新物化同一 snapshot，若任一内容指纹改变则失败，不重新匹配。runtime quota 计入 candidate root，进程确认 reaped 后清理。

## 兼容与迁移

snapshot 字段均 nullable，旧任务和旧 attempt 仍可被读取。新 Agent 任务必须使用 protocol v2；旧 queued/running 任务在第一次 claim 时生成 snapshot，不能再走 callback。已完成任务不重跑。新 callback capability 只保留 reverse-image operation，视觉 URL 和视觉 callback token 不再注入 Agent 环境，直至旧任务排空。
