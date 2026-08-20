## MODIFIED Requirements

### Requirement: 后端必须校验后原子接收结果文件

系统 MUST 由后端读取临时文件并执行 JSON 解析、schema 校验、业务字段校验和公开数据边界校验。只有全部校验通过，后端才可以原子接收文件内容并写入图片语境；Agent 不得直接写入数据库。校验 MUST 拒绝未知顶层字段以及凭据、内部 URL、绝对路径、执行绑定或其他敏感信息；命中任一项时 MUST 整体拒绝结果。

#### Scenario: 临时文件包含有效结果
- **WHEN** Agent 写入符合输出 schema 且不包含敏感信息的 JSON
- **THEN** 后端接受结果并将任务标记为成功

#### Scenario: 临时文件包含敏感或未知字段
- **WHEN** Agent 写入带未知字段、内部路径、敏感 URL 或凭据的 JSON
- **THEN** 后端拒绝完整结果，图片语境不被部分更新，任务以稳定错误标识失败

#### Scenario: 临时文件不是有效 JSON
- **WHEN** Agent 写入截断或无效 JSON
- **THEN** 后端拒绝该结果，图片语境不被部分更新，任务以稳定错误标识失败
