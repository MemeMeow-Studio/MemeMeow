"""dotenv 模板同步脚本的合并规则与安全写入测试。"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from scripts.sync_env import merge_env_text, sync_env


def test_merge_uses_template_structure_and_preserves_raw_values(tmp_path: Path) -> None:
    """同名字段只移植原始值，模板的顺序、注释、默认值和换行保持不变。"""
    example_path = tmp_path / ".env.example"
    env_path = tmp_path / ".env"
    example = "# group\nA=template # keep\nB=default\nMULTI=default\n"
    current = "B='old # value' # discard\nA=${OTHER}\nMULTI=\"first\nsecond\"\n"

    merged = merge_env_text(example, current, example_path=example_path, env_path=env_path)

    assert merged == "# group\nA=${OTHER} # keep\nB='old # value'\nMULTI=\"first\nsecond\"\n"


def test_sync_creates_env_from_template_with_safe_permissions(tmp_path: Path) -> None:
    """目标不存在时直接复制模板语义，并把新文件权限限制为当前用户读写。"""
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("# template\nA=default\n", encoding="utf-8")

    assert sync_env(example, target) == target

    assert target.read_text(encoding="utf-8") == "# template\nA=default\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_sync_appends_only_final_extra_definitions_in_original_order(tmp_path: Path) -> None:
    """模板外字段按首次出现顺序追加，重复字段只保留最后一次生效定义。"""
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("A=default\n", encoding="utf-8")
    target.write_text("EXTRA_B=first\nEXTRA_A=one\nEXTRA_B=final # keep\nA=local\n", encoding="utf-8")

    sync_env(example, target)

    assert target.read_text(encoding="utf-8") == (
        "A=local\n\nEXTRA_B=final # keep\nEXTRA_A=one\n"
    )


def test_sync_is_idempotent(tmp_path: Path) -> None:
    """对已同步文件再次运行不会产生空行、排序或内容漂移。"""
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("# group\nA=default\nB=default\n", encoding="utf-8")
    target.write_text("B=local\nEXTRA=value\n", encoding="utf-8")

    sync_env(example, target)
    first = target.read_bytes()
    sync_env(example, target)

    assert target.read_bytes() == first


def test_sync_preserves_crlf_from_template(tmp_path: Path) -> None:
    """磁盘读写不应把模板的 CRLF 换行静默转换为 LF。"""
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_bytes(b"A=default\r\nB=default\r\n")
    target.write_bytes(b"A=local\r\nEXTRA=value\r\n")

    sync_env(example, target)

    assert target.read_bytes() == b"A=local\r\nB=default\r\n\r\nEXTRA=value\r\n"


def test_merge_preserves_no_assignment_semantics(tmp_path: Path) -> None:
    """合法的无等号声明保持 None 语义，模板声明也能接收已有的普通值。"""
    example_path = tmp_path / ".env.example"
    env_path = tmp_path / ".env"

    assert merge_env_text(
        "# group\nA=default # keep\n",
        "export A\n",
        example_path=example_path,
        env_path=env_path,
    ) == "# group\nA # keep\n"
    assert merge_env_text(
        "# group\nA # keep\n",
        "export A='local value'\n",
        example_path=example_path,
        env_path=env_path,
    ) == "# group\nA='local value' # keep\n"


def test_sync_rejects_invalid_env_without_modifying_it(tmp_path: Path) -> None:
    """旧配置含语法错误时中止同步，避免在覆盖时静默丢失无法识别的字段。"""
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("A=default\n", encoding="utf-8")
    original = b"A=local\nBROKEN LINE\n"
    target.write_bytes(original)

    with pytest.raises(ValueError, match="无法解析"):
        sync_env(example, target)

    assert target.read_bytes() == original


def test_sync_rejects_symlink_target(tmp_path: Path) -> None:
    """输出路径为符号链接时拒绝写入，防止脚本越过调用者指定的目标。"""
    example = tmp_path / ".env.example"
    actual = tmp_path / "actual.env"
    target = tmp_path / ".env"
    example.write_text("A=default\n", encoding="utf-8")
    actual.write_text("A=local\n", encoding="utf-8")
    target.symlink_to(actual)

    with pytest.raises(ValueError, match="符号链接"):
        sync_env(example, target)

    assert actual.read_text(encoding="utf-8") == "A=local\n"
