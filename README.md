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
- **异步语境**: OpenCode Agent 使用研究 skill 为每张图片生成结构化 JSON，任务状态可在“处理任务”页面查看。
- **受控并发**：不同图片可在安全范围 `1..8` 内使用独立 Agent lane 并行处理，默认并发为 `1`；超出并发上限的任务保持排队，等待队列达到 `MEMEMEOW_AGENT_BACKPRESSURE`（默认 `32`）后返回稳定背压错误；缓存和 metadata repair 保留独立资源。
- **后端设置**：独立“后端设置”页仅允许授权操作者调整 Agent 并发数量，保存到 `.env` 后重启生效；密钥、路径和 provider 地址不会回传浏览器。
- **便捷使用**: 提供 Vue Web 界面、统一 API 和受控媒体访问，可部署在本地单机环境。
- **可维护**：长任务、缓存和文件边界都有明确的 API 状态与错误契约。
- 另外，**单纯使用检索功能**，若使用API无需任何花费💰


Mememeow 是一个基于自然语言的表情包检索工具。它能让你通过描述想要的场景，快速找到合适的表情包。不再需要记住具体的文件名或标签，就能轻松找到想要的表情！

<a id="-screenshots"></a>
## 📸 Screenshots

当前界面由 Vue 3 提供，启动后访问 `http://localhost:5275`。迁移前的 Streamlit 截图仍保存在 [`legacy/streamlit-v1/screenshots/`](legacy/streamlit-v1/screenshots/)，仅用于历史参考。

## ℹ️ Data Source

本项目张维为表情包来源于 [知乎](https://www.zhihu.com/question/656505859/answer/55843704436)

> [!CAUTION]
> 若有侵权，请联系删除

<a id="-quick-start"></a>
## 🚀 Quick Start

### 环境要求

- Python 3.12（使用 uv 管理）
- Node.js 22+
- 可选：嵌入模型 API Key；使用异步语境需要预先安装 OpenCode、skill 和共享 `node_modules`

### 安装步骤

1. 克隆仓库
```bash
git clone https://github.com/MemeMeow-Studio/MemeMeow.git
cd MemeMeow
```

2. 安装后端依赖
```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements-dev.txt
cp .env.example .env
```

3. 安装共享 Agent skill
```bash
./scripts/install-agent-skills.sh
```

该脚本只创建指向 `skills/research-meme-context` 的相对符号链接，供 Codex 和 OpenCode 发现同一份 skill；可重复执行，不会下载 Node.js 依赖或覆盖已有真实目录。部署前在共享目录一次性安装 `@ai-sdk/openai`（例如 `cd .opencode && npm install --save-exact @ai-sdk/openai@4.0.37`），任务执行期间不会运行包管理器。服务复用 `.opencode/node_modules` 中预装的 OpenCode 插件和 Responses provider，而不是前端 `node_modules`。配置 `.env` 中的 `MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY` 和 `MEMEMEOW_OPENCODE_MODEL=mememeow/gpt-5.6-luna` 后，服务会在 `data/opencode/workspace/opencode.json` 写入无密钥的通用 OpenCode 配置。

4. 启动 FastAPI
```bash
uvicorn api:app --reload --port 8275
```

5. 启动 Vue 开发服务器（另一个终端）
```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0
```

也可以使用项目根目录的 `./start.sh` 一键停止旧实例并在 tmux 中启动前后端。

<a id="-usage"></a>
## 📖 Usage

### Basic Usage

1. 访问 Web 界面（开发模式默认为 `http://localhost:5275`，后端默认为 `http://localhost:8275`）
2. 在搜索框中输入你想要的表情包场景描述
3. 点击搜索，系统会返回最匹配的表情包

### 图片管理

#### 上传新图片

1. 进入“上传”页面
2. 选择目标目录和图片文件
3. 可选：启用“处理完成后按标题自动命名”；图片上传立即返回，Agent 完成并成功写入 JSON 后才会异步重命名图片与 sidecar

> [!CAUTION]
> 每次上传后需要重新生成缓存。

图片库中的“选择图片”可勾选指定的 `pending` 或 `repair_required` 图片并点击“重试选中”；“重试所有未就绪”会批量提交所有尚未完成语境处理的图片。每行同时显示 JSON 处理状态和 embedding 索引状态，顶栏显示全局 embedding 缓存状态。

#### 处理任务

1. 打开“处理任务”查看上传后的语境生成、缓存生成和 metadata repair
2. 使用状态和类型筛选定位任务
3. 失败的语境任务可在详情侧栏重试；完成语境后按需重新生成 v4 缓存

#### 检查 OpenCode 会话

```bash
./scripts/open-opencode.sh
```

该入口复用图片语境任务的 runtime、数据库、skill 与无密钥配置。在 OpenCode 中输入 `/sessions` 查看历史会话并打开检查；也可运行 `./scripts/open-opencode.sh --list` 在终端列出会话，或将 OpenCode 参数直接传入，例如 `./scripts/open-opencode.sh --session <session-id>`。启动器会隔离项目根目录配置，避免混入其他 provider 或凭据。



### 资源包功能

本次重构的生产入口已移除本地/在线资源包导入、启停、社区同步和 ZIP 导出。资源包功能后续单独设计和实现；当前图片直接存放在 `.env` 配置的图片根目录中。

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
返回格式为 JSON：

```json
{"results":["/media/example.png"]}
```

缓存生成、metadata repair 和语境生成都是长任务，接口立即返回 `202` 和 `task_id`，使用 `GET /tasks` 或 `GET /tasks/{task_id}` 查询。每张图片的 JSON 与图片同目录，文件名为完整图片文件名追加 `.json`。


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
