"""打开 MemeMeow 专用 OpenCode runtime，供开发者检查历史图片分析会话。"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import Settings  # noqa: E402
from backend.opencode import OpenCodeError, OpenCodeRunner  # noqa: E402


COMPOSE_SERVICE = "mememeow-agent-runtime"
CONTAINER_WORKSPACE = "/runtime/workspace"
CONTAINER_ENVIRONMENT = {
    "OPENCODE_DB": "/runtime/opencode.db",
    "OPENCODE_CONFIG": "/runtime/workspace/opencode.json",
    "OPENCODE_CONFIG_DIR": "/runtime/workspace/.opencode",
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
}


def build_parser() -> argparse.ArgumentParser:
    """创建启动器参数解析器；未知参数原样交给 OpenCode TUI。"""
    parser = argparse.ArgumentParser(description="打开 MemeMeow OpenCode 会话检查界面")
    parser.add_argument("--list", action="store_true", help="在终端列出历史 session 后退出")
    return parser


def _compose_file() -> Path:
    """返回 Compose 配置绝对路径，供任意工作目录下的诊断调用复用。"""
    configured = Path(os.environ.get("MEMEMEOW_COMPOSE_FILE", "docker-compose.yml")).expanduser()
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    return configured.resolve()


def _docker_command_for_execution(command: list[str]) -> list[str]:
    """选择直接 Docker 或 ``sg docker``，兼容尚未刷新组权限的宿主会话。"""
    try:
        probe = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        if probe.returncode == 0:
            return command
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ["sg", "docker", "-c", shlex.join(command)]


def _compose_command(*arguments: str) -> list[str]:
    """构造固定 Compose 项目的命令，避免依赖调用者当前目录。"""
    return ["docker", "compose", "-f", str(_compose_file()), *arguments]


def _compose_runtime_running() -> bool:
    """检查权威 Agent 服务是否运行；调用场景为选择 session 数据源。"""
    if not _compose_file().is_file():
        return False
    command = _docker_command_for_execution(
        _compose_command("ps", "--status", "running", "--services", COMPOSE_SERVICE)
    )
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and COMPOSE_SERVICE in result.stdout.splitlines()


def _compose_runtime_selected(runtime_mode: str) -> bool:
    """判断启动器是否应以 Compose 为权威来源；显式 host/docker 保留旧路径。"""
    return runtime_mode in {"auto", "executor"} and _compose_file().is_file()


def _compose_launcher_command(*, session_list: bool, passthrough: list[str]) -> list[str]:
    """构造容器内 OpenCode 入口，连接 named volume 中的共享配置和数据库。"""
    arguments = ["exec"]
    if session_list:
        # 非交互查询关闭 TTY，便于重定向、测试和脚本消费 JSON。
        arguments.append("-T")
    arguments.extend(("--workdir", CONTAINER_WORKSPACE))
    for key, value in CONTAINER_ENVIRONMENT.items():
        arguments.extend(("--env", f"{key}={value}"))
    arguments.append(COMPOSE_SERVICE)
    if session_list:
        arguments.extend(("opencode", "session", "list", *passthrough))
    else:
        # 模型配置只存在于容器环境；用固定 shell 程序读取，透传参数仍保持独立 argv。
        arguments.extend(
            (
                "sh",
                "-lc",
                'exec opencode /runtime/workspace --model "$MEMEMEOW_OPENCODE_MODEL" "$@"',
                "open-opencode",
                *passthrough,
            )
        )
    return _docker_command_for_execution(_compose_command(*arguments))


def main(argv: list[str] | None = None) -> int:
    """准备共享 runtime 并以同一 DB 启动 TUI，调用场景为本地诊断历史 session。"""
    parser = build_parser()
    known, passthrough = parser.parse_known_args(argv)
    settings = Settings.from_env(PROJECT_ROOT / ".env")

    # Compose named volume 是部署态权威数据源；显式 host/docker 才允许绕过它。
    if _compose_runtime_selected(settings.agent_runtime_mode):
        if not _compose_runtime_running():
            parser.exit(1, f"Compose Agent 服务 {COMPOSE_SERVICE} 未运行，无法打开共享 OpenCode runtime。\n")
        command = _compose_launcher_command(session_list=known.list, passthrough=passthrough)
        try:
            os.execvp(command[0], command)
        except OSError as exc:
            parser.exit(1, f"无法进入 Compose OpenCode runtime: {exc}\n")

    runner = OpenCodeRunner(settings, project_root=PROJECT_ROOT)
    try:
        runner.prepare_runtime()
    except OpenCodeError as exc:
        parser.exit(1, f"OpenCode runtime 初始化失败 [{exc.code}]: {exc}\n")

    if runner.executor_mode:
        parser.exit(1, f"Compose Agent 服务 {COMPOSE_SERVICE} 未运行，无法打开共享 OpenCode runtime。\n")

    # Docker 模式只使用镜像内的固定命令，避免把宿主绝对路径传入容器。
    executable = "opencode" if runner.docker_mode else str(settings.opencode_executable or "opencode")
    environment = runner._allowed_container_environment(0) if runner.docker_mode else runner.build_environment()
    if known.list:
        command = [executable, "session", "list", *passthrough]
    else:
        command = [
            executable,
            str(runner.workspace),
            "--model",
            str(settings.opencode_model),
            *passthrough,
        ]
    try:
        os.chdir(runner.workspace)
        if runner.docker_mode:
            # 诊断入口沿用共享容器和同一 runtime DB，不会启动第二个容器。
            container_command = list(command)
            if not known.list:
                # 宿主 workspace 路径不能传给容器；runtime 在容器内固定挂载到 /runtime。
                container_command[1] = "/runtime/workspace"
            docker_command = runner._container_exec(
                *container_command,
                environment=runner._allowed_container_environment(0),
                workdir="/runtime/workspace",
            )
            if not known.list:
                # 旧 Docker 兼容模式同样需要真实终端，否则 OpenCode TUI 无法接收输入。
                docker_command.insert(2, "-it")
            docker_command = runner._docker_command_for_execution(docker_command)
            launcher_environment = {"PATH": os.environ.get("PATH", ""), **environment}
            os.execvpe(docker_command[0], docker_command, launcher_environment)
        else:
            os.execvpe(executable, command, environment)
    except OSError as exc:
        parser.exit(1, f"无法启动 OpenCode: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
