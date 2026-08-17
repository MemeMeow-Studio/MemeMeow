# Compose 运行身份与存储权限

## 服务账户

生产部署应使用专用的非 root 服务账户执行 `./start.sh start`。入口在没有显式
`MEMEMEOW_RUNTIME_UID`/`MEMEMEOW_RUNTIME_GID` 时读取该账户的 `id -u` 和 `id -g`，
并把数值身份传给 API、Agent 和存储初始化服务。显式值必须都是正整数且不能为 `0`。
以 root 执行入口或提供非法值会在创建业务容器前失败。

绕过入口直接运行 Compose 时，必须在当前环境或 `.env` 中填写这两个变量，例如：

```bash
MEMEMEOW_RUNTIME_UID=1500 MEMEMEOW_RUNTIME_GID=1500 docker compose --profile app up -d --build
```

不要把当前宿主机用户名、密码、API key 或 executor token 写入身份变量。

## 初始化与迁移

`runtime-init` 是唯一需要 root 的短生命周期服务。它只接收显式挂载的图片根、Agent
runtime named volume 和 executor token named volume，不连接网络、数据库或 Docker
socket。初始化会拒绝符号链接、特殊节点和具有多个硬链接的普通文件，再把受控目录设为
`0700`、普通文件设为 `0600` 并归属目标 UID/GID。API 与 Agent 依赖它以成功退出后才启动。

初始化可以归一化旧版本 root 创建的图片和 sidecar；操作只改变所有权与 mode，不读写图片
内容，因此可在迁移前后比较 SHA-256 和文件大小。若发现危险节点，先移出该节点并保留
原始备份，再重新运行初始化；不要手工让业务容器以 root 运行来绕过失败。

## 故障排查与回滚

查看 `./start.sh logs runtime-init` 和 `./start.sh status`，重点确认初始化服务退出码、
API/Agent 的 UID 以及 `/runtime`、图片根和 token volume 的权限。不要使用
`docker compose down -v`，否则会删除持久 volume。

回滚时先停止 API、Agent 和初始化流程，保留图片与 named volume 备份，再恢复旧镜像。
权限迁移不会改变图片字节，但旧版本若重新以 root 上传图片会再次产生 Agent 无法读取的
`0600` 文件；回滚只适合紧急恢复，不应作为长期运行方式。executor token 仍只存在
named volume，轮换时按 [`agent-callback-migration.md`](agent-callback-migration.md) 的
停止、备份和重新启动顺序操作。
