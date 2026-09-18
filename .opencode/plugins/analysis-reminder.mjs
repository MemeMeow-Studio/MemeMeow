// OpenCode 主 session 分析用量提醒；进程终止由 Executor 独立负责。
import { writeFile, rename } from "node:fs/promises";
import { OpencodeClient } from "@opencode-ai/sdk/v2";

const runtimeVersion = "1.18.18";
const reminder = "请尽快完成必要工作、验证结果并生成报告。";

/** 将当前 attempt 状态原子写入专属文件，供 Executor 检查就绪和提醒结果。 */
async function publish(path, state) {
  const temporary = `${path}.${process.pid}.pending`;
  await writeFile(temporary, JSON.stringify(state), { mode: 0o600 });
  await rename(temporary, path);
}

/** 使用当前进程的 SDK transport 读取累计金额，每个 attempt 最多追加一次提醒。 */
export default async function analysisReminder({ client }, options) {
  const { attempt_id, policy, status_path, session_id } = options ?? {};
  if (!attempt_id || !status_path || !policy || policy.version !== 1) {
    throw new Error("analysis_plugin_configuration_invalid");
  }
  const state = {
    attempt_id, policy_version: policy.version, ready: false,
    session_id: session_id ?? null, reminder_sent: false, error: null,
  };
  // 固定版本的 v1 transport 保留 OpenCode 进程内 fetch，v2 SDK 提供累计金额投影。
  const sdk = new OpencodeClient({ client: client._client });
  try {
    const response = await sdk.global.health({ throwOnError: true });
    if (response.data.version !== runtimeVersion) {
      throw new Error("analysis_plugin_runtime_version_incompatible");
    }
    state.ready = true;
    await publish(status_path, state);
  } catch (error) {
    state.error = error.message === "analysis_plugin_runtime_version_incompatible"
      ? error.message : "analysis_plugin_initialization_failed";
    await publish(status_path, state);
    throw error;
  }

  let pending = Promise.resolve();
  return {
    "experimental.chat.system.transform": async (input, output) => {
      // 同一 attempt 的并发钩子串行处理检查和注入，避免重复提醒。
      pending = pending.then(async () => {
        if (state.reminder_sent) return;
        try {
          if (!input.sessionID) throw new Error("analysis_reminder_session_missing");
          if (state.session_id && input.sessionID !== state.session_id) return;
          const response = await sdk.v2.session.get(
            { sessionID: input.sessionID }, { throwOnError: true },
          );
          const session = response.data.data;
          if (session.parentID) return;
          if (session.id !== input.sessionID) throw new Error("analysis_reminder_session_mismatch");
          state.session_id = session.id;
          if (typeof session.cost !== "number" || !Number.isFinite(session.cost) || session.cost < 0) {
            throw new Error("analysis_reminder_cost_invalid");
          }
          if (session.cost >= Number(policy.reminder_cost)) {
            output.system.push(reminder);
            state.reminder_sent = true;
          }
          state.error = null;
        } catch (error) {
          state.error = error.message.startsWith("analysis_reminder_")
            ? error.message : "analysis_reminder_session_read_failed";
        }
        try {
          await publish(status_path, state);
        } catch (error) {
          // 仅记录文件系统错误类别；Executor 的金额检查不依赖提醒状态写入。
          console.error(JSON.stringify({
            event: "analysis_reminder_status_write_failed", attempt_id,
            reason: error.code ?? error.name,
          }));
        }
      });
      await pending;
    },
  };
}
