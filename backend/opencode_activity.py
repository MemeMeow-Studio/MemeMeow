"""只读读取 OpenCode 会话活跃度的后端适配器。

该模块位于 FastAPI 任务摘要与共享 OpenCode runtime 之间，只依赖已验证的
SQLite 会话元数据，不把 OpenCode 内部表结构泄露到任务服务或 API 层。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection
from urllib.parse import quote


LOGGER = logging.getLogger(__name__)
SESSION_TITLE_PREFIX = "mememeow-task-"
DEFAULT_BUSY_TIMEOUT_MS = 250


@dataclass(frozen=True, slots=True)
class AgentActivity:
    """一条完整的 Agent 活跃度摘要。

    ``completed_turns`` 是已写入 ``step-finish`` 的步骤数量，
    ``turn_running`` 表示是否仍有未收束的步骤，``last_activity_at`` 是
    最近 part 更新时间的 UTC 文本。该值只在三项数据都可验证时交给 API。
    """

    completed_turns: int
    turn_running: bool
    last_activity_at: str

    def as_dict(self) -> dict[str, object]:
        """返回 API 摘要装配使用的公开字段，不包含 OpenCode 原始内容。"""
        return {
            "agent_completed_turns": self.completed_turns,
            "agent_turn_running": self.turn_running,
            "agent_last_activity_at": self.last_activity_at,
        }


class OpenCodeActivityReader:
    """从共享 runtime 中批量读取指定任务的 Agent 活跃度。

    实例只保存 runtime 根目录和数据库位置，不在初始化时创建文件或打开
    连接，因此数据库缺失不会阻止 FastAPI 启动。每次读取都在原目录以普通
    SQLite WAL 只读快照打开数据库，失败时返回空映射供 API 静默降级。
    """

    def __init__(self, runtime_root: Path | str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        """初始化适配器边界。

        参数 ``runtime_root`` 是 OpenCode 的共享运行目录；输出数据库路径
        固定为其下的 ``opencode.db``。``busy_timeout_ms`` 只接受短暂等待值，
        避免观测读取拖慢任务 API。
        """
        self.runtime_root = Path(runtime_root).expanduser()
        self.database_path = self.runtime_root / "opencode.db"
        self.busy_timeout_ms = max(1, min(int(busy_timeout_ms), 1000))

    def read_many(self, task_ids: Collection[str]) -> dict[str, AgentActivity]:
        """批量返回任务活跃度，任意数据库异常均转换为空映射。

        输入是任务 ID 集合，输出以任务 ID 为键；没有匹配 session、没有
        part，或单个 session 的 JSON/时间不完整时不会虚构零轮数据。查询只
        提取 ``part.data`` 的 ``type``，不读取推理文本、工具参数或正文。
        """
        unique_ids = tuple(dict.fromkeys(item for item in task_ids if isinstance(item, str) and item))
        if not unique_ids:
            return {}

        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            self._validate_schema(connection)
            rows = self._query_rows(connection, unique_ids)
            return self._build_results(rows)
        except (OSError, PermissionError, sqlite3.Error, TypeError, ValueError, OverflowError, json.JSONDecodeError):
            # 活跃度是可选观测，任何内部 schema、锁、JSON 或时间异常都不能
            # 传播到任务接口，更不能改变 Agent 的执行状态。
            LOGGER.debug("OpenCode 活跃度读取不可用", exc_info=True)
            return {}
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    def read(self, task_ids: Collection[str]) -> dict[str, AgentActivity]:
        """提供简短别名，供边界测试和替换 reader 的调用方使用。"""
        return self.read_many(task_ids)

    def _connect(self) -> sqlite3.Connection:
        """在 runtime 原目录创建 SQLite 普通只读连接。

        不复制主库文件，也不使用会忽略 WAL 的特殊连接标志；SQLite 会在
        同一目录内自动组合 ``opencode.db-wal`` 和 ``opencode.db-shm``。
        """
        if not self.database_path.is_file():
            raise FileNotFoundError(self.database_path)
        encoded_path = quote(str(self.database_path.resolve()), safe="/")
        connection = sqlite3.connect(
            f"file:{encoded_path}?mode=ro",
            uri=True,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        """验证当前 OpenCode schema 至少包含适配器所需的只读列。"""
        required = {
            "session": {"id", "title", "time_created"},
            "part": {"session_id", "time_updated", "data"},
        }
        for table, columns in required.items():
            names = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(names):
                raise ValueError(f"opencode_schema_incompatible:{table}")

    @staticmethod
    def _query_rows(connection: sqlite3.Connection, task_ids: tuple[str, ...]) -> list[tuple[object, ...]]:
        """以一条参数化批量查询取出最新 session 的事件类型和时间。"""
        titles = tuple(f"{SESSION_TITLE_PREFIX}{task_id}" for task_id in task_ids)
        placeholders = ",".join("?" for _ in titles)
        query = f"""
            WITH latest_session AS (
                SELECT id, title,
                       ROW_NUMBER() OVER (
                           PARTITION BY title
                           ORDER BY time_created DESC, id DESC
                       ) AS row_number
                FROM session
                WHERE title IN ({placeholders})
            )
            SELECT latest_session.title,
                   part.time_updated,
                   CASE
                       WHEN json_valid(part.data) = 1
                       THEN json_extract(part.data, '$.type')
                       ELSE NULL
                   END AS part_type,
                   json_valid(part.data) AS data_valid
            FROM latest_session
            JOIN part ON part.session_id = latest_session.id
            WHERE latest_session.row_number = 1
        """
        return [tuple(row) for row in connection.execute(query, titles)]

    @classmethod
    def _build_results(cls, rows: list[tuple[object, ...]]) -> dict[str, AgentActivity]:
        """将查询行按 session 聚合并丢弃不完整的 session。"""
        states: dict[str, dict[str, object]] = {}
        for title, raw_time, part_type, data_valid in rows:
            if not isinstance(title, str) or not title.startswith(SESSION_TITLE_PREFIX):
                continue
            task_id = title[len(SESSION_TITLE_PREFIX):]
            state = states.setdefault(
                task_id,
                {"starts": 0, "finishes": 0, "last_time": None, "invalid": False},
            )
            if data_valid != 1:
                state["invalid"] = True
                continue
            try:
                timestamp = cls._timestamp_ms(raw_time)
            except (TypeError, ValueError, OverflowError, OSError):
                # 单个 session 的时间损坏只隐藏该 session，不能污染同一批次
                # 中仍然可读的其他任务活动。
                state["invalid"] = True
                continue
            last_time = state["last_time"]
            if last_time is None or timestamp > last_time:
                state["last_time"] = timestamp
            if part_type == "step-start":
                state["starts"] = int(state["starts"]) + 1
            elif part_type == "step-finish":
                state["finishes"] = int(state["finishes"]) + 1

        results: dict[str, AgentActivity] = {}
        for task_id, state in states.items():
            if state["invalid"] or state["last_time"] is None:
                continue
            completed = int(state["finishes"])
            running = int(state["starts"]) > completed
            try:
                last_activity_at = cls._utc_iso(int(state["last_time"]))
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            results[task_id] = AgentActivity(completed_turns=completed, turn_running=running, last_activity_at=last_activity_at)
        return results

    @staticmethod
    def _timestamp_ms(value: object) -> int:
        """校验并规范 OpenCode 使用的毫秒 Unix 时间。"""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("opencode_time_invalid")
        if not math.isfinite(value):
            raise ValueError("opencode_time_invalid")
        return value

    @staticmethod
    def _utc_iso(timestamp_ms: int) -> str:
        """将毫秒时间转换为带 ``Z`` 后缀的 UTC ISO 8601 文本。"""
        moment = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        precision = "seconds" if moment.microsecond == 0 else "milliseconds"
        return moment.isoformat(timespec=precision).replace("+00:00", "Z")
