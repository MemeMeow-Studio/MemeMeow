"""公网结果、任务历史和图片处理快照的安全 DTO 测试。"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from backend.image_processing import ImageProcessingSnapshot
from backend.public_dto import (
    PublicDataError,
    public_processing_stage,
    public_processing_warning,
    sanitize_task_result,
    secret_inventory_from_mapping,
    validate_agent_result,
)
from backend.tasks import TaskRecord
from api import _task_summary


def _agent_result() -> dict[str, object]:
    """构造不含敏感内容的最小 Agent 公开结果。"""
    return {
        "title": "测试图片",
        "summary": "一张用于边界测试的图片",
        "subjects": ["测试"],
        "visible_text": [],
        "references": [],
        "meaning": None,
        "keywords": ["测试"],
        "search_queries": [],
        "uncertainties": [],
        "source_urls": ["https://example.com/reference"],
    }


def test_agent_result_rejects_unknown_fields_as_a_whole() -> None:
    """未知顶层字段必须拒绝完整结果，不能静默删除后继续写入。"""
    value = _agent_result()
    value["scope_id"] = "scope-secret"
    with pytest.raises(PublicDataError, match="result_schema_invalid"):
        validate_agent_result(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://user:password@example.com/private",
        "http://127.0.0.1:8080/internal/result",
        "https://example.com/download?access_token=secret-value",
        "postgresql://db-user:db-password@example.internal:5432/app",
        "password=plain-text-secret",
    ],
)
def test_agent_result_rejects_sensitive_urls(value: str) -> None:
    """URL userinfo、私网地址和敏感查询参数都必须整体拒绝。"""
    result = _agent_result()
    result["summary"] = value
    with pytest.raises(PublicDataError, match="result_sensitive_data"):
        validate_agent_result(result)


def test_agent_result_rejects_registered_secret_encoding_variant() -> None:
    """已登记凭据的常见 Base64 变体不能绕过结果边界。"""
    secret = "public-test-secret-123456"
    result = _agent_result()
    result["summary"] = base64.b64encode(secret.encode()).decode()
    inventory = secret_inventory_from_mapping({"MEMEMEOW_OPENCODE_API_KEY": secret})
    with pytest.raises(PublicDataError, match="result_sensitive_data"):
        validate_agent_result(result, secret_inventory=inventory)


def test_dirty_task_history_and_result_are_projected_without_type_errors() -> None:
    """脏历史字段不能触发裸异常，也不能通过公开 DTO 原样返回。"""
    record = TaskRecord.from_dict(
        {
            "task_id": "task-1",
            "task_type": "visual_embedding_generation",
            "status": [],
            "submission_mode": {},
            "image_stage": [],
            "processing_job_id": {"path": "/runtime/internal"},
            "attempts": [],
            "resume_attempts": {},
            "agent_concurrency": [],
            "created_at": "/runtime/task.json",
            "updated_at": "not-a-time",
            "resume_started_at": {"path": "/tmp"},
            "result": {
                "visual_model": "/runtime/model.bin",
                "dimensions": [],
                "preprocess_version": "safe-version",
                "internal_scope_id": "scope-secret",
            },
            "payload": {"scope_id": "scope-secret", "api_key": "hidden"},
        }
    )
    public = record.as_dict()
    assert public["status"] == "failed"
    assert public["result"] == {"preprocess_version": "safe-version"}
    assert "payload" not in public
    assert "scope_id" not in public
    assert public["created_at"] is not None
    assert public["resume_started_at"] is None


def test_processing_dtos_fail_closed_for_non_string_enum_values() -> None:
    """阶段和 warning 的枚举字段遇到列表或对象时必须收束为安全值。"""
    stage = public_processing_stage(
        {
            "stage": [],
            "status": {},
            "task_id": [],
            "session_id": {"secret": "value"},
            "attempt": [],
            "resume_available": [],
        },
        job_id="job-1",
    )
    warning = public_processing_warning({"stage": {}, "error": [], "message": {}})
    assert stage["stage"] == "unknown"
    assert stage["status"] == "failed"
    assert stage["processing_job_id"] == "job-1"
    assert warning == {"error": "auto_rename_warning", "message": "自动重命名未完成"}


def test_image_processing_snapshot_projects_dirty_history() -> None:
    """图片快照不得把脏状态、路径、错误正文或未知阶段字段直接返回。"""
    snapshot = ImageProcessingSnapshot(
        job_id="../job",
        scope_id="scope-secret",
        meme_id="../../meme",
        revision=[],
        image_sha256="/runtime/image.png",
        reverse_image_policy=[],
        status=[],
        current_stage={},
        stages=({"stage": [], "status": [], "scope_id": "scope-secret"},),
        error={"error": [], "message": "https://user:pass@example.com/secret"},
        retry_at="/tmp/retry",
        created_at="/tmp/created",
        warnings=({"stage": [], "error": [], "message": {"path": "/runtime"}},),
    )
    public = snapshot.as_dict()
    assert public["task_id"] == "invalid-job"
    assert public["meme_id"] == "invalid-meme"
    assert public["image_sha256"] is None
    assert public["status"] == "failed"
    assert public["current_stage"] is None
    assert public["retry_at"] is None
    assert public["error"]["error"] == "image_processing_failed"
    assert "user:pass" not in str(public)
    assert public["stages"][0]["stage"] == "unknown"


def test_task_summary_does_not_echo_payload_derived_paths_or_invalid_ids() -> None:
    """任务摘要的动态视觉字段和图片标识必须经过公开投影。"""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)))
    record = TaskRecord(
        task_id="task-visual",
        task_type="visual_embedding_generation",
        payload={
            "visual_model": "/runtime/private-model",
            "visual_dimensions": {"secret": "/runtime/private"},
            "preprocess_version": "safe-version",
            "meme_id": "../../private-meme",
        },
    )
    public = _task_summary(request, record, {})
    assert public["visual"] == {"model": None, "dimensions": None, "preprocess_version": "safe-version"}
    assert "image" not in public
    assert "/runtime" not in str(public)
    assert "payload" not in public
