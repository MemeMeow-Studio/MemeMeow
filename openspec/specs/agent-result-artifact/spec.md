# agent-result-artifact Specification

## Purpose
将图片研究结果从脆弱的会话末尾文本改为任务专属、可校验的 JSON 文件交付，使工具调用阶段异常结束时不会把普通说明误判为结构化结果。
## Requirements
### Requirement: Agent 必须以任务专属临时文件交付研究结果
系统 MUST 为每个语境生成任务提供独立输出目录和预先确定的临时 JSON 文件路径。Agent MUST 先写入该临时文件；不得要求 Agent 将业务 JSON 作为最终会话文本交付。

#### Scenario: Agent 正常写入结果
- **WHEN** Agent 完成图片研究
- **THEN** 结果以该任务输出目录中的临时 JSON 文件存在，且不与其他任务共用路径

### Requirement: 后端必须校验后原子接收结果文件
系统 MUST 由后端读取临时文件并执行 JSON 解析、schema 校验和业务字段校验。只有全部校验通过，后端才可以原子接收文件内容并写入图片语境；Agent 不得直接写入数据库。

#### Scenario: 临时文件包含有效结果
- **WHEN** Agent 写入符合输出 schema 的 JSON
- **THEN** 后端接受结果并将任务标记为成功

#### Scenario: 临时文件不是有效 JSON
- **WHEN** Agent 写入截断或无效 JSON
- **THEN** 后端拒绝该结果，图片语境不被部分更新，任务以稳定错误标识失败

### Requirement: 缺失交付文件必须可诊断
系统 MUST 在 Agent 会话结束但没有可校验结果文件时，以 `agent_result_file_missing` 或等价稳定错误标识结束任务，不得把工具调用说明文本解析为研究结果。

#### Scenario: Agent 在工具调用阶段结束
- **WHEN** Agent session 没有产出任务结果文件
- **THEN** 任务失败状态明确表示结果文件缺失，且不会报告 `agent_output_invalid_json`
