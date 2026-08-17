"""OpenCode 候选解析与持久任务恢复测试。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend import opencode as opencode_module
from backend.config import Settings
from backend.opencode import DIAGNOSTIC_LOG_BYTES, OPENCODE_REASONING_VARIANT, OpenCodeError, OpenCodeRunner
from backend.tasks import PersistentTaskService


def make_settings(tmp_path: Path) -> Settings:
    """构造不依赖真实 OpenCode 的隔离配置。"""
    return Settings(
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        embedding_api_key=None,
        embedding_base_url=None,
        embedding_model="embedding",
        llm_enhance_model=None,
        protected_mode=False,
        allowed_endpoints=("/",),
        rate_limit_enabled=False,
        rate_limit_requests=1,
        rate_limit_window=1,
        max_upload_size=1,
        opencode_executable=None,
        opencode_model=None,
    )


def candidate() -> dict[str, object]:
    """提供满足研究输出契约的最小候选。"""
    return {
        "title": "测试标题",
        "summary": "一张用于测试的图片",
        "subjects": ["主体"],
        "visible_text": [],
        "references": [],
        "meaning": None,
        "keywords": ["测试"],
        "search_queries": [],
        "uncertainties": [],
        "source_urls": [],
    }


def test_candidate_extraction_rejects_extra_text_and_accepts_single_fence(tmp_path: Path):
    """只接受完整 JSON 或唯一 fenced JSON，不猜测花括号边界。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    raw = __import__("json").dumps(candidate(), ensure_ascii=False)
    assert runner.extract_candidate(raw)["title"] == "测试标题"
    assert runner.extract_candidate(f"```json\n{raw}\n```")["summary"] == "一张用于测试的图片"
    with pytest.raises(OpenCodeError) as error:
        runner.extract_candidate(f"说明\n```json\n{raw}\n```")
    assert error.value.code == "agent_output_invalid_json"


def test_candidate_validation_checks_required_fields_and_uri_format(tmp_path: Path):
    """输出必须通过 schema 和 Pydantic 双重字段约束。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    assert runner.validate_candidate(candidate())["title"] == "测试标题"
    invalid = candidate()
    invalid["source_urls"] = ["not-a-uri"]
    with pytest.raises(OpenCodeError) as error:
        runner.validate_candidate(invalid)
    assert error.value.code == "agent_output_schema_invalid"


def test_candidate_validation_rejects_title_punctuation(tmp_path: Path):
    """Agent 即使输出结构合法，也不能把标点或符号写入标题。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    invalid = candidate()
    invalid["title"] = "滑稽表情“认真！”"
    with pytest.raises(OpenCodeError) as error:
        runner.validate_candidate(invalid)
    assert error.value.code == "agent_output_schema_invalid"


def test_missing_opencode_configuration_has_stable_error(tmp_path: Path):
    """没有可执行文件或模型时 worker 返回稳定诊断。"""
    with pytest.raises(OpenCodeError) as error:
        OpenCodeRunner(make_settings(tmp_path)).prepare_runtime()
    assert error.value.code == "opencode_not_configured"


def test_prepare_runtime_writes_common_config_without_secrets(tmp_path: Path):
    """runtime 配置固定在 workspace，provider 凭据只保留环境变量引用。"""
    project = tmp_path / "project"
    (project / "skills" / "research-meme-context").mkdir(parents=True)
    modules = project / "node_modules"
    (modules / "@ai-sdk" / "openai").mkdir(parents=True)
    executable = tmp_path / "opencode"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "opencode_executable": str(executable),
            "opencode_model": "mememeow/gpt-5.6-luna",
            "opencode_base_url": "https://example.invalid/v1",
            "opencode_api_key": "not-written-to-config",
            "opencode_node_modules": modules,
        }
    )
    runner = OpenCodeRunner(settings, project_root=project)
    runner.prepare_runtime()

    payload = __import__("json").loads((runner.workspace / "opencode.json").read_text(encoding="utf-8"))
    assert payload["experimental"] == {"continue_loop_on_deny": True}
    provider = payload["provider"]["mememeow"]
    assert provider["npm"] == "@ai-sdk/openai"
    assert provider["options"] == {
        "baseURL": "{env:MEMEMEOW_OPENCODE_BASE_URL}",
        "apiKey": "{env:MEMEMEOW_OPENCODE_API_KEY}",
    }
    assert set(provider["models"]) == {"gpt-5.6-luna"}
    assert provider["models"]["gpt-5.6-luna"]["variants"] == {
        "max": {"reasoningEffort": "max"}
    }
    assert "not-written-to-config" not in (runner.workspace / "opencode.json").read_text(encoding="utf-8")


def test_runtime_environment_isolates_project_config(tmp_path: Path):
    """后台任务和检查脚本必须固定 DB、显式配置并禁止合并父目录配置。"""
    settings = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "opencode_model": "mememeow/gpt-5.6-luna",
            "opencode_base_url": "https://example.invalid/v1",
            "opencode_api_key": "test-key",
        }
    )
    runner = OpenCodeRunner(settings)
    environment = runner.build_environment()
    assert environment["OPENCODE_DB"] == str(runner.db_path)
    assert environment["OPENCODE_CONFIG"] == str(runner.workspace / "opencode.json")
    assert environment["OPENCODE_CONFIG_DIR"] == str(runner.workspace / ".opencode")
    assert environment["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "SERPAPI_API_KEY" not in environment
    assert environment["MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL"].endswith("/internal/reverse-image/search")


def test_host_runtime_environment_contains_only_claim_scoped_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Host 模式也必须把当前 claim task id 传给薄客户端，避免请求落到错误任务。"""
    monkeypatch.setenv("MEMEMEOW_AGENT_CALLBACK_SECRET", "root-callback-secret")
    monkeypatch.setenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN", "executor-token")
    monkeypatch.setenv("MEMEMEOW_DATABASE_URL", "postgresql://should-not-enter-agent")
    settings = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "opencode_model": "mememeow/gpt-5.6-luna",
            "opencode_base_url": "https://example.invalid/v1",
            "opencode_api_key": "test-key",
            "agent_runtime_mode": "host",
        }
    )
    runner = OpenCodeRunner(settings)
    environment = runner.build_environment(2, "claim-task-123")
    assert environment["MEMEMEOW_OPENCODE_SLOT"] == "2"
    assert environment["MEMEMEOW_AGENT_TASK_ID"] == "claim-task-123"
    assert "MEMEMEOW_AGENT_CALLBACK_TOKEN" not in environment
    assert "MEMEMEOW_AGENT_CALLBACK_SECRET" not in environment
    assert "MEMEMEOW_AGENT_EXECUTOR_TOKEN" not in environment
    assert "MEMEMEOW_DATABASE_URL" not in environment
    assert "SERPAPI_API_KEY" not in environment


def test_prepare_runtime_uses_project_opencode_modules_by_default(tmp_path: Path):
    """未覆盖依赖路径时复用项目的 OpenCode 插件依赖而非前端依赖。"""
    project = tmp_path / "project"
    (project / "skills" / "research-meme-context").mkdir(parents=True)
    modules = project / ".opencode" / "node_modules"
    (modules / "@ai-sdk" / "openai").mkdir(parents=True)
    executable = tmp_path / "opencode"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "opencode_executable": str(executable),
            "opencode_model": "mememeow/gpt-5.6-luna",
            "opencode_base_url": "https://example.invalid/v1",
            "opencode_api_key": "test-key",
        }
    )
    runner = OpenCodeRunner(settings, project_root=project)
    runner.prepare_runtime()
    assert (runner.workspace / "node_modules").resolve() == modules.resolve()


def test_runtime_config_is_accepted_by_installed_opencode(tmp_path: Path):
    """已安装 CLI 必须展开 workspace 配置中的环境变量引用。"""
    executable = shutil.which("opencode")
    if executable is None:
        pytest.skip("当前环境未安装 OpenCode CLI")
    project = tmp_path / "project"
    (project / "skills" / "research-meme-context").mkdir(parents=True)
    modules = project / "node_modules"
    (modules / "@ai-sdk" / "openai").mkdir(parents=True)
    settings = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "opencode_executable": executable,
            "opencode_model": "mememeow/gpt-5.6-luna",
            "opencode_base_url": "https://example.invalid/v1",
            "opencode_api_key": "test-key",
            "opencode_node_modules": modules,
        }
    )
    runner = OpenCodeRunner(settings, project_root=project)
    runner.prepare_runtime()
    environment = runner.build_environment()
    result = subprocess.run(
        [executable, "debug", "config"],
        cwd=runner.workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved["provider"]["mememeow"]["options"] == {
        "baseURL": "https://example.invalid/v1",
        "apiKey": "test-key",
    }


def test_runner_fixes_luna_at_max_reasoning_variant(tmp_path: Path):
    """每个研究任务传绝对图片路径并显式要求 max 推理强度。"""
    settings = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "opencode_executable": "opencode",
            "opencode_model": "mememeow/gpt-5.6-luna",
            "opencode_base_url": "https://example.invalid/v1",
            "opencode_api_key": "test-key",
        }
    )
    command = OpenCodeRunner(settings)._run_command(tmp_path / "image.png", "prompt")
    file_index = command.index("--file")
    assert command[file_index + 1] == str((tmp_path / "image.png").resolve())
    variant_index = command.index("--variant")
    assert command[variant_index + 1] == OPENCODE_REASONING_VARIANT == "max"


def test_runner_accepts_large_cli_output_without_accumulating_pipe_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI 事件流超过旧门禁时仍可解析，持久日志只保留诊断前缀。"""
    project = tmp_path / "project"
    (project / "skills" / "research-meme-context").mkdir(parents=True)
    modules = project / "node_modules"
    (modules / "@ai-sdk" / "openai").mkdir(parents=True)
    executable = tmp_path / "fake-opencode.py"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1] == 'run':\n"
        "    sys.stdout.write('{\\\"type\\\":\\\"noise\\\",\\\"payload\\\":\\\"' + ('x' * (3 * 1024 * 1024)) + '\\\"}\\n')\n"
        "    sys.stdout.write('{\\\"type\\\":\\\"session.created\\\",\\\"session_id\\\":\\\"large-session\\\"}\\n')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    settings = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "opencode_executable": str(executable),
            "opencode_model": "mememeow/gpt-5.6-luna",
            "opencode_base_url": "https://example.invalid/v1",
            "opencode_api_key": "test-key",
            "opencode_node_modules": modules,
        }
    )
    runner = OpenCodeRunner(settings, project_root=project)
    monkeypatch.setattr(runner, "_session_messages", lambda session_id, environment: {"messages": [{"role": "assistant", "parts": [{"type": "text", "text": json.dumps(candidate(), ensure_ascii=False)}]}]})
    monkeypatch.setattr(runner, "validate_candidate", lambda value: value)
    image = tmp_path / "image.png"
    image.write_bytes(b"image")

    result, session_id = runner.run(image, lambda value, message: None)

    assert session_id == "large-session"
    assert result["title"] == "测试标题"
    log_path = runner.log_root / f"{hashlib.sha256(str(image).encode()).hexdigest()[:16]}.jsonl"
    assert log_path.stat().st_size == DIAGNOSTIC_LOG_BYTES


def test_session_messages_stream_large_response_without_read_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """session API 大响应按块落盘读取，不再因固定字节门禁失败。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    body = json.dumps([{"role": "tool", "diagnostic": "x" * (3 * 1024 * 1024)}, {"role": "assistant", "parts": [{"type": "text", "text": "{}"}]}]).encode()
    read_sizes: list[int] = []

    class Response:
        """提供可记录 read 尺寸的最小 HTTP 响应夹具。"""

        def __init__(self, payload: bytes):
            self.payload = payload
            self.position = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            if size < 0:
                size = len(self.payload) - self.position
            chunk = self.payload[self.position : self.position + size]
            self.position += len(chunk)
            return chunk

    class Server:
        """避免测试启动真实 OpenCode server。"""

        pid = 0

        def poll(self):
            return None

    monkeypatch.setattr(opencode_module.subprocess, "Popen", lambda *args, **kwargs: Server())
    monkeypatch.setattr(opencode_module, "urlopen", lambda endpoint, timeout=1: Response(body))
    monkeypatch.setattr(runner, "_terminate", lambda process: None)

    result = runner._session_messages("large-session", runner.build_environment())

    assert result["messages"][-1]["role"] == "assistant"
    assert read_sizes
    assert max(read_sizes) <= opencode_module.STREAM_COPY_CHUNK_BYTES


def test_opencode_launcher_reuses_runtime_for_session_list(tmp_path: Path):
    """会话检查入口必须复用同一 workspace 和 DB，并把 list 映射到公开 CLI。"""
    project = Path(__file__).resolve().parent.parent
    executable = tmp_path / "fake-opencode"
    capture = tmp_path / "capture.txt"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$PWD\" \"$OPENCODE_DB\" \"$OPENCODE_CONFIG\" "
        f"\"$OPENCODE_CONFIG_DIR\" \"$OPENCODE_DISABLE_PROJECT_CONFIG\" \"$@\" > {str(capture)!r}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    modules = tmp_path / "node_modules"
    (modules / "@ai-sdk" / "openai").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    environment = {
        **os.environ,
        "MEMEMEOW_OPENCODE_EXECUTABLE": str(executable),
        "MEMEMEOW_OPENCODE_MODEL": "mememeow/gpt-5.6-luna",
        "MEMEMEOW_OPENCODE_BASE_URL": "https://example.invalid/v1",
        "MEMEMEOW_OPENCODE_API_KEY": "test-key",
        "MEMEMEOW_OPENCODE_RUNTIME_ROOT": str(runtime),
        "MEMEMEOW_OPENCODE_NODE_MODULES": str(modules),
        "MEMEMEOW_AGENT_RUNTIME_MODE": "host",
        "MEMEMEOW_PYTHON": sys.executable,
    }
    result = subprocess.run(
        [str(project / "scripts" / "open-opencode.sh"), "--list", "--format", "json"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = capture.read_text(encoding="utf-8").splitlines()
    workspace = str((runtime / "workspace").resolve())
    assert lines[:5] == [
        workspace,
        str((runtime / "opencode.db").resolve()),
        str((runtime / "workspace" / "opencode.json").resolve()),
        str((runtime / "workspace" / ".opencode").resolve()),
        "1",
    ]
    assert lines[5:] == ["session", "list", "--format", "json"]


def test_opencode_launcher_keeps_cli_executable_in_docker_mode(tmp_path: Path):
    """Docker 诊断入口必须保留 opencode 命令并只替换容器内 workspace。"""
    project = Path(__file__).resolve().parent.parent
    capture = tmp_path / "capture-docker.txt"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == info ]]; then exit 0; fi\n"
        "if [[ \"$1\" == inspect ]]; then printf true; exit 0; fi\n"
        "if [[ \"$1\" == exec ]]; then\n"
        f"  printf '%s\\n' \"$@\" > {str(capture)!r}\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    modules = tmp_path / "node_modules"
    (modules / "@ai-sdk" / "openai").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "MEMEMEOW_AGENT_RUNTIME_MODE": "docker",
        "MEMEMEOW_AGENT_CONTAINER_NAME": "mememeow-agent-runtime",
        "MEMEMEOW_OPENCODE_MODEL": "mememeow/gpt-5.6-luna",
        "MEMEMEOW_OPENCODE_BASE_URL": "https://example.invalid/v1",
        "MEMEMEOW_OPENCODE_API_KEY": "test-key",
        "MEMEMEOW_OPENCODE_RUNTIME_ROOT": str(runtime),
        "MEMEMEOW_PYTHON": sys.executable,
    }
    result = subprocess.run(
        [str(project / "scripts" / "open-opencode.sh"), "--list", "--format", "json"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = capture.read_text(encoding="utf-8").splitlines()
    assert "opencode" in lines
    assert lines[lines.index("opencode") + 1 : lines.index("opencode") + 4] == ["session", "list", "--format"]


def test_opencode_launcher_allocates_terminal_for_docker_tui(tmp_path: Path):
    """旧 Docker 兼容模式必须为交互式 OpenCode 分配 stdin 和 TTY。"""
    project = Path(__file__).resolve().parent.parent
    capture = tmp_path / "capture-docker-tui.txt"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == info ]]; then exit 0; fi\n"
        "if [[ \"$1\" == inspect ]]; then printf true; exit 0; fi\n"
        "if [[ \"$1\" == exec ]]; then\n"
        f"  printf '%s\\n' \"$@\" > {str(capture)!r}\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    runtime = tmp_path / "runtime"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "MEMEMEOW_AGENT_RUNTIME_MODE": "docker",
        "MEMEMEOW_AGENT_CONTAINER_NAME": "mememeow-agent-runtime",
        "MEMEMEOW_OPENCODE_MODEL": "mememeow/gpt-5.6-luna",
        "MEMEMEOW_OPENCODE_BASE_URL": "https://example.invalid/v1",
        "MEMEMEOW_OPENCODE_API_KEY": "test-key",
        "MEMEMEOW_OPENCODE_RUNTIME_ROOT": str(runtime),
        "MEMEMEOW_PYTHON": sys.executable,
    }
    result = subprocess.run(
        [str(project / "scripts" / "open-opencode.sh"), "--session", "ses_test"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = capture.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["exec", "-it"]
    opencode_index = lines.index("opencode")
    assert lines[opencode_index + 1 : opencode_index + 3] == ["/runtime/workspace", "--model"]
    assert lines[-2:] == ["--session", "ses_test"]


def test_opencode_launcher_uses_running_compose_runtime_for_session_list(tmp_path: Path):
    """Compose 服务运行时必须查询 named volume，而不是宿主旧数据库。"""
    project = Path(__file__).resolve().parent.parent
    capture = tmp_path / "capture-compose-list.txt"
    compose_file = tmp_path / "compose file.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == info ]]; then exit 0; fi\n"
        "if [[ \" $* \" == *\" ps --status running --services mememeow-agent-runtime \"* ]]; then\n"
        "  printf 'mememeow-agent-runtime\\n'\n"
        "  exit 0\n"
        "fi\n"
        f"printf '%s\\n' \"$@\" > {str(capture)!r}\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    host_runtime = tmp_path / "host-runtime"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "MEMEMEOW_AGENT_RUNTIME_MODE": "executor",
        "MEMEMEOW_COMPOSE_FILE": str(compose_file),
        "MEMEMEOW_OPENCODE_RUNTIME_ROOT": str(host_runtime),
        "MEMEMEOW_PYTHON": sys.executable,
    }
    environment.pop("MEMEMEOW_AGENT_EXECUTOR_TOKEN_FILE", None)
    result = subprocess.run(
        [str(project / "scripts" / "open-opencode.sh"), "--list", "--format", "json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = capture.read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["compose", "-f", str(compose_file)]
    assert "exec" in lines
    assert "-T" in lines
    assert "OPENCODE_DB=/runtime/opencode.db" in lines
    assert lines[-4:] == ["session", "list", "--format", "json"]
    assert str(host_runtime) not in "\n".join(lines)


def test_opencode_launcher_uses_container_model_for_compose_tui(tmp_path: Path):
    """Compose TUI 必须读取容器模型配置，并把未知参数原样交给 OpenCode。"""
    project = Path(__file__).resolve().parent.parent
    capture = tmp_path / "capture-compose-tui.txt"
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == info ]]; then exit 0; fi\n"
        "if [[ \" $* \" == *\" ps --status running --services mememeow-agent-runtime \"* ]]; then\n"
        "  printf 'mememeow-agent-runtime\\n'\n"
        "  exit 0\n"
        "fi\n"
        f"printf '%s\\n' \"$@\" > {str(capture)!r}\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "MEMEMEOW_AGENT_RUNTIME_MODE": "auto",
        "MEMEMEOW_COMPOSE_FILE": str(compose_file),
        "MEMEMEOW_PYTHON": sys.executable,
    }
    environment.pop("MEMEMEOW_AGENT_EXECUTOR_TOKEN_FILE", None)
    result = subprocess.run(
        [str(project / "scripts" / "open-opencode.sh"), "--session", "ses_test"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = capture.read_text(encoding="utf-8").splitlines()
    assert "-T" not in lines
    shell_index = lines.index("sh")
    assert lines[shell_index : shell_index + 4] == [
        "sh",
        "-lc",
        'exec opencode /runtime/workspace --model "$MEMEMEOW_OPENCODE_MODEL" "$@"',
        "open-opencode",
    ]
    assert lines[-2:] == ["--session", "ses_test"]


@pytest.mark.parametrize("runtime_mode", ["auto", "executor"])
def test_opencode_launcher_rejects_stale_host_runtime_when_compose_is_down(
    tmp_path: Path,
    runtime_mode: str,
):
    """Compose 模式不可在服务停机时静默回退到宿主历史 runtime。"""
    project = Path(__file__).resolve().parent.parent
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "MEMEMEOW_AGENT_RUNTIME_MODE": runtime_mode,
        "MEMEMEOW_COMPOSE_FILE": str(compose_file),
        "MEMEMEOW_PYTHON": sys.executable,
    }
    environment.pop("MEMEMEOW_AGENT_EXECUTOR_TOKEN_FILE", None)
    result = subprocess.run(
        [str(project / "scripts" / "open-opencode.sh"), "--list"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Compose Agent 服务 mememeow-agent-runtime 未运行" in result.stderr


def test_last_assistant_message_excludes_tool_content(tmp_path: Path):
    """session 消息解析兼容公开 API 结构且只使用最后 assistant 的 text parts。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    text = runner._last_assistant_text({"messages": [
        {"role": "assistant", "parts": [{"type": "text", "text": "旧内容"}]},
        {"role": "tool", "parts": [{"type": "text", "text": "工具结果"}]},
        {
            "info": {"role": "assistant"},
            "parts": [
                {"type": "reasoning", "text": "内部推理"},
                {"type": "text", "text": "{"},
                {"type": "text", "text": "}"},
                {"type": "step-finish"},
            ],
        },
    ]})
    assert text == "{}"


def test_process_error_diagnostic_reads_opencode_jsonl_error(tmp_path: Path):
    """非零退出时应从 OpenCode JSONL 中保留有限的上游错误诊断。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    stdout = json.dumps({
        "type": "error",
        "error": {"data": {"message": "Upstream request failed", "statusCode": 400}},
    }).encode()
    assert runner._process_error_diagnostic(stdout, b"") == "Upstream request failed (HTTP 400)"
    assert runner._process_error_diagnostic(stdout, b"CLI failed\n") == "CLI failed"


def test_persistent_task_keeps_stable_error_code_and_limited_diagnostic(tmp_path: Path):
    """任务服务保留 Agent 失败码，同时允许有限的安全诊断。"""
    service = PersistentTaskService(tmp_path / "tasks")

    def fail(payload, progress):
        raise RuntimeError("agent_process_failed: Upstream request failed (HTTP 400)")

    service.register("meme_context_generation", fail)
    service.start()
    task = service.submit("meme_context_generation", {})
    import time

    for _ in range(50):
        record = service.get(task.task_id)
        if record and record.status == "failed":
            break
        time.sleep(0.01)
    record = service.get(task.task_id)
    assert record.error == {
        "error": "agent_process_failed",
        "message": "agent_process_failed: Upstream request failed (HTTP 400)",
    }
    service.shutdown()


def test_persistent_tasks_recover_queued_and_interrupt_running(tmp_path: Path):
    """服务重启会恢复 queued 并将旧 running 记录标为 task_interrupted。"""
    root = tmp_path / "tasks"
    first = PersistentTaskService(root)
    queued = first.submit("known", {"value": 1})
    running = first.submit("other", {"value": 2})
    first.update(running.task_id, status="running")
    first._executor.shutdown(wait=False, cancel_futures=True)

    second = PersistentTaskService(root)
    handled: list[int] = []
    second.register("known", lambda payload, progress: handled.append(payload["value"]))
    second.start()
    import time

    for _ in range(50):
        if second.get(queued.task_id).status == "succeeded":
            break
        time.sleep(0.01)
    assert second.get(queued.task_id).status == "succeeded"
    assert handled == [1]
    interrupted = second.get(running.task_id)
    assert interrupted.status == "failed"
    assert interrupted.error["error"] == "task_interrupted"
    second.shutdown()


def test_slot_lock_is_cross_runner_and_does_not_reuse_busy_slot(tmp_path: Path):
    """两个应用 runner 不能同时持有同一个 slot 文件锁。"""
    settings = Settings(
        **{
            **make_settings(tmp_path).__dict__,
            "opencode_concurrency": 2,
        }
    )
    first = OpenCodeRunner(settings)
    second = OpenCodeRunner(settings)
    first.slots_root.mkdir(parents=True, exist_ok=True)
    first_slot, first_handle = first._acquire_slot()
    second_slot, second_handle = second._acquire_slot()
    assert first_slot != second_slot
    first._release_slot(first_slot, first_handle)
    second._release_slot(second_slot, second_handle)


def test_slot_acquire_after_shutdown_does_not_leak_semaphore(tmp_path: Path):
    """关闭中的 runner 拒绝新任务且不会遗留已占用的进程内配额。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    runner.shutdown()
    with pytest.raises(OpenCodeError) as error:
        runner._acquire_slot()
    assert error.value.code == "task_interrupted"
    assert runner._slot_semaphore.acquire(timeout=0.1)
    runner._slot_semaphore.release()
