# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

当前主要用户是维护本地图片库的个人使用者。他们在整理、检索或分享表情包时，希望用自然语言描述意图，而不是记忆文件名或目录位置。

未来可能支持共享图片库和多人协作，但不作为当前阶段的用户或交付约束。

## Product Purpose

MemeMeow 让用户通过一句自然语言快速找到合适的本地表情包，并把结果作为图片分享出去。产品成功的标准是：用户能够从意图描述直接得到相关图片，并低成本完成选择和分享。

## Positioning

MemeMeow 面向本地图片库，把自然语言检索与表情包语境记录结合起来；用户检索的是图片在表达什么，而不只是文件名或人工标签。

## Operating Context

- 用户通过 Web 工作台上传、整理和浏览本地表情图片。
- 图片可以经过异步语境处理，生成与图片同目录关联的结构化 JSON。
- 检索使用图片语境生成的语义索引，任务状态需要在处理期间可查看。
- 检索结果需要能被用户直接打开、选择并分享。

## Capabilities and Constraints

- 支持使用自然语言检索本地表情包。
- 支持图片与 sidecar JSON 的关联存储，并以结构化语境支持检索。
- 支持异步处理、失败诊断和任务状态查看。
- 图片与语境记录当前以本地文件系统为主，不预设共享图片库的权限、同步和协作模型。
- 共享图片库、多人协作、部署拓扑和长期数据保留策略尚未确定，后续设计不得假定其具体方案。

## Brand Commitments

- 产品名称为 MemeMeow。

## Evidence on Hand

- 仓库中的 Vue 3 前端、FastAPI 后端和本地图片库实现。
- 图片 sidecar JSON schema 与研究 skill：`skills/research-meme-context/`。
