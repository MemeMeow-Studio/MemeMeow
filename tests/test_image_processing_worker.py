"""图片处理 Worker 的去重与 Agent grant 生命周期单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.image_processing import ImageProcessingError, ImageProcessingOptions, ImageProcessingWorker, normalize_auto_name, normalize_reverse_image_policy
from backend.operation_policy import AllowAllOperationPolicy, GrantAssociationStore, OperationPolicyGateway, PolicyDecision


class _CountingPolicy(AllowAllOperationPolicy):
    """记录 acquire/release 次数的测试 policy。"""

    def __init__(self, *, allowed: bool = True) -> None:
        super().__init__()
        self.allowed = allowed
        self.acquire_count = 0
        self.release_count = 0

    def acquire(self, request):
        """返回允许或拒绝结果，并统计真实 acquire。"""
        self.acquire_count += 1
        if not self.allowed:
            return PolicyDecision(False, "operation_forbidden")
        return super().acquire(request)

    def release(self, grant):
        """统计补偿 release 后复用 allow-all 幂等实现。"""
        self.release_count += 1
        return super().release(grant)


class _TaskService:
    """只实现 Worker 准备阶段所需的活动查询和提交协议。"""

    def __init__(self, *, fail_submit: bool = False) -> None:
        self.active: dict[str, SimpleNamespace] = {}
        self.fail_submit = fail_submit

    def find_active(self, task_type: str, dedupe_key: str):
        """返回活动任务快照。"""
        return self.active.get(dedupe_key)

    def submit(self, task_type: str, payload: dict[str, object], *, schedule: bool = True):
        """按 Worker 传入的 dedupe key 记录一个活动任务。"""
        del schedule
        if self.fail_submit:
            raise RuntimeError("submit_failed")
        dedupe_key = ImageProcessingWorker._task_dedupe_key(task_type, payload)
        task = self.active.get(dedupe_key)
        if task is None:
            task = SimpleNamespace(task_id=uuid4().hex, status="queued", payload=dict(payload), task_type=task_type)
            self.active[dedupe_key] = task
        return task


def _job() -> SimpleNamespace:
    """构造一份不依赖 ORM session 的图片 job 快照。"""
    return SimpleNamespace(
        id=uuid4(),
        meme_id=uuid4(),
        revision=1,
        claim_generation=1,
        image_sha256="a" * 64,
        reverse_image_policy="auto",
        processing_config_hash="b" * 64,
        processing_config={"agent_model": "test-model", "embedding_model": "test-embedding"},
    )


def _worker(tasks: _TaskService, policy: _CountingPolicy) -> ImageProcessingWorker:
    """构造不启动数据库 facade 的 Worker，并替换阶段绑定写入。"""
    worker = ImageProcessingWorker(
        object(),
        scope_id="local",
        task_service=tasks,
        policy=OperationPolicyGateway(policy),
        grant_store=GrantAssociationStore(),
    )
    worker.jobs.attach_task = lambda job_id, stage, task_id: True
    return worker


def test_active_agent_task_is_bound_before_policy_acquire() -> None:
    """已有活动 Agent Task 直接复用，不建立新的 policy reservation。"""
    tasks = _TaskService()
    policy = _CountingPolicy()
    worker = _worker(tasks, policy)
    try:
        job = _job()
        first = worker._prepare_task(job, "agent")
        second = worker._prepare_task(job, "agent")
        assert first == second
        assert policy.acquire_count == 1
    finally:
        worker.shutdown()


def test_image_leaf_runner_inherits_resume_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整图片流水线创建叶子 facade 时必须传递 session 续跑边界。"""
    captured: dict[str, object] = {}

    class _CapturingTaskRunner:
        """只记录叶子 facade 构造参数的最小替身。"""

        def __init__(self, _resources, **kwargs):
            captured.update(kwargs)

        def register(self, _task_type, _handler):
            """接受图片阶段 handler 注册。"""

        def shutdown(self):
            """模拟叶子 facade 关闭。"""

    monkeypatch.setattr("backend.pg_services.PostgresTaskService", _CapturingTaskRunner)
    task_service = SimpleNamespace(
        agent_concurrency=2,
        agent_backpressure=12,
        settings_version="settings-test",
        lease_seconds=45,
        max_attempts=4,
        resume_enabled=True,
        resume_max_attempts=3,
        resume_backoff_seconds=5,
        resume_max_backoff_seconds=90,
        resume_timeout_seconds=1200,
    )
    worker = ImageProcessingWorker(
        SimpleNamespace(factory=lambda: None),
        scope_id="local",
        task_service=task_service,
        task_handlers={"meme_context_generation": lambda *_args: None},
    )
    try:
        assert captured["resume_enabled"] is True
        assert captured["resume_max_attempts"] == 3
        assert captured["resume_backoff_seconds"] == 5
        assert captured["resume_max_backoff_seconds"] == 90
        assert captured["resume_timeout_seconds"] == 1200
    finally:
        worker.shutdown()


def test_submit_failure_releases_only_uncommitted_grant() -> None:
    """叶子 Task 未创建时才补偿释放已取得的 grant。"""
    tasks = _TaskService(fail_submit=True)
    policy = _CountingPolicy()
    worker = _worker(tasks, policy)
    try:
        with pytest.raises(RuntimeError, match="submit_failed"):
            worker._prepare_task(_job(), "agent")
        assert policy.acquire_count == 1
        assert policy.release_count == 1
    finally:
        worker.shutdown()


def test_policy_denial_blocks_stage_without_task() -> None:
    """Agent policy 拒绝时阶段进入 blocked，且不提交叶子 Task。"""
    tasks = _TaskService()
    policy = _CountingPolicy(allowed=False)
    worker = _worker(tasks, policy)
    transitions: list[dict[str, object]] = []
    worker.jobs.transition = lambda *args, **kwargs: transitions.append(kwargs) or True
    try:
        with pytest.raises(ImageProcessingError, match="blocked"):
            worker._prepare_task(_job(), "agent")
        assert policy.acquire_count == 1
        assert tasks.active == {}
        assert transitions[-1]["status"] == "blocked"
    finally:
        worker.shutdown()


def test_standalone_stage_aliases_are_canonical_and_mode_isolated() -> None:
    """公开任务类型别名必须落到固定阶段，且 standalone 不复用 pipeline key。"""
    assert ImageProcessingWorker._canonical_stage("visual_embedding_generation") == "visual"
    assert ImageProcessingWorker._canonical_stage("meme_context_generation") == "agent"
    pipeline = ImageProcessingWorker._task_dedupe_key(
        "meme_context_generation",
        {"submission_mode": "pipeline", "job_id": "job-1", "meme_id": "meme", "image_sha256": "a" * 64, "processing_config_hash": "b" * 64, "reverse_image_policy": "forbid", "job_revision": 1},
    )
    standalone = ImageProcessingWorker._task_dedupe_key(
        "meme_context_generation",
        {"submission_mode": "standalone", "meme_id": "meme", "image_sha256": "a" * 64, "processing_config_hash": "b" * 64, "reverse_image_policy": "forbid"},
    )
    assert pipeline != standalone


def test_unknown_standalone_stage_is_rejected() -> None:
    """阶段控制面拒绝未知标识，避免客户端选择落入错误处理器。"""
    with pytest.raises(ImageProcessingError, match="invalid_image_stage"):
        ImageProcessingWorker._canonical_stage("metadata_repair")


def test_processing_options_use_safe_defaults_but_reject_explicit_empty_values() -> None:
    """缺失选项使用安全默认值，显式空字符串和非布尔值不能静默改写语义。"""
    assert ImageProcessingOptions.normalize() == ImageProcessingOptions(reverse_image_policy="forbid", auto_name=False)
    assert normalize_reverse_image_policy(None) == "forbid"
    assert normalize_auto_name(None) is False
    with pytest.raises(ImageProcessingError, match="invalid_reverse_image_policy"):
        normalize_reverse_image_policy("")
    with pytest.raises(ImageProcessingError, match="invalid_reverse_image_policy"):
        normalize_reverse_image_policy([])
    with pytest.raises(ImageProcessingError, match="invalid_reverse_image_policy"):
        normalize_reverse_image_policy({"policy": "auto"})
    with pytest.raises(ImageProcessingError, match="invalid_auto_name"):
        normalize_auto_name("")
    with pytest.raises(ImageProcessingError, match="invalid_auto_name"):
        normalize_auto_name("false")


class _AttachSession:
    """按固定查询顺序返回 Job、阶段和叶子任务的绑定测试 session。"""

    def __init__(self, values: list[object]) -> None:
        self.values = iter(values)

    def __enter__(self):
        """返回可供 repository 使用的伪 session。"""
        return self

    def __exit__(self, *_args) -> bool:
        """结束测试事务，不吞掉异常。"""
        return False

    def scalar(self, _statement):
        """按 repository 的三次查询顺序返回固定实体。"""
        return next(self.values)

    def commit(self) -> None:
        """记录事务提交边界；测试不需要持久化。"""
        return None


def test_pipeline_task_binding_requires_current_job_lease() -> None:
    """过期 Worker 不能绑定叶子 Task，当前 claim 才能写入父阶段。"""
    from backend.image_processing import ImageProcessingRepository

    job_id = uuid4()
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(
        id=job_id,
        status="running",
        lease_owner="current-worker",
        lease_expires_at=now + timedelta(minutes=1),
        claim_generation=4,
        meme_id=uuid4(),
        image_sha256="a" * 64,
    )
    stage = SimpleNamespace(task_id=None, updated_at=None)
    child = SimpleNamespace(
        task_type="visual_embedding_generation",
        submission_mode=None,
        processing_job_id=None,
        image_stage=None,
        payload={},
    )
    repository = ImageProcessingRepository(object(), "local")
    repository._session = lambda: _AttachSession([job, stage, child])

    assert repository.attach_task(job_id, "visual", "task-1", owner="stale-worker", claim_generation=4) is False
    assert stage.task_id is None

    repository._session = lambda: _AttachSession([job, stage, child])
    assert repository.attach_task(job_id, "visual", "task-1", owner="current-worker", claim_generation=4) is True
    assert stage.task_id == "task-1"


def test_unbound_pipeline_task_cleanup_checks_job_ownership() -> None:
    """阶段绑定失败时只收束仍归属当前 Job 的 queued 叶子任务。"""
    job_id = uuid4()
    meme_id = uuid4()
    job = SimpleNamespace(id=job_id, meme_id=meme_id, image_sha256="b" * 64)
    task = SimpleNamespace(
        task_type="visual_embedding_generation",
        submission_mode="pipeline",
        image_stage="visual",
        processing_job_id=job_id,
        status="queued",
        payload={"job_id": str(job_id), "meme_id": str(meme_id), "image_sha256": job.image_sha256},
        message=None,
        error=None,
        completed_at=None,
        updated_at=None,
    )
    session = _AttachSession([task])
    worker = ImageProcessingWorker(object(), scope_id="local", task_service=_TaskService())
    worker.resources = SimpleNamespace(factory=lambda: session)
    try:
        assert worker._fail_unbound_pipeline_task("task-2", job, "visual", "stage_task_bind_failed") is True
        assert task.status == "failed"
        assert task.error == {"error": "stage_task_bind_failed"}
    finally:
        worker.shutdown()
