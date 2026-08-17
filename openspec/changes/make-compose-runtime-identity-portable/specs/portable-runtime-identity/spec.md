## Purpose

为使用 bind mount 或 Docker volume 的部署提供可移植的非 root 运行身份和文件权限契约，使 API、Agent 与受控图片存储在不同宿主机上都能以可预测方式工作。

## ADDED Requirements

### Requirement: 部署必须提供有效的非 root 运行身份
系统 MUST 使用同一数值 UID/GID 运行 API 与 Agent。启动入口在部署方未显式提供运行身份时 MUST 采用启动入口进程的 UID/GID；直接使用 Compose 的部署 MUST 显式提供运行身份。UID/GID 必须为正整数，且 UID 与 GID 均不得为 `0`；不满足条件时系统 MUST 在启动业务服务前失败，不得回退为 root 或固定宿主机身份。

#### Scenario: 服务用户通过启动入口部署
- **WHEN** 非 root 服务用户未设置运行身份变量并启动系统
- **THEN** API 与 Agent 使用该服务用户的数值 UID/GID 运行

#### Scenario: 直接 Compose 部署缺少身份
- **WHEN** 部署方直接启动 Compose 且未提供运行身份变量
- **THEN** Compose 配置校验失败，API 与 Agent 均不会启动

#### Scenario: 运行身份非法
- **WHEN** 部署方提供 root、非数字或非正数的 UID/GID
- **THEN** 初始化失败并报告不含敏感路径或凭据的部署配置错误

### Requirement: 受控存储必须在业务服务前完成权限初始化
系统 MUST 在 API 与 Agent 启动前，以短生命周期初始化步骤准备图片存储、Agent runtime 和 executor token 所需的目录。初始化步骤 MUST 只处理其显式挂载的受控存储根，拒绝符号链接、特殊文件和不安全的硬链接；初始化失败时依赖服务 MUST 不启动。

#### Scenario: 全新部署初始化存储
- **WHEN** 部署方启动尚未初始化的受控存储
- **THEN** 初始化步骤创建业务所需目录，并使配置的运行身份可以读写其应有的存储

#### Scenario: 存储根中存在不安全节点
- **WHEN** 初始化步骤在受控存储根中发现符号链接、特殊文件或具有多个链接的普通文件
- **THEN** 初始化失败，且 API 与 Agent 不启动

### Requirement: 图片存储权限必须保持稳定且可恢复
系统 MUST 将受控图片普通文件保持为仅运行身份可读写，将图片根、scope 目录、暂存目录和隔离目录保持为仅运行身份可访问。上传、重命名、替换、删除恢复和历史权限迁移 MUST 不改变已登记图片的字节内容；迁移后 API 与只读挂载的 Agent MUST 能以运行身份读取图片。

#### Scenario: 新上传图片可由 Agent 读取
- **WHEN** API 以配置的运行身份成功上传一张图片
- **THEN** 图片以受控权限保存，Agent 可以通过其只读挂载读取该图片，且不能写入图片存储

#### Scenario: 修复历史 root 所有图片
- **WHEN** 受控图片存储中存在此前由 root 创建的普通图片
- **THEN** 初始化或迁移将其所有权和权限归一化为运行身份，图片字节摘要保持不变
