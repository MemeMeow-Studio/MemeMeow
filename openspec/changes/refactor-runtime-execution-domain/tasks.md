## 1. 域级计划与兼容合同

- [x] 1.1 在公共核心创建域级 change，补齐 proposal/design/spec/tasks，并通过 OpenSpec strict validation。
- [x] 1.2 新增任务状态、attempt、scope/workspace 绑定和稳定失败码的无状态合同模块，保留旧 import/re-export。
- [x] 1.3 新增固定图片 stage plan 与有效性判断模块，令旧图片 Worker facade 只组合 repository、plan、task runner 和结果收束。

## 2. 公共核心运行时职责

- [x] 2.1 抽取 OpenCode session/workspace/attempt 合同与结果 store 边界，接入现有 OpenCodeRunner 并保持 host/executor 双模式协议。
- [x] 2.2 抽取 executor request/response、queue、process supervisor 和 result store 合同，保留 `executor.server` 兼容入口。
- [x] 2.3 为恢复、取消、超时、unknown execution、结果原子提交和跨 scope 绑定补充黑盒/竞态测试。
- [x] 2.4 运行公共核心受影响测试、完整后端测试、compileall、OpenSpec strict 和 diff check，修复全部 P1/P2。

## 3. 公共核心精确提交门禁

- [x] 3.1 检查公共工作区只包含本域目标，形成唯一域级 commit 并记录 SHA、祖先、范围和验证结果。
- [x] 3.2 向用户提供公共 commit 审核信息并在获得 Server 同步授权前停止远端/Server 合并。
