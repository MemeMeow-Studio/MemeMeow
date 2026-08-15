# DINOv2 权重不复制进公开镜像，部署时由只读 volume 挂载到 /models。
FROM python:3.12-slim

WORKDIR /app

ARG DINOV2_SOURCE_COMMIT=7764ea0f912e53c92e82eb78a2a1631e92725fc8

# 只复制官方源码，不复制权重；源码提交固定以保证 checkpoint 架构不漂移。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && mkdir -p /opt/dinov2 \
    && curl --fail --silent --show-error --location --retry 3 \
        "https://github.com/facebookresearch/dinov2/archive/${DINOV2_SOURCE_COMMIT}.tar.gz" \
        | tar -xzf - --strip-components=1 -C /opt/dinov2 \
    && printf '%s\n' "${DINOV2_SOURCE_COMMIT}" > /opt/dinov2/.mememeow-dinov2-source-commit \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch==2.8.0+cpu torchvision==0.23.0+cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY visual_service.py ./visual_service.py

ENV PYTHONUNBUFFERED=1 \
    MEMEMEOW_VISUAL_MODEL_REPO=/opt/dinov2 \
    MEMEMEOW_VISUAL_WEIGHTS_PATH=/models/dinov2_vitb14_pretrain.pth \
    MEMEMEOW_VISUAL_MODEL=dinov2_vitb14 \
    MEMEMEOW_VISUAL_DIMENSIONS=768 \
    MEMEMEOW_VISUAL_PREPROCESS_VERSION=dinov2_vitb14-rgb224-first-frame-v1 \
    MEMEMEOW_VISUAL_CPU_THREADS=4 \
    MEMEMEOW_VISUAL_CPU_INTEROP_THREADS=1

EXPOSE 8276
CMD ["uvicorn", "visual_service:app", "--host", "0.0.0.0", "--port", "8276"]
