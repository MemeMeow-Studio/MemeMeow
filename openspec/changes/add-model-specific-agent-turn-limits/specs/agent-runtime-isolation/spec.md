## MODIFIED Requirements

提醒插件加载及启动后就绪检查仅适用于冻结为启用分析用量策略的任务。公共默认配置不加载插件；关闭不改变现有容器隔离要求。启用后，插件只读与任务间隔离要求全部生效。就绪状态必须绑定当前 attempt，不能复用旧 attempt 的状态；允许发现插件失败前已经发出模型请求，文件存在不能替代成功加载和初始化的确认。

### Requirement: Agent 容器必须具有明确的宿主机访问边界

系统 MUST 仅向 Agent 容器提供所需的目录挂载：当前任务 scratch、结果目录和任务专属 OpenCode 数据库可写，当前输入图片、项目 Skill、分析程度提醒插件源码、插件运行时依赖及可选候选目录只读。启用分析策略时 MUST 强制经过 bubblewrap，并使用独立 PID namespace 和 procfs；Executor token、其他任务 runtime、其他 workspace、其他图片及宿主进程信息 MUST 不可见。提醒插件和依赖 MUST 从与任务可写配置目录分离的受保护位置加载；任务中的 Bash、Python 或 Node 不能修改它们，也不能通过修改共享配置让后续任务加载不同的插件代码。插件可以通过当前 OpenCode 进程提供的受控接口读取本任务 session 的累计用量。容器 MUST 不获得项目根目录、用户目录、业务数据库凭据或 Docker socket 的访问能力。

#### Scenario: Agent 读取输入图片和受保护插件
- **WHEN** Agent 执行图片研究并加载分析程度提醒插件
- **THEN** 它可以读取当前任务输入、Skill 和插件，但不能写入插件源码、插件依赖或未挂载的宿主机路径

#### Scenario: Agent 读取输入图片和 Skill
- **WHEN** Agent 执行图片研究
- **THEN** 它可以读取被挂载的图片和 Skill，但不能读取未挂载的宿主机路径

#### Scenario: 一个任务不能影响后续插件
- **WHEN** 某个任务通过 Bash、Python 或 Node 尝试修改提醒插件、共享依赖或插件配置
- **THEN** 写入被文件权限或挂载边界拒绝，后续任务仍加载镜像中经过验证的同一插件版本

#### Scenario: 插件只读取当前任务用量
- **WHEN** 提醒插件判断当前任务是否达到提醒金额限额
- **THEN** 插件只能通过当前 OpenCode 进程和 session 标识读取本任务所需的累计用量，不能读取其他任务内容或业务数据库凭据

#### Scenario: Agent 尝试访问宿主 Docker
- **WHEN** Agent 在容器内检查 Docker socket
- **THEN** 容器中不存在可用的宿主 Docker socket

#### Scenario: 启用策略的 Agent 尝试读取 Executor 或其他任务
- **WHEN** Agent 读取 Executor token、宿主进程环境、其他任务目录或其他图片
- **THEN** bubblewrap 挂载和 PID namespace 拒绝访问，同时当前任务输入、Skill、scratch 和结果目录保持可用
