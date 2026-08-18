"""持久化长任务服务。

本模块位于 API 生命周期与耗时业务处理之间。每个任务独立保存为 JSON，进程重启后
仍可查询终态，并通过任务类型处理器从可序列化 payload 重建待执行工作。
"""

from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4


TERMINAL = {"succeeded", "failed"}
STABLE_TASK_ERRORS = {
    "no_indexable_images",
    "opencode_not_configured",
    "agent_process_failed",
    "agent_timeout",
    "agent_event_invalid",
    "agent_export_failed",
    "opencode_slot_unavailable",
    "agent_output_invalid_json",
    "agent_output_schema_invalid",
    "agent_result_file_missing",
    "agent_result_file_unreadable",
    "agent_result_file_too_large",
    "agent_result_file_invalid_json",
    "agent_result_file_schema_invalid",
    "agent_runtime_unavailable",
    "agent_executor_not_configured",
    "agent_executor_unavailable",
    "agent_executor_unauthorized",
    "agent_executor_invalid_response",
    "agent_backpressure",
    "task_exists",
    "agent_timeout_limit_exceeded",
    "agent_image_root_mismatch",
    "agent_input_provider_unavailable",
    "agent_image_path_forbidden",
    "agent_result_path_invalid",
    "target_changed",
    "task_interrupted",
    "generation_policy_conflict",
    "reverse_image_forbidden",
    "reverse_image_unavailable",
    "invalid_task",
    "invalid_reverse_image_policy",
    "task_not_running",
    "invalid_image_format",
    "image_too_large",
    "invalid_search_type",
    "reverse_image_provider_unavailable",
    "reverse_image_provider_invalid",
    "cache_write_failed",
    "usage_request_conflict",
    "visual_model_not_configured",
    "visual_model_migration_required",
    "visual_model_identity_invalid",
    "visual_model_source_not_configured",
    "visual_model_source_unreadable",
    "visual_weights_unreadable",
    "visual_weights_checksum_mismatch",
    "visual_checkpoint_format_invalid",
    "visual_model_architecture_mismatch",
    "visual_model_runtime_unavailable",
    "visual_service_unavailable",
    "visual_service_http_error",
    "visual_service_invalid_response",
    "visual_model_identity_mismatch",
    "visual_image_decode_failed",
    "visual_inference_failed",
    "visual_embedding_invalid",
    "visual_embedding_dimensions_mismatch",
    "visual_embedding_non_finite",
    "visual_embedding_zero_norm",
    "visual_embedding_sha256_invalid",
    "visual_embedding_sha256_mismatch",
    "query_embedding_not_ready",
    "embedding_not_configured",
    "embedding_dimensions_mismatch",
    "embedding_non_finite",
    "embedding_zero_norm",
    "task_scope_invalid",
    "task_scope_mismatch",
    "task_scope_unavailable",
    "unknown_execution",
    "reverse_image_unknown_execution",
    "operation_forbidden",
    "operation_limit_exceeded",
    "operation_policy_unavailable",
    "operation_grant_invalid",
    "blocked",
}
# 这三类任务由逐图图片处理 Worker 独占扫描、认领和执行；通用任务 Worker
# 只能处理其它系统任务，避免旧调度器把图片链误判为缺少 handler。
IMAGE_PROCESSING_TASK_TYPES = frozenset(
    {
        "visual_embedding_generation",
        "meme_context_generation",
        "text_embedding_generation",
    }
)
TaskHandler = Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]


def now() -> str:
    """返回任务记录使用的 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskRecord:
    """一条可序列化任务记录，承载状态、图片来源事实和有限诊断。

    图片阶段任务的来源字段由数据库控制面填充；前端只能读取这些安全摘要，不能
    通过 ``payload`` 改写 Job 归属或执行阶段。
    """

    task_id: str
    task_type: str
    submission_mode: str | None = None
    image_stage: str | None = None
    processing_job_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    progress: float | None = 0.0
    message: str | None = None
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    completed_at: str | None = None
    attempts: int = 0
    error: dict[str, str] | None = None
    result: Any = None
    settings_version: str | None = None
    agent_concurrency: int | None = None
    slot_id: int | None = None
    # 数据库任务服务内部使用；公共 ``as_dict`` 不暴露 scope 身份。
    scope_id: str | None = None

    def as_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        """返回稳定 API 结构；列表调用方可排除内部 payload。"""
        result = {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "submission_mode": self.submission_mode,
            "image_stage": self.image_stage,
            "processing_job_id": self.processing_job_id,
            "job_id": self.processing_job_id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "attempts": self.attempts,
            "error": self.error,
            "result": self.result,
            "settings_version": self.settings_version,
            "agent_concurrency": self.agent_concurrency,
            "slot_id": self.slot_id,
        }
        if include_payload:
            result["payload"] = self.payload
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskRecord":
        """从磁盘 JSON 恢复并验证最小任务字段。"""
        task_id = value.get("task_id")
        task_type = value.get("task_type")
        payload = value.get("payload", {})
        if not isinstance(task_id, str) or not task_id or not isinstance(task_type, str) or not task_type or not isinstance(payload, dict):
            raise ValueError("task_record_invalid")
        return cls(
            task_id=task_id,
            task_type=task_type,
            submission_mode=value.get("submission_mode") if value.get("submission_mode") in {None, "pipeline", "standalone"} else None,
            image_stage=value.get("image_stage") if value.get("image_stage") in {None, "visual", "agent", "text_embedding"} else None,
            processing_job_id=(value.get("processing_job_id") or value.get("job_id")) if isinstance(value.get("processing_job_id") or value.get("job_id"), str) else None,
            payload=payload,
            status=str(value.get("status", "queued")),
            progress=value.get("progress"),
            message=value.get("message"),
            created_at=str(value.get("created_at") or now()),
            updated_at=str(value.get("updated_at") or value.get("created_at") or now()),
            completed_at=value.get("completed_at"),
            attempts=int(value.get("attempts", 0)),
            error=value.get("error") if isinstance(value.get("error"), dict) else None,
            result=value.get("result"),
            settings_version=value.get("settings_version"),
            agent_concurrency=value.get("agent_concurrency"),
            slot_id=value.get("slot_id"),
            scope_id=value.get("scope_id") if isinstance(value.get("scope_id"), str) else None,
        )


class PersistentTaskService:
    """统一的任务存储与执行器。

    `task_root` 保存任务 JSON；`handlers` 由应用在启动期注册，避免把 Python closure
    写进任务文件。所有更新在锁内持久化，保证并发读者只会看到完整记录。
    """

    def __init__(self, task_root: Path, max_workers: int = 2, *, agent_concurrency: int = 1, agent_backpressure: int = 32, settings_version: str | None = None):
        """创建持久任务服务，并为 Agent 任务建立独立执行 lane。"""
        self.task_root = task_root.expanduser()
        self.task_root.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, TaskRecord] = {}
        self._handlers: dict[str, TaskHandler] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mememeow-task")
        self.agent_concurrency = max(1, min(int(agent_concurrency), 8))
        self.agent_backpressure = max(1, min(int(agent_backpressure), 500))
        self.settings_version = settings_version
        self._agent_executor = ThreadPoolExecutor(max_workers=self.agent_concurrency, thread_name_prefix="mememeow-agent")
        self._agent_task_types = {"meme_context_generation"}
        self._batch_finalizer: Callable[[str], Any] | None = None
        self._finalized_batches: set[str] = set()
        self._started = False
        self._stopped = False
        self._load_records()

    def register(self, task_type: str, handler: TaskHandler) -> None:
        """注册能从 payload 重建任务的处理器，必须在 start 前完成。"""
        if not task_type:
            raise ValueError("task_type_required")
        self._handlers[task_type] = handler

    def set_batch_finalizer(self, callback: Callable[[str], Any] | None) -> None:
        """注册批次终态回调，供 API 在所有语境任务结束后合并生成一次缓存。"""
        self._batch_finalizer = callback

    def _path(self, task_id: str) -> Path:
        """返回任务 JSON 的受控存储位置。"""
        return self.task_root / f"{task_id}.json"

    def _write(self, record: TaskRecord) -> None:
        """以 fsync 加原子替换保存单条任务，防止半写入记录。"""
        target = self._path(record.task_id)
        temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}.{id(self)}")
        payload = json.dumps(record.as_dict(include_payload=True), ensure_ascii=False, separators=(",", ":"))
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                os.fchmod(handle.fileno(), 0o600)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                directory_fd = os.open(self.task_root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # 某些文件系统不支持目录 fsync；文件替换仍是可恢复的。
                pass
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _load_records(self) -> None:
        """恢复任务文件并隔离损坏记录，避免单个文件阻断服务启动。"""
        for path in sorted(self.task_root.glob("*.json")):
            try:
                record = TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if self._path(record.task_id) != path:
                    raise ValueError("task_filename_mismatch")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                corrupt = path.with_suffix(".corrupt")
                index = 1
                while corrupt.exists():
                    corrupt = path.with_suffix(f".corrupt.{index}")
                    index += 1
                try:
                    shutil.move(path, corrupt)
                except OSError:
                    pass
                continue
            self._records[record.task_id] = record

    @staticmethod
    def _payload_key(payload: dict[str, Any]) -> str:
        """将 payload 规范化为活动任务去重键。"""
        # 批次标识只用于协调，不应让同一图片在不同批次重复启动 Agent。
        comparable = {key: value for key, value in payload.items() if key not in {"batch_id", "batch_ids"}}
        return json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _batch_ids(record: TaskRecord) -> tuple[str, ...]:
        """读取任务关联的批次标识，兼容单值和去重合并后的列表格式。"""
        values: list[str] = []
        single = record.payload.get("batch_id")
        if isinstance(single, str) and single:
            values.append(single)
        multiple = record.payload.get("batch_ids")
        if isinstance(multiple, list):
            values.extend(value for value in multiple if isinstance(value, str) and value)
        return tuple(dict.fromkeys(values))

    def start(self) -> None:
        """恢复排队任务，并把无法证明存活的旧运行任务标记为中断。"""
        with self._lock:
            if self._started:
                return
            self._started = True
            queued: list[str] = []
            for record in self._records.values():
                if record.status == "running":
                    record.status = "failed"
                    record.message = "服务重启中断了任务"
                    record.error = {"error": "task_interrupted", "message": "服务重启导致执行状态无法确认"}
                    record.completed_at = now()
                    record.updated_at = record.completed_at
                    self._write(record)
                elif record.status == "queued":
                    queued.append(record.task_id)
        for task_id in sorted(queued, key=lambda value: (self._records[value].created_at, value)):
            self._schedule(task_id)

    def submit(self, task_type: str, payload: dict[str, Any] | None = None) -> TaskRecord:
        """持久化新任务；相同类型和规范化 payload 的活动任务会被复用。"""
        payload = dict(payload or {})
        key = self._payload_key(payload)
        with self._lock:
            if self._stopped:
                raise RuntimeError("task_service_stopped")
            for record in self._records.values():
                same_context_target = (
                    task_type == "meme_context_generation"
                    and record.task_type == task_type
                    and isinstance(payload.get("image_relative_path"), str)
                    and isinstance(payload.get("image_sha256"), str)
                    and record.payload.get("image_relative_path") == payload.get("image_relative_path")
                    and record.payload.get("image_sha256") == payload.get("image_sha256")
                )
                if record.task_type == task_type and record.status not in TERMINAL and (same_context_target or self._payload_key(record.payload) == key):
                    if same_context_target and isinstance(payload.get("batch_id"), str) and payload["batch_id"]:
                        # 同一图片被不同批次复用时保留全部关联，确保每个批次都能触发终态缓存合并。
                        batch_ids = list(self._batch_ids(record))
                        if payload["batch_id"] not in batch_ids:
                            batch_ids.append(payload["batch_id"])
                            record.payload["batch_ids"] = batch_ids
                            self._write(record)
                    return TaskRecord.from_dict(record.as_dict(include_payload=True))
            if task_type in self._agent_task_types and self._queued_agent_count_locked() >= self.agent_backpressure:
                # 只限制等待队列，正在运行的 slot 不被新配置半途打断。
                raise RuntimeError("agent_backpressure")
            record = TaskRecord(
                task_id=uuid4().hex,
                task_type=task_type,
                payload=payload,
                message="Agent lane 背压排队" if task_type in self._agent_task_types and self._agent_load_locked() >= self.agent_concurrency else None,
                settings_version=str(payload.get("settings_version") or self.settings_version) if task_type == "meme_context_generation" else self.settings_version,
                agent_concurrency=self.agent_concurrency if task_type == "meme_context_generation" else None,
            )
            self._records[record.task_id] = record
            self._write(record)
        if self._started:
            self._schedule(record.task_id)
        return TaskRecord.from_dict(record.as_dict(include_payload=True))

    def _schedule(self, task_id: str) -> None:
        """把已持久化的 queued 任务交给线程池，不重复提交运行记录。"""
        with self._lock:
            record = self._records.get(task_id)
            if not record or record.status != "queued" or self._stopped:
                return
        executor = self._agent_executor if record.task_type in self._agent_task_types else self._executor
        executor.submit(self._run, task_id)

    def _queued_agent_count_locked(self) -> int:
        """统计当前尚未启动的 Agent 任务数量，调用方必须持有任务锁。"""
        return sum(1 for item in self._records.values() if item.task_type in self._agent_task_types and item.status == "queued")

    def _agent_load_locked(self) -> int:
        """统计 Agent lane 中排队或运行的任务数量，调用方必须持有任务锁。"""
        return sum(1 for item in self._records.values() if item.task_type in self._agent_task_types and item.status not in TERMINAL)

    def _maybe_finalize_batch(self, record: TaskRecord) -> None:
        """在批次所有语境任务进入终态后调用一次缓存合并回调。"""
        batch_ids = self._batch_ids(record)
        if record.task_type not in self._agent_task_types or not batch_ids:
            return
        callbacks: list[tuple[str, Callable[[str], Any]]] = []
        with self._lock:
            callback = self._batch_finalizer
            if callback is None:
                return
            for batch_id in batch_ids:
                if batch_id in self._finalized_batches:
                    continue
                active = any(
                    item.task_type in self._agent_task_types
                    and batch_id in self._batch_ids(item)
                    and item.status not in TERMINAL
                    for item in self._records.values()
                )
                if active:
                    continue
                self._finalized_batches.add(batch_id)
                callbacks.append((batch_id, callback))
        for batch_id, callback in callbacks:
            try:
                callback(batch_id)
            except Exception:
                # 缓存失败由其独立任务记录承载，不改变已完成的语境任务。
                pass

    def _run(self, task_id: str) -> None:
        """执行已注册 handler，并将全部状态转换持久化。"""
        with self._lock:
            record = self._records.get(task_id)
            if not record or record.status != "queued" or self._stopped:
                return
            record.status = "running"
            record.attempts += 1
            record.message = "任务开始"
            record.updated_at = now()
            self._write(record)
            handler = self._handlers.get(record.task_type)
        if handler is None:
            self.update(task_id, status="failed", message="任务处理器不可用", error={"error": "task_handler_missing", "message": "当前服务未注册此任务类型"})
            return

        def progress(value: float | None, message: str | None = None) -> None:
            self.update(task_id, progress=value, message=message)

        try:
            result = handler(dict(record.payload), progress)
        except Exception as exc:  # noqa: BLE001
            diagnostic = str(exc)[:500]
            code = diagnostic.partition(":")[0]
            if code not in STABLE_TASK_ERRORS:
                code = "task_failed"
            self.update(task_id, status="failed", message="任务执行失败", error={"error": code, "message": diagnostic})
        else:
            self.update(task_id, status="succeeded", progress=1.0, message="任务完成", result=result)
        finally:
            latest = self.get(task_id)
            if latest:
                self._maybe_finalize_batch(latest)

    def update(self, task_id: str, **changes: Any) -> None:
        """更新任务状态或进度，终态记录不可被后续线程覆写。"""
        with self._lock:
            record = self._records.get(task_id)
            if not record or record.status in TERMINAL:
                return
            for key, value in changes.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            record.updated_at = now()
            if record.status in TERMINAL:
                record.completed_at = record.updated_at
            self._write(record)

    def get(self, task_id: str) -> TaskRecord | None:
        """返回单条任务的独立快照。"""
        with self._lock:
            record = self._records.get(task_id)
            return TaskRecord.from_dict(record.as_dict(include_payload=True)) if record else None

    def find_active(self, task_type: str, dedupe_key: str) -> TaskRecord | None:
        """按兼容任务服务的规范化 payload 查找活动任务。

        本地 JSON 服务没有数据库 dedupe 列；该方法只为图片 Worker 的统一查询
        协议保留，PostgreSQL 服务使用持久 dedupe_key 实现更严格的判断。
        """
        if not isinstance(task_type, str) or not task_type or not isinstance(dedupe_key, str) or not dedupe_key:
            return None

        def comparable(record: TaskRecord) -> str:
            """按 PostgreSQL facade 的规则重建兼容服务去重键。"""
            payload = record.payload
            if task_type == "meme_context_generation":
                return "context:{meme}:{sha}:{config}:{policy}:r{revision}".format(
                    meme=payload.get("meme_id"),
                    sha=payload.get("image_sha256"),
                    config=payload.get("processing_config_hash") or payload.get("skill_hash") or payload.get("model"),
                    policy=payload.get("reverse_image_policy") or "forbid",
                    revision=payload.get("job_revision") or "legacy",
                )
            return f"{task_type}:{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"

        with self._lock:
            records = [record for record in self._records.values() if record.task_type == task_type and record.status not in TERMINAL and comparable(record) == dedupe_key]
            if not records:
                return None
            record = min(records, key=lambda item: (item.created_at, item.task_id))
            return TaskRecord.from_dict(record.as_dict(include_payload=True))

    def list(self, *, statuses: set[str] | None = None, task_types: set[str] | None = None, cursor: str | None = None, limit: int = 50) -> tuple[list[TaskRecord], str | None]:
        """按更新时间和 ID 稳定倒序分页，返回受调用方控制的记录快照。"""
        with self._lock:
            records = list(self._records.values())
            if statuses:
                records = [record for record in records if record.status in statuses]
            if task_types:
                records = [record for record in records if record.task_type in task_types]
            records.sort(key=lambda record: (record.updated_at, record.task_id), reverse=True)
            if cursor:
                try:
                    position = next(index for index, record in enumerate(records) if record.task_id == cursor) + 1
                except StopIteration:
                    position = 0
                records = records[position:]
            page = records[: max(1, min(limit, 100))]
            next_cursor = page[-1].task_id if len(records) > len(page) else None
            return [TaskRecord.from_dict(record.as_dict(include_payload=True)) for record in page], next_cursor

    def shutdown(self) -> None:
        """收束未完成记录；子进程由相应 handler 的服务负责终止。"""
        with self._lock:
            self._stopped = True
            for record in self._records.values():
                if record.status not in TERMINAL:
                    record.status = "failed"
                    record.message = "服务关闭中断了任务"
                    record.error = {"error": "task_interrupted", "message": "服务关闭导致任务中断"}
                    record.completed_at = now()
                    record.updated_at = record.completed_at
                    self._write(record)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._agent_executor.shutdown(wait=False, cancel_futures=True)


class TaskManager:
    """兼容旧单元测试的进程内任务管理器，应用不再使用该类。"""

    def __init__(self, max_workers: int = 2):
        self._records: dict[str, TaskRecord] = {}
        self._active: dict[str, str] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mememeow-legacy-task")

    def submit(self, task_type: str, fn: Callable[[Callable[[float | None, str | None], None]], Any]) -> TaskRecord:
        """提交旧式 closure 任务，仅为兼容外部直接调用保留。"""
        with self._lock:
            active = self._active.get(task_type)
            if active and self._records[active].status not in TERMINAL:
                return self._records[active]
            record = TaskRecord(task_id=uuid4().hex, task_type=task_type)
            self._records[record.task_id] = record
            self._active[task_type] = record.task_id
        self._executor.submit(self._run, record.task_id, fn)
        return record

    def _run(self, task_id: str, fn: Callable[[Callable[[float | None, str | None], None]], Any]) -> None:
        """执行兼容任务并持有原有任务状态机语义。"""
        self.update(task_id, status="running", message="任务开始")
        try:
            result = fn(lambda value, message=None: self.update(task_id, progress=value, message=message))
        except Exception as exc:  # noqa: BLE001
            self.update(task_id, status="failed", message="任务执行失败", error={"error": "task_failed", "message": str(exc)})
        else:
            self.update(task_id, status="succeeded", progress=1.0, message="任务完成", result=result)

    def update(self, task_id: str, **changes: Any) -> None:
        """更新兼容任务记录。"""
        with self._lock:
            record = self._records.get(task_id)
            if not record or record.status in TERMINAL:
                return
            for key, value in changes.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            record.updated_at = now()
            if record.status in TERMINAL:
                record.completed_at = record.updated_at

    def get(self, task_id: str) -> TaskRecord | None:
        """读取兼容任务快照。"""
        with self._lock:
            record = self._records.get(task_id)
            return TaskRecord.from_dict(record.as_dict(include_payload=True)) if record else None

    def shutdown(self) -> None:
        """结束兼容执行器。"""
        with self._lock:
            for record in self._records.values():
                if record.status not in TERMINAL:
                    record.status = "failed"
                    record.error = {"error": "task_not_recoverable", "message": "服务重启导致任务失败"}
                    record.completed_at = now()
        self._executor.shutdown(wait=False, cancel_futures=True)
