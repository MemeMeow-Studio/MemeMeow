"""Agent 最终结果交付脚本的协议校验测试。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "skills" / "research-meme-context" / "scripts" / "validate_result.py"


def run_verifier(directory: Path) -> subprocess.CompletedProcess[str]:
    """运行 Agent 可调用的结果验证脚本并保留其诊断输出。"""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(directory)],
        capture_output=True,
        check=False,
        text=True,
    )


def _result_with_reference(reference: str) -> dict[str, object]:
    """构造包含公开结果字段的预检样本。"""
    return {
        "title": "验证",
        "summary": "用于预检的结果",
        "subjects": [],
        "visible_text": [],
        "references": [reference],
        "meaning": None,
        "keywords": [],
        "search_queries": [],
        "uncertainties": [],
    }


def test_verifier_accepts_final_result_file(tmp_path: Path):
    """正确命名且可解析的最终 JSON 必须通过 Agent 侧检查。"""
    (tmp_path / "result.json.tmp").write_text(json.dumps({"title": "验证"}), encoding="utf-8")

    completed = run_verifier(tmp_path)

    assert completed.returncode == 0
    assert "校验通过" in completed.stdout


def test_verifier_explains_common_result_filename_mistake(tmp_path: Path):
    """误写 result.json 时必须给出可操作的固定文件名诊断。"""
    (tmp_path / "result.json").write_text(json.dumps({"title": "验证"}), encoding="utf-8")

    completed = run_verifier(tmp_path)

    assert completed.returncode == 1
    assert "发现 result.json" in completed.stderr
    assert "result.json.tmp" in completed.stderr


def test_verifier_rejects_invalid_json(tmp_path: Path):
    """最终文件格式损坏时必须阻止 Agent 正常退出。"""
    (tmp_path / "result.json.tmp").write_text('{"title":', encoding="utf-8")

    completed = run_verifier(tmp_path)

    assert completed.returncode == 1
    assert "不是有效 UTF-8 JSON" in completed.stderr


def test_verifier_allows_natural_language_slash(tmp_path: Path):
    """共享安全扫描不能误伤自然语言中的作品名分隔符。"""
    (tmp_path / "result.json.tmp").write_text(
        json.dumps(_result_with_reference("はたらく細胞 / Cells at Work!"), ensure_ascii=False),
        encoding="utf-8",
    )

    completed = run_verifier(tmp_path)

    assert completed.returncode == 0


def test_verifier_reports_shared_sensitive_data_reason(tmp_path: Path):
    """Agent 预检应复用服务端扫描并返回不含原文的原因码。"""
    (tmp_path / "result.json.tmp").write_text(
        json.dumps(_result_with_reference("path=/runtime/secret"), ensure_ascii=False),
        encoding="utf-8",
    )

    completed = run_verifier(tmp_path)

    assert completed.returncode == 1
    assert "result_sensitive_data" in completed.stderr
    assert "/runtime/secret" not in completed.stderr


def test_verifier_runs_with_executor_snapshot_only(tmp_path: Path):
    """容器只提供 executor 协议快照时，Skill 预检仍必须可启动。"""
    image_root = tmp_path / "image"
    script_path = image_root / "skills/research-meme-context/scripts/validate_result.py"
    executor_root = image_root / "executor"
    result_root = tmp_path / "task-results/task-1"
    script_path.parent.mkdir(parents=True)
    result_root.mkdir(parents=True)
    shutil.copytree(SCRIPT_PATH.parents[3] / "executor", executor_root)
    shutil.copyfile(SCRIPT_PATH, script_path)
    shutil.copyfile(SCRIPT_PATH.parents[3] / "backend/public_dto.py", executor_root / "public_dto.py")
    shutil.copyfile(SCRIPT_PATH.parents[3] / "backend/opencode_workspace.py", executor_root / "opencode_workspace.py")
    (result_root / "result.json.tmp").write_text(
        json.dumps(_result_with_reference("はたらく細胞 / Cells at Work!"), ensure_ascii=False),
        encoding="utf-8",
    )

    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONPATH"] = str(image_root)
    completed = subprocess.run(
        [sys.executable, str(script_path), str(result_root)],
        capture_output=True,
        check=False,
        text=True,
        cwd=image_root,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr

    startup = subprocess.run(
        [sys.executable, "-c", "import executor.server"],
        capture_output=True,
        check=False,
        text=True,
        cwd=image_root,
        env=environment,
    )
    assert startup.returncode == 0, startup.stderr


def test_agent_image_declares_runtime_snapshots() -> None:
    """Agent 镜像构建定义必须提供预检和 executor 所需的最小快照。"""
    dockerfile = (SCRIPT_PATH.parents[3] / "docker" / "agent" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY executor /opt/mememeow/executor" in dockerfile
    assert "COPY backend/public_dto.py /opt/mememeow/executor/public_dto.py" in dockerfile
    assert "COPY backend/opencode_workspace.py /opt/mememeow/executor/opencode_workspace.py" in dockerfile
    assert "PYTHONPATH=/opt/mememeow" in dockerfile
