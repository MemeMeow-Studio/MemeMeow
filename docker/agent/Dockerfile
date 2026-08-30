FROM node:22-slim

WORKDIR /opt/mememeow

# Agent 镜像只包含运行时工具和项目 Skill，不复制项目源码、配置或数据库凭据。
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl file jq \
    imagemagick ffmpeg \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
    python3 python3-pip python3-venv python3-dotenv \
    git unzip zip procps findutils grep sed gawk \
    && rm -rf /var/lib/apt/lists/*

COPY .opencode/package*.json /opt/mememeow/
RUN npm ci --omit=dev
RUN npm install --global --no-fund --no-audit opencode-ai@1.18.18

COPY skills/research-meme-context /skills/research-meme-context
COPY executor /opt/mememeow/executor
# executor 与 Skill 共享无状态协议快照，不把项目后端或数据库带入 Agent 镜像。
COPY backend/public_dto.py /opt/mememeow/executor/public_dto.py
COPY backend/opencode_workspace.py /opt/mememeow/executor/opencode_workspace.py

RUN groupadd --system mememeow-agent \
    && useradd --system --gid mememeow-agent --create-home --shell /bin/bash mememeow-agent \
    && mkdir -p /runtime/home /runtime/task-results /runtime/workspace /runtime/logs /runtime/slots /images /run/agent-executor-secret \
    && chown -R mememeow-agent:mememeow-agent /runtime /images /run/agent-executor-secret \
    && chmod 700 /run/agent-executor-secret \
    && chmod -R a=rX /opt/mememeow/node_modules \
    && chmod -R a=rX /skills/research-meme-context

ENV HOME=/runtime/home \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/mememeow \
    PYTHONUNBUFFERED=1

USER mememeow-agent
WORKDIR /runtime/workspace

# executor 是容器唯一入口；后端通过 Compose 内部网络调用它，不需要 Docker CLI 或 socket。
CMD ["python3", "-m", "executor.server"]
