"""运行时身份、存储初始化和 Compose 插值契约测试。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from scripts.runtime_init import RuntimeInitError, initialize_storage, parse_runtime_identity
from backend.storage_security import StorageRootError, validate_controlled_root
from backend.config import Settings


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """创建初始化服务使用的三个独立测试根目录。"""
    image_root = tmp_path / "images"
    runtime_root = tmp_path / "runtime"
    token_root = tmp_path / "token"
    image_root.mkdir()
    runtime_root.mkdir()
    token_root.mkdir()
    return image_root, runtime_root, token_root


def _initialize(image_root: Path, runtime_root: Path, token_root: Path) -> None:
    """以当前测试用户执行初始化，避免依赖固定宿主 UID/GID。"""
    initialize_storage(image_root, runtime_root, token_root, os.getuid(), os.getgid())


def test_runtime_identity_rejects_root_missing_and_non_numeric_values() -> None:
    """身份解析必须拒绝 root、缺失、零值和非数字输入。"""
    for uid, gid in (("0", "1000"), ("1000", "0"), (None, "1000"), ("1000", None), ("x", "1000"), ("1000", "1.5")):
        with pytest.raises(RuntimeInitError, match="runtime_.*_invalid"):
            parse_runtime_identity(uid, gid)
    assert parse_runtime_identity("1501", "1502") == (1501, 1502)


def test_runtime_init_normalizes_permissions_and_preserves_image_digest(tmp_path: Path) -> None:
    """历史图片归一化只修改身份和 mode，字节摘要必须保持不变。"""
    image_root, runtime_root, token_root = _roots(tmp_path)
    image = image_root / "legacy.png"
    image.write_bytes(b"legacy image bytes")
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    image.chmod(0o600)

    _initialize(image_root, runtime_root, token_root)

    assert hashlib.sha256(image.read_bytes()).hexdigest() == before
    assert stat.S_IMODE(image.stat().st_mode) == 0o600
    for directory in (
        image_root,
        image_root / ".staging",
        runtime_root,
        runtime_root / "home",
        runtime_root / "workspace",
        runtime_root / "task-results",
        token_root,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert directory.stat().st_uid == os.getuid()
        assert directory.stat().st_gid == os.getgid()


@pytest.mark.parametrize("kind", ["symlink", "special", "hardlink"])
def test_runtime_init_rejects_unsafe_nodes_without_changing_outside_bytes(tmp_path: Path, kind: str) -> None:
    """初始化必须拒绝链接、特殊节点和多链接文件，不能触及根外对象。"""
    image_root, runtime_root, token_root = _roots(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside bytes")
    outside_digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    target = image_root / "unsafe.png"
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "special":
        os.mkfifo(target)
    else:
        target.hardlink_to(outside)

    with pytest.raises(RuntimeInitError, match="runtime_storage_(symlink|special_node|hardlink)_forbidden"):
        initialize_storage(image_root, runtime_root, token_root, os.getuid(), os.getgid())
    assert hashlib.sha256(outside.read_bytes()).hexdigest() == outside_digest


def test_storage_root_fails_closed_on_symlink(tmp_path: Path) -> None:
    """业务启动路径不能把符号链接当作受控根目录。"""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(StorageRootError, match="symlink"):
        validate_controlled_root(link)


def test_settings_startup_fails_closed_for_runtime_root_symlink(tmp_path: Path) -> None:
    """Settings 启动门禁也必须拒绝 runtime 根目录的符号链接。"""
    data_root = tmp_path / "data"
    image_root = data_root / "images"
    data_root.mkdir()
    image_root.mkdir()
    real_runtime = tmp_path / "real-runtime"
    real_runtime.mkdir()
    runtime_link = data_root / "opencode"
    runtime_link.symlink_to(real_runtime, target_is_directory=True)
    settings = Settings(_env_file=None, data_root=data_root, image_root=image_root, opencode_runtime_root=runtime_link, database_url="postgresql+psycopg://example/example")
    with pytest.raises(StorageRootError, match="symlink"):
        settings.ensure_directories()


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    """创建只验证环境继承的 Docker CLI 替身。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "identity.txt"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f"printf '%s:%s\\n' \"$MEMEMEOW_RUNTIME_UID\" \"$MEMEMEOW_RUNTIME_GID\" > {capture}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir, capture


def test_start_sh_auto_and_explicit_runtime_identity(tmp_path: Path) -> None:
    """启动入口应自动导出当前身份，并允许显式非 root 值覆盖。"""
    bin_dir, capture = _fake_docker(tmp_path)
    base_env = os.environ.copy()
    base_env["PATH"] = f"{bin_dir}:{base_env['PATH']}"
    base_env.pop("MEMEMEOW_RUNTIME_UID", None)
    base_env.pop("MEMEMEOW_RUNTIME_GID", None)
    automatic = subprocess.run(["bash", "start.sh", "status"], cwd=Path(__file__).parent.parent, env=base_env, capture_output=True, text=True, check=False)
    assert automatic.returncode == 0, automatic.stderr
    assert capture.read_text(encoding="utf-8").strip() == f"{os.getuid()}:{os.getgid()}"

    base_env["MEMEMEOW_RUNTIME_UID"] = "2301"
    base_env["MEMEMEOW_RUNTIME_GID"] = "2302"
    explicit = subprocess.run(["bash", "start.sh", "status"], cwd=Path(__file__).parent.parent, env=base_env, capture_output=True, text=True, check=False)
    assert explicit.returncode == 0, explicit.stderr
    assert capture.read_text(encoding="utf-8").strip() == "2301:2302"


def test_compose_requires_explicit_identity_when_bypassing_start(tmp_path: Path) -> None:
    """绕过启动入口时 Compose 必须拒绝缺失身份并接受非 root 覆盖。"""
    if shutil.which("docker") is None:
        pytest.skip("未安装 Docker CLI")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# runtime identity intentionally omitted\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("MEMEMEOW_RUNTIME_UID", None)
    environment.pop("MEMEMEOW_RUNTIME_GID", None)
    missing = subprocess.run(
        ["docker", "compose", "--env-file", str(empty_env), "config", "--quiet"],
        cwd=Path(__file__).parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "MEMEMEOW_RUNTIME_" in missing.stderr

    environment["MEMEMEOW_RUNTIME_UID"] = "2401"
    environment["MEMEMEOW_RUNTIME_GID"] = "2402"
    valid = subprocess.run(
        ["docker", "compose", "--env-file", str(empty_env), "config", "--quiet"],
        cwd=Path(__file__).parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr


def test_compose_uses_project_generated_names_and_stable_service_dns() -> None:
    """Agent/Visual 不固定真实容器名，多 project 仍通过 service key 保持内部 DNS。"""
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "container_name:" not in compose
    assert "mememeow-agent-runtime:" in compose
    assert "mememeow-visual:" in compose
    assert "http://mememeow-agent-runtime:8277" in compose
    assert "http://mememeow-visual:8276" in compose
    generated = [f"{project}-mememeow-agent-runtime-1" for project in ("project-a", "project-b")]
    assert generated[0] != generated[1]


@pytest.mark.skipif(os.getenv("MEMEMEOW_RUNTIME_IDENTITY_E2E") != "1", reason="显式设置 MEMEMEOW_RUNTIME_IDENTITY_E2E=1 才运行 Compose 身份验收")
def test_compose_agent_reads_new_image_as_non_root_with_read_only_mount(tmp_path: Path) -> None:
    """Compose Agent 以非固定 UID 读取初始化后的图片且不能写入只读挂载。"""
    if shutil.which("docker") is None:
        pytest.skip("未安装 Docker CLI")
    image_root = tmp_path / "images"
    image_root.mkdir()
    env_file = tmp_path / "empty.env"
    env_file.write_text("# E2E uses explicit process environment\n", encoding="utf-8")
    project_name = f"mememeow-runtime-{uuid.uuid4().hex[:12]}"
    compose_base = ["docker", "compose", "--project-name", project_name, "--env-file", str(env_file)]
    environment = os.environ.copy()
    environment.update(
        {
            "MEMEMEOW_RUNTIME_UID": "1501",
            "MEMEMEOW_RUNTIME_GID": "1502",
            "MEMEMEOW_IMAGE_ROOT_HOST": str(image_root),
        }
    )
    try:
        started = subprocess.run(
            [*compose_base, "up", "-d", "--build", "runtime-init", "mememeow-agent-runtime"],
            cwd=Path(__file__).parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert started.returncode == 0, started.stderr[-4000:]
        uploaded = subprocess.run(
            [
                *compose_base,
                "run",
                "--rm",
                "--no-deps",
                "mememeow",
                "python",
                "-c",
                "import uuid; from backend.database import BlobStore, ScopeContext; store = BlobStore(root='/app/data/images', scope=ScopeContext('local'), local=True); staged = store.stage_bytes(b'uploaded-by-api-fixture', token=uuid.uuid4()); store.link_move(staged, 'uploaded.png')",
            ],
            cwd=Path(__file__).parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert uploaded.returncode == 0, uploaded.stderr[-4000:]
        uploaded_path = image_root / "uploaded.png"
        assert stat.S_IMODE(uploaded_path.stat().st_mode) == 0o600
        assert uploaded_path.stat().st_uid == 1501
        assert uploaded_path.stat().st_gid == 1502
        checked = subprocess.run(
            [
                *compose_base,
                "run",
                "--rm",
                "--no-deps",
                "mememeow-agent-runtime",
                "sh",
                "-lc",
                'test "$(id -u)" = 1501 && test "$(id -g)" = 1502 && python3 -c \'from pathlib import Path; assert Path("/images/uploaded.png").read_bytes() == b"uploaded-by-api-fixture"\' && test -w /runtime/home && ! touch /images/should-not-write.png',
            ],
            cwd=Path(__file__).parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr[-4000:]
    finally:
        # runtime-init 按设计把 bind mount 归属改为目标 UID；测试结束前用受控 API
        # 镜像的 root 一次性恢复临时目录所有权，避免 pytest 无法清理测试夹具。
        subprocess.run(
            [
                *compose_base,
                "run",
                "--rm",
                "--no-deps",
                "--user",
                "0:0",
                "mememeow",
                "sh",
                "-lc",
                f"chown -R {os.getuid()}:{os.getgid()} /app/data/images",
            ],
            cwd=Path(__file__).parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        subprocess.run([*compose_base, "down", "-v", "--remove-orphans"], cwd=Path(__file__).parent.parent, env=environment, capture_output=True, text=True, timeout=120, check=False)


@pytest.mark.parametrize(
    ("uid", "gid"),
    [("0", "1000"), ("1000", "0"), ("not-a-number", "1000"), ("1000", "-2")],
)
def test_start_sh_rejects_invalid_runtime_identity(tmp_path: Path, uid: str, gid: str) -> None:
    """启动入口在 Docker 调用前拒绝 root 和非法身份。"""
    bin_dir, _capture = _fake_docker(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
    environment["MEMEMEOW_RUNTIME_UID"] = uid
    environment["MEMEMEOW_RUNTIME_GID"] = gid
    result = subprocess.run(["bash", "start.sh", "status"], cwd=Path(__file__).parent.parent, env=environment, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "MEMEMEOW_RUNTIME_" in result.stderr
