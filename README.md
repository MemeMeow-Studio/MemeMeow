<div align="center">

<pre align="center">
███╗   ███╗███████╗███╗   ███╗███████╗███╗   ███╗███████╗ ██████╗ ██╗    ██╗
████╗ ████║██╔════╝████╗ ████║██╔════╝████╗ ████║██╔════╝██╔═══██╗██║    ██║
██╔████╔██║█████╗  ██╔████╔██║█████╗  ██╔████╔██║█████╗  ██║   ██║██║ █╗ ██║
██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║╚██╔╝██║██╔══╝  ██║   ██║██║███╗██║
██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗██║ ╚═╝ ██║███████╗╚██████╔╝╚███╔███╔╝
╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝ ╚═════╝  ╚══╝╚══╝ 
</pre>

_✨ 通过自然语言检索表情包 ✨_

[在线体验](https://zvv.quest) · [反馈问题](https://github.com/MemeMeow-Studio/MemeMeow/issues) · [参与贡献](https://github.com/MemeMeow-Studio/MemeMeow/pulls)

[![License](https://img.shields.io/github/license/MemeMeow-Studio/MemeMeow)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org)
[![Online Demo](https://img.shields.io/website?url=https%3A%2F%2Fzvv.quest&up_message=online&down_message=offline&label=demo)](https://zvv.quest)

---

<p align="center">
    <a href="#-features">Features</a> •
    <a href="#-screenshots">Screenshots</a> •
    <a href="#-quick-start">Quick Start</a> •
    <a href="#-usage">Usage</a> •
    <a href="#-api">API</a> •
    <a href="#-related-applications">Related Applications</a>
</p>

</div>

<a id="-features"></a>
## ✨ Features

> [!CAUTION]
> 本项目返回表情包结果由AI生成，与本人观点无关。

- **自然语言处理**: 采用嵌入模型，实现 Q&A 式的检索，能够对给出问题自动使用表情包回应。
- **异步语境**: OpenCode Agent 使用研究 skill 为每张图片生成结构化数据库语境，任务状态可在“处理任务”页面查看。
- **本地视觉近邻**：上传先异步生成官方 DINOv2 ViT-B/14（768 维）视觉向量，再幂等启动 Agent；Agent 只能查询同一 scope 中已有 Agent-ready 语境的候选，视觉相似度不是出处证明。
- **受控并发**：Agent lane 使用 PostgreSQL 持久公平 claim；全局并发由 `MEMEMEOW_OPENCODE_CONCURRENCY` 控制，单个 scope 的同时运行数由 `MEMEMEOW_AGENT_SCOPE_CONCURRENCY` 控制（默认 `1`），超出并发上限的任务保持排队，等待队列达到 `MEMEMEOW_AGENT_BACKPRESSURE`（默认 `32`）后返回稳定背压错误；缓存和 metadata repair 保留独立资源。
- **后端设置**：独立“后端设置”页仅允许授权操作者调整 Agent 并发数量，保存到 `.env` 后重启生效；密钥、路径和 provider 地址不会回传浏览器。
- **便捷使用**: 提供 Vue Web 界面、统一 API 和受控媒体访问，可部署在本地单机环境。
- **可维护**：长任务、缓存和文件边界都有明确的 API 状态与错误契约。
- 另外，**单纯使用检索功能**，若使用API无需任何花费💰


Mememeow 是一个基于自然语言的表情包检索工具。它能让你通过描述想要的场景，快速找到合适的表情包。不再需要记住具体的文件名或标签，就能轻松找到想要的表情！

<a id="-screenshots"></a>
## 📸 Screenshots

当前界面由 Vue 3 提供，Compose 启动后由 API 提供 SPA，访问 `http://127.0.0.1:8275/`。迁移前的 Streamlit 截图仍保存在 [`legacy/streamlit-v1/screenshots/`](legacy/streamlit-v1/screenshots/)，仅用于历史参考。

## ℹ️ Data Source

本项目张维为表情包来源于 [知乎](https://www.zhihu.com/question/656505859/answer/55843704436)

> [!CAUTION]
> 若有侵权，请联系删除

<a id="-quick-start"></a>
## 🚀 Quick Start

### 环境要求

- Docker Engine 与 Compose v2 插件；若当前用户尚未刷新 Docker 组权限，`start.sh` 会临时通过 `sg docker` 执行
- `uv` 和 Python 3.12（仅在宿主机运行测试或开发工具时需要）
- Compose 运行 PostgreSQL 16 + pgvector、视觉服务、Agent executor 和 API；宿主机只有 API 通过 `127.0.0.1:8275` 访问
- 如启用本地视觉近邻，还需从 Meta 官方公开地址取得 DINOv2 ViT-B/14 `.pth` checkpoint，并提供固定源码提交和只读权重挂载；权重不内嵌公开镜像，缺失时视觉任务以 `visual_model_not_configured` 失败。
- 嵌入模型 API Key；语境任务由 OpenCode Agent executor 执行，需要在 Compose 环境中显式配置 `MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY` 和 `MEMEMEOW_OPENCODE_MODEL`

应用 scope、适配宿主 resolver、non-local Agent 输入、双 scope staging 和回滚约束见
[`docs/application-scope.md`](docs/application-scope.md)；单机开源入口仍固定使用显式
`LocalScopeResolver("local")`。

### 安装步骤

1. 克隆仓库
```bash
git clone https://github.com/MemeMeow-Studio/MemeMeow.git
cd MemeMeow
```

2. 创建 Compose 配置
```bash
uv run python -m scripts.sync_env
# 编辑 .env，填写需要的模型凭据和可选视觉权重配置
```

重复运行同步命令时，`.env.example` 继续提供字段顺序、分组注释和默认值；已有
`.env` 的同名字段值会被保留，模板中没有的本地字段会追加到文件末尾。

3. 启动 Compose 全栈
```bash
./start.sh start
```

`start.sh` 会先清理旧 tmux 会话和迁移前的宿主机端口进程，再执行
`docker compose --profile app up -d --build`。它等待 PostgreSQL、数据库迁移、Agent
executor、视觉服务和 API 的健康状态，并检查 `/`、`/health`、`/config`。重复执行是幂等的，
不会删除数据库、图片或 named volume。

`start.sh start` 默认把当前服务用户的 `id -u`/`id -g` 导出为
`MEMEMEOW_RUNTIME_UID`/`MEMEMEOW_RUNTIME_GID`，也可以显式设置这两个变量覆盖自动值。
两者必须是非 root 正整数；以 root 启动入口会在 Compose 创建服务前失败。直接运行
`docker compose --profile app up` 时必须在环境或 `.env` 中显式填写同一组身份，否则
Compose 插值校验失败。完整迁移、排障和回滚顺序见
[`docs/runtime-identity.md`](docs/runtime-identity.md)。

每次启动先运行一次性的 `runtime-init` 容器。它只挂载图片根、Agent runtime volume
和 executor token volume，拒绝符号链接、特殊节点和多链接普通文件；仅兼容留在同一
`node_modules` 树内、目标为普通文件的 npm `.bin` 相对链接，并把受控目录设为
`0700`、普通文件设为 `0600` 后归属目标 UID/GID。历史图片只改变所有权和权限，不改变
文件字节；初始化失败时 API 与 Agent 不会启动。回滚前应停止新版本并保留 volume 备份，
旧版本若再次以 root 写入新图片会重新产生权限冲突，不能把回滚当作长期部署方案。

常用运维命令：

```bash
./start.sh status
./start.sh logs                 # 可追加服务名或 -f
./start.sh logs mememeow -f
./start.sh stop
```

前端开发时可保留 Compose 后端运行，并单独启动带热更新的 Vite 开发服务器：

```bash
./start.sh --vite
```

开发服务器默认访问 `http://127.0.0.1:5275`，`/api` 和 `/media` 请求会代理到
`http://127.0.0.1:8275`。该命令在前台运行，按 `Ctrl-C` 停止；可通过
`MEMEMEOW_VITE_HOST` 和 `MEMEMEOW_VITE_PORT` 覆盖监听地址与端口。

API 只绑定 `127.0.0.1:8275`；Agent executor `8277` 和视觉服务 `8276` 只通过 Compose
内部网络访问，不发布到宿主机。API 和 Agent 容器都不挂载 Docker socket。数据库数据保存在
具名 Docker volume `mememeow_mememeow-postgres-data` 中，Agent runtime 和 executor token
分别保存在 `mememeow_mememeow-agent-runtime-data` 与
`mememeow_mememeow-agent-executor-secret` 中；`stop` 不删除这些 volume。

只需要数据库时仍可使用 `./database.sh start|stop|restart|status|logs|migrate`；它与
`start.sh` 共用 PostgreSQL volume，不负责 API、Agent 或视觉服务。

如需在浏览器中查看数据库，可启动本地 Adminer：

```bash
./db-viewer.sh start
```

然后访问 `http://127.0.0.1:8080`。连接时选择 `PostgreSQL`，服务器填写 `postgres`（容器内默认端口 5432），数据库、用户名和密码使用 `.env` 中的 `POSTGRES_*` 配置（默认均为 `mememeow`）。查看器只绑定 `127.0.0.1`，停止服务使用 `./db-viewer.sh stop`。可通过 `MEMEMEOW_DB_VIEWER_PORT=8081 ./db-viewer.sh start` 修改宿主端口。若 Docker Hub 不可达，启动脚本会自动尝试备用镜像；主镜像可在 `.env` 中通过 `MEMEMEOW_DB_VIEWER_IMAGE` 覆盖，备用镜像可在启动命令中通过 `MEMEMEOW_DB_VIEWER_FALLBACK_IMAGE` 覆盖或设为空禁用。

镜像位于 `docker/agent/Dockerfile`，预装 OpenCode、Node、Python、Bash、curl、jq、file、ImageMagick、ffmpeg、Tesseract 中英文 OCR 和常见文本工具。镜像默认用户本身是非 root；Compose 会以部署提供的 UID/GID 覆盖运行身份，入口是固定的 `executor.server` HTTP 服务。只读挂载 `data/images` 和 `skills/research-meme-context`，读写挂载 named volume `mememeow-agent-runtime-data:/runtime`，并在独立的 `mememeow-agent-executor-secret` named volume 中以 0600 权限持久化首次生成的随机 executor token。Agent 的 HOME、workspace 和任务结果都在初始化过的 runtime volume 中，不依赖镜像内固定用户的 home 所有权。API 以只读方式读取同一 token 文件，不会把 token 写入 checkout、`.env`、日志、结果文件或 OpenCode 子进程环境。容器不会挂载项目根目录、数据库凭据、用户目录或 Docker socket。后端只向 `http://mememeow-agent-runtime:8277` 发送带 token 的结构化任务；每个任务仍使用独立 OpenCode session 和 `task-results/<task_id>/` 结果目录。反向图片能力由后端内部接口统一代理，Agent 不持有 `SERPAPI_API_KEY`。callback 根 secret、密钥轮换和禁用式回滚按 [`docs/agent-callback-migration.md`](docs/agent-callback-migration.md) 执行。

`./scripts/agent-runtime.sh build|start|check|stop|restart|logs` 仍可单独运维 executor；`check` 同时验证 executor 健康接口、非 root、网络、OCR、JSON、挂载权限和 Docker socket 边界。应用服务依赖 executor 健康后才启动；全新 checkout 或干净宿主 shell 无需导出 token，executor 会在首次启动时原子生成并复用该 named volume 中的随机凭据。不要删除该 secret volume；若确需轮换，先停止 API 和 executor，再删除 volume 并重新启动 Compose。

Agent 运行模式仅支持 `auto`、`executor` 和 `host`。`auto` 只有在 executor 地址与非空 token 同时可用时才选择 executor，否则使用 host；显式 `executor` 缺少任一配置会失败关闭，不会回退到 host。旧的 Docker runtime、容器名和容器运行时字段不再受支持；本地回滚请明确设置 `MEMEMEOW_AGENT_RUNTIME_MODE=host`。人工诊断仍可在当前 Compose project 中运行 `docker compose exec mememeow-agent-runtime ...`，不需要也不应配置真实容器名。

OpenCode 的共享数据库、scope-aware workspace、opaque selector、签名 capability 和常规文件工具权限边界见 [`docs/opencode-workspaces.md`](docs/opencode-workspaces.md)。`external_directory` 不是操作系统沙箱；Bash、Python、Node 与网络研究能力仍需由另外的容器或 OS 策略约束。

4. （可选）准备视觉服务权重
```bash
mkdir -p data/models
curl -L --fail --output data/models/dinov2_vitb14_pretrain.pth \
  https://dl.fbaipublicfiles.com/dinov2/dinov2_vitb14/dinov2_vitb14_pretrain.pth
sha256sum data/models/dinov2_vitb14_pretrain.pth
```

视觉服务只暴露 Compose 内部 `8276` 端口，使用 `MEMEMEOW_VISUAL_WEIGHTS_DIR`、
`MEMEMEOW_VISUAL_WEIGHTS_SHA256` 和 CPU 线程变量配置。镜像内固定包含 DINOv2 官方源码
提交 `7764ea0f912e53c92e82eb78a2a1631e92725fc8`，默认读取
`dinov2_vitb14_pretrain.pth`；权重从 Meta 官方公开地址下载并按 SHA-256 校验。完整的
模型许可、加载路径、预处理、模型资源权衡和基线记录见
[`docs/visual-model-baseline.md`](docs/visual-model-baseline.md)。

视觉服务所在网络只与 Compose `mememeow` 主后端共享。Compose 会覆盖宿主 `.env` 中的
`127.0.0.1` 数据库和内部服务地址，Agent 回调使用 `http://mememeow:8275`，视觉状态使用
`http://mememeow-visual:8276/health`。权重缺失时容器仍可健康启动，但视觉任务会返回
`visual_model_not_configured`，不会使用随机模型伪造结果。

5. 安装共享 Agent skill（仅宿主机开发工具需要）
```bash
./scripts/install-agent-skills.sh
```

该脚本只创建指向 `skills/research-meme-context` 的相对符号链接，不会覆盖真实目录。容器镜像内固定使用 `/skills/research-meme-context` 和 `/opt/mememeow/node_modules`，不依赖宿主绝对软链接。配置 `.env` 中的 `MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY` 和 `MEMEMEOW_OPENCODE_MODEL=mememeow/gpt-5.6-luna` 后，运行器会在 `data/opencode/workspace/opencode.json` 写入无密钥配置。

宿主机单独运行测试时使用 `uv sync --dev`；应用运行不需要宿主机 Node.js、npm、Python
虚拟环境或 tmux，前端构建在 API 镜像中完成。

<a id="-usage"></a>
## 📖 Usage

### Basic Usage

1. 访问 Web 界面 `http://127.0.0.1:8275/`
2. 在搜索框中输入你想要的表情包场景描述
3. 点击搜索，系统会返回最匹配的表情包

### 图片管理

#### 上传新图片

1. 进入“上传”页面
2. 选择图片文件，图片会直接进入图片库
3. 可选：启用“处理完成后按标题自动命名”；图片上传立即返回，Agent 完成并成功写入数据库语境后才会异步重命名图片

> [!NOTE]
> 上传成功后会自动创建或复用逐图处理任务，按“视觉向量 → Agent 语境 → 可选自动重命名 → 文本 embedding”
> 异步推进；通常不需要每次上传后手动重建缓存。只有切换 embedding 模型、迁移存量图片或
> 进行显式全量维护时，才需要按处理任务中的提示执行回填或 v4 缓存生成。

图片库中的“选择图片”可勾选指定的 `pending` 或 `repair_required` 图片并点击“重试选中”；“完整重试所有未就绪”会先确认联网策略和是否自动命名，再由服务端枚举当前 scope 的全部核心未就绪图片，不受当前筛选或分页影响。每行同时显示元数据处理状态、文本 embedding 索引状态和图片视觉向量状态，顶栏显示当前文本 embedding 来源状态。

#### 处理任务

1. 打开“处理任务”查看上传后的逐图处理、显式缓存生成和 metadata repair
2. 使用状态和类型筛选定位任务
3. 失败的阶段可在详情侧栏按规则重试；模型切换或迁移维护时再按需重新生成 v4 缓存

#### 检查 OpenCode 会话

```bash
./scripts/open-opencode.sh
```

该入口复用图片语境任务的 runtime、数据库、skill 与无密钥配置。在 Compose executor 模式下，
入口会直接进入正在运行的 Agent 服务，并读取 named volume 中的历史 session；服务未运行时不会
静默回退到宿主旧数据库。仅在明确设置 `MEMEMEOW_AGENT_RUNTIME_MODE=host` 时使用宿主 runtime；
生产 API 仍不提供任意 OpenCode 命令转发。

### 回滚与诊断

停机或诊断优先使用 `./start.sh stop|status|logs`；不要使用 `docker compose down -v`，否则会
删除持久数据。named volume、图片和 PostgreSQL 数据不会被删除；从旧 `data/opencode`
迁移到 `mememeow-agent-runtime-data` 前先停止 executor 并保留备份。任务结果文件按保留策略保存，便于排查
`agent_result_file_missing`、`agent_result_file_invalid_json`、`agent_result_file_schema_invalid` 等错误。

callback 强制校验、Runner 的任务级凭据、反向图片调用边界、密钥轮换、旧任务收束和禁用式
回滚见 [`docs/agent-callback-migration.md`](docs/agent-callback-migration.md)。



### 合集资源包

合集详情提供动态 ZIP 下载和复制下载链接；链接只由合集稳定 ID 构造，每次请求现场读取当前成员，不保存分享快照、token 或权限记录。合集页也支持上传 `mememeow-collection` v1 ZIP，在预检通过后创建新合集并逐项导入图片。

资源包根目录包含 `manifest.json` 与 `images/`，manifest 记录合集名称、成员顺序、来源 ID、文件名、扩展名、大小和 SHA-256。导入最多 500 张图片、总解压大小 512 MiB，拒绝路径穿越、符号链接、重复条目、损坏内容和不可解码图片；同名同 SHA 复用，冲突文件名使用 SHA 前缀。资源包不包含 embedding、语境任务、scope、内部路径或权限信息。预检失败完全不写入业务数据，预检通过后的存储或任务错误按逐项部分成功返回。

<a id="-api"></a>
## 🔌 API

本项目开放 API 接口，规范检索入口为 `POST /search`：

### Endpoint
`POST http://localhost:8275/search`

### 请求参数
| 参数名 | 类型 | 简介 | 是否必填 | 范围 |
|-----------|--------|-----------------------------------------------|----------|----------------------|
| `query`       | string | 要查询的内容（例如关键词或某个话题）  | ✅       | 非空 |
| `n_results`       | integer| 返回的图片数量 | 否 | 1 - 30 |
| `llm_enhance`       | boolean| 是否启用 LLM 查询增强 | 否 | 默认 false |

### 返回格式
返回格式为 JSON（媒体地址使用稳定 `meme_id`）：

```json
{"results":["/media/2f3a2a6d-93f6-4cd0-a4c8-1578c5b929b2"]}
```

缓存生成、metadata repair 和逐图处理都是长任务，接口立即返回 `202` 和 `task_id`；逐图处理入口同时返回 `processing_job_id`，使用 `GET /tasks`、`GET /tasks/{task_id}` 或 `GET /images/processing/{job_id}` 查询。图片处理固定按视觉、Agent、文本 embedding 阶段推进，三类图片 Task 由专用 Worker 独占；外部执行结果无法确认时显示 `unknown_execution`，不会自动重放。存量图片迁移和搜索来源切换见 [`docs/image-processing-migration.md`](docs/image-processing-migration.md)。图片、语境、向量和任务均由 PostgreSQL 保存；图片字节继续位于 `MEMEMEOW_IMAGE_ROOT`。业务资源使用稳定 `meme_id`，媒体 URL 统一为 `/media/{meme_id}`。本项目后端以 API 为主，不包含管理界面。


<a id="-related-applications"></a>
## 📦 Related Applications

Mememeow 相关应用:

| 应用 | 作者   | GitHub | 链接 |
| --- | --- | --- | --- |
| VVQuest网页端 |  | [VVQuest](https://github.com/DanielZhangyc/VVQuest) | [链接](https://zvv.quest) |
| VVQuest*iOS*捷径 | [TomSmith163](https://github.com/TomSmith163) |  | [链接](https://www.icloud.com/shortcuts/a7084c7ae29e4de5898ce7c8386705f3) |
| HakuBot().vv() 命令 | [apple_catwaii](https://github.com/Apple-QAQ) |  | [QQ](https://qm.qq.com/cgi-bin/qm/qr?k=GJSCe1_B98V4Ni6leVtKAjQrAtJW-VG5 ) |
| VVQuest油猴脚本 | [DanielZhangyc](https://github.com/DanielZhangyc) | [vvquest-tampermonkey-extension](https://github.com/DanielZhangyc/vvquest-tampermonkey-extension) | [greasyfork](https://greasyfork.org/zh-CN/scripts/528477-vvquest-vv%E8%A1%A8%E6%83%85%E5%8C%85%E5%8A%A9%E6%89%8B) |
| Yunzai-Bot 插件 | [TomyJan](https://github.com/TomyJan) | [TomyJan/Yunzai-TomyJan-Plugin](https://github.com/TomyJan/Yunzai-TomyJan-Plugin/) |  |


> [!TIP]
> 如果你想添加你的应用，请提交 [PR](https://github.com/MemeMeow-Studio/MemeMeow/pulls) 或 [Issue](https://github.com/MemeMeow-Studio/MemeMeow/issues)

## 📄 License

本项目采用 [MIT](LICENSE) 开源协议。

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=MemeMeow-Studio/MemeMeow&type=Date)](https://star-history.com/#MemeMeow-Studio/MemeMeow&Date)

---

## 旧版本归档

迁移前的 Streamlit 实现已移动到 [`legacy/streamlit-v1/`](legacy/streamlit-v1/)，仅供查阅，不参与构建、测试或部署。完整的 Git 基线由 `streamlit-v1` 标签保留。
