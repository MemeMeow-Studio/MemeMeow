"""Agent 最终结果交付脚本的协议校验测试。"""

from __future__ import annotations

import json
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
