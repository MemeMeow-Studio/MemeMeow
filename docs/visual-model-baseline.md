# DINOv2 ViT-B/14 活动运行基线

当前活动视觉空间固定为官方模型标识 `dinov2_vitb14`，输出 768 维，
`preprocess_version=dinov2_vitb14-rgb224-first-frame-v1`。预处理固定为 RGB、GIF
第一帧、Resize 256、CenterCrop 224、ImageNet mean/std 归一化，并只取官方归一化
class token；不得把 patch token 或 register token 混入向量。

## 官方源码与 checkpoint

视觉服务使用 Meta 官方 DINOv2 仓库的固定源码提交：

```text
仓库：https://github.com/facebookresearch/dinov2
提交：7764ea0f912e53c92e82eb78a2a1631e92725fc8
模型：dinov2_vitb14
```

ViT-B/14 的官方 checkpoint 文件名和 canonical URL 为：

```text
dinov2_vitb14_pretrain.pth
https://dl.fbaipublicfiles.com/dinov2/dinov2_vitb14/dinov2_vitb14_pretrain.pth
```

当前部署审核记录：

```text
文件大小：346378731 bytes
SHA-256：0b8b82f85de91b424aded121c7e1dcc2b7bc6d0adeea651bf73a13307fad8c73
```

宿主机离线部署可以这样准备源码目录和权重：

```bash
mkdir -p data/models
git clone https://github.com/facebookresearch/dinov2.git data/dinov2
git -C data/dinov2 checkout 7764ea0f912e53c92e82eb78a2a1631e92725fc8
printf '%s\n' 7764ea0f912e53c92e82eb78a2a1631e92725fc8 > data/dinov2/.mememeow-dinov2-source-commit
curl -L --fail --output data/models/dinov2_vitb14_pretrain.pth \
  https://dl.fbaipublicfiles.com/dinov2/dinov2_vitb14/dinov2_vitb14_pretrain.pth
sha256sum data/models/dinov2_vitb14_pretrain.pth
```

`MEMEMEOW_VISUAL_MODEL_REPO` 指向源码目录，`MEMEMEOW_VISUAL_WEIGHTS_PATH` 指向只读
checkpoint 文件，`MEMEMEOW_VISUAL_WEIGHTS_SHA256` 应填写组织审核后的完整摘要。公开仓库
不保存权重之外的凭据，也不在服务启动时联网下载。DINOv2 源码使用 Apache 2.0 License，
部署方仍需按组织政策审核第三方依赖和分发范围：
<https://github.com/facebookresearch/dinov2/blob/main/LICENSE>。

## 模型选择与资源权衡

以下是官方 DINOv2 非 register 变体的部署比较。实际峰值 RSS、延迟和召回必须在目标 CPU
容器中实测，不能用表格估算代替验收。

| 模型 | 输出维度 | 参数量约 | FP32 权重约 | CPU 取舍 |
| --- | ---: | ---: | ---: | --- |
| ViT-S/14 | 384 | 21M | 84 MB | 资源较省，质量和容量较低 |
| ViT-B/14 | 768 | 86M | 346 MB | 当前默认，资源与质量折中 |
| ViT-L/14 | 1024 | 300M | 1.2 GB | 质量更高，CPU 延迟和 RSS 增加 |
| ViT-g/14 | 1536 | 1.1B | 4.4 GB | 资源成本最高，不适合默认 CPU |

活动选择 ViT-B/14 是因为它提供 768 维 class-token 表示，同时实际 checkpoint 约 346 MB，
单实例 FP32 CPU 内存和延迟处于可控范围。ViT-S/14 更省资源但表示容量较低；ViT-L/14
和 ViT-g/14 会显著抬高 CPU Docker 的启动时间、峰值 RSS 和排队成本。DINOv2 的其他
变体以及 DINOv3 H+/16 均不能仅靠环境变量启用，必须新增独立向量表迁移、权重 SHA 和
实测基线。

## 加载与错误边界

服务启动或首次请求时加载器会：

1. 校验源码目录、checkpoint 可读性和 SHA-256；
2. 从固定源码导入 `dinov2.hub.backbones.dinov2_vitb14`，以 `pretrained=False` 构造模型；
3. 使用 `torch.load(..., map_location="cpu", weights_only=True)` 读取官方 state dict，并以
   `strict=True` 加载；
4. 将模型切换为 FP32、`eval()`，再检查输出维度为 768。

缺少配置、源码或权重不可读、SHA 不匹配、checkpoint 格式错误、模型身份未迁移以及架构/维度
不匹配分别返回稳定错误 `visual_model_not_configured`、`visual_model_source_unreadable`、
`visual_weights_unreadable`、`visual_weights_checksum_mismatch`、
`visual_model_migration_required`、`visual_checkpoint_format_invalid` 或
`visual_model_architecture_mismatch`。加载过程不下载网络资源，也不会用随机模型伪造成功。

## 数据库迁移边界

`0009_dinov2_vitb14_visual_search` 将 0008 活动的 DINOv3 1280 维表改名为
`meme_visual_embeddings_dinov3_vith16plus`，并把 0007 已创建的 DINOv2 768 维表改回活动
表 `meme_visual_embeddings`。迁移不会删除旧向量或图片；两套表、模型身份和维度始终隔离，
匹配只使用当前 DINOv2 空间。`0009` 的显式降级会把活动表安全交换回 DINOv3 表，后续
切回必须同时更新完整模型配置并执行该迁移，不能只改维度造成隐式混用。

## 运行基线

活动服务固定 CPU FP32、单模型实例和单请求并发；线程由
`MEMEMEOW_VISUAL_CPU_THREADS` 与 `MEMEMEOW_VISUAL_CPU_INTEROP_THREADS` 控制，输入像素数
上限由 `MEMEMEOW_VISUAL_MAX_PIXELS` 控制。BF16、量化、batch 和 ANN 均留到取得真实数据
评测后再单独变更。部署验收至少记录加载时间、峰值 RSS、batch=1 的 p50/p95、吞吐和真实
Meme Recall@K。

本机 Docker CPU 验证记录（2026-08-14）：独立容器首次加载约 7.0 秒，模型加载后常驻内存
约 593 MiB；14 张真实图片重复两轮共 28 次请求的 p50 为 146.27 ms、p95 为 165.12 ms，
吞吐为 6.787 图/秒。样本中两组各 5 张的字节级重复 Meme 作为可验证 smoke ground truth，
10 个查询的 Recall@1、Recall@3 和 Recall@5 均为 1.0；该结果不替代更大规模的语义 Meme
标注评测。
