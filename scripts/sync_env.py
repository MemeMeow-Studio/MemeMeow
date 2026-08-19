"""项目维护脚本：按 ``.env.example`` 的结构同步本地 ``.env``。"""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

from dotenv.parser import Binding, parse_stream


def _parse_bindings(content: str, source: Path) -> list[Binding]:
    """解析 dotenv 文本并拒绝语法错误，供合并前的数据校验调用。"""
    bindings = list(parse_stream(StringIO(content)))
    errors = [str(binding.original.line) for binding in bindings if binding.error]
    if errors:
        lines = ", ".join(errors)
        raise ValueError(f"{source} 存在无法解析的 dotenv 内容（起始行：{lines}）")
    return bindings


def _assignment_parts(binding: Binding) -> tuple[str, str, str] | None:
    """拆出赋值语句的前缀、原始值和后缀，避免改变引号及变量插值语义。"""
    text = binding.original.string
    quote: str | None = None
    equals_at: int | None = None
    for index, character in enumerate(text):
        if character in {"'", '"'}:
            escaped = index > 0 and text[index - 1] == "\\"
            if not escaped and (quote is None or quote == character):
                quote = None if quote == character else character
        elif character == "=" and quote is None:
            equals_at = index
            break
        elif character in {"\r", "\n"} and quote is None and text[:index].strip():
            break
    if equals_at is None:
        return None

    value_start = equals_at + 1
    while value_start < len(text) and text[value_start].isspace() and text[value_start] not in {"\r", "\n"}:
        value_start += 1

    if value_start < len(text) and text[value_start] in {"'", '"'}:
        delimiter = text[value_start]
        value_end = value_start + 1
        while value_end < len(text):
            if text[value_end] == delimiter:
                backslashes = 0
                cursor = value_end - 1
                while cursor >= value_start and text[cursor] == "\\":
                    backslashes += 1
                    cursor -= 1
                if backslashes % 2 == 0:
                    value_end += 1
                    break
            value_end += 1
    else:
        line_end = len(text)
        for newline in ("\r", "\n"):
            position = text.find(newline, value_start)
            if position >= 0:
                line_end = min(line_end, position)
        unquoted = text[value_start:line_end]
        comment = re.search(r"[^\S\r\n]+#", unquoted)
        value_end = value_start + (comment.start() if comment else len(unquoted.rstrip()))
    return text[:value_start], text[value_start:value_end], text[value_end:]


def _binding_head(binding: Binding) -> tuple[str, str]:
    """拆出模板字段名及其后缀，供无等号声明和普通赋值相互转换。"""
    match = re.match(r"\s*(?:export[^\S\r\n]+)?(?:'[^']+'|[^=#\s]+)", binding.original.string)
    if match is None:
        raise ValueError(f"无法定位 dotenv 字段：{binding.key}")
    return binding.original.string[: match.end()], binding.original.string[match.end() :]


def _without_leading_blank_lines(text: str) -> str:
    """移除解析器归入字段的前置空行，供模板外字段统一追加到文件尾部。"""
    return re.sub(r"\A(?:[^\S\r\n]*(?:\r\n|\n|\r))+", "", text)


def _newline_for(content: str) -> str:
    """选择模板使用的换行符，供追加字段时保持文件风格一致。"""
    return "\r\n" if "\r\n" in content else "\n"


def _read_text(path: Path) -> str:
    """读取 dotenv 原文且不转换换行符，供同步过程保留模板的文件风格。"""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def merge_env_text(example: str, current: str, *, example_path: Path, env_path: Path) -> str:
    """以模板结构合并 dotenv 文本，返回可直接写入目标文件的完整内容。"""
    example_bindings = _parse_bindings(example, example_path)
    current_bindings = _parse_bindings(current, env_path)

    current_by_key: dict[str, Binding] = {}
    for binding in current_bindings:
        if binding.key is not None:
            # 字典保留字段首次出现的位置，同时让最后一次定义提供最终生效值。
            current_by_key[binding.key] = binding

    template_keys = {binding.key for binding in example_bindings if binding.key is not None}
    output: list[str] = []
    for binding in example_bindings:
        source = current_by_key.get(binding.key) if binding.key is not None else None
        if source is None:
            output.append(binding.original.string)
            continue

        template_parts = _assignment_parts(binding)
        source_parts = _assignment_parts(source)
        if template_parts is None and source_parts is None:
            output.append(binding.original.string)
            continue
        if template_parts is None:
            head, suffix = _binding_head(binding)
            output.append(f"{head}={source_parts[1]}{suffix}")
            continue
        prefix, _, suffix = template_parts
        if source_parts is None:
            head, _ = _binding_head(binding)
            output.append(f"{head}{suffix}")
        else:
            output.append(f"{prefix}{source_parts[1]}{suffix}")

    extras = [
        _without_leading_blank_lines(binding.original.string)
        for key, binding in current_by_key.items()
        if key not in template_keys
    ]
    if not extras:
        return "".join(output)

    newline = _newline_for(example)
    merged = "".join(output)
    if merged and not merged.endswith(("\n", "\r")):
        merged += newline
    if merged and not merged.endswith(newline * 2):
        merged += newline
    for extra in extras:
        merged += extra
        if not merged.endswith(("\n", "\r")):
            merged += newline
    return merged


def _validate_target(target: Path) -> None:
    """确认写入目标不会跟随符号链接或覆盖非普通文件。"""
    if target.is_symlink():
        raise ValueError(f"拒绝写入符号链接：{target}")
    if target.exists() and not target.is_file():
        raise ValueError(f"写入目标不是普通文件：{target}")
    for parent in (target.parent, *target.parent.parents):
        if parent == parent.parent:
            break
        if parent.is_symlink():
            raise ValueError(f"拒绝通过符号链接目录写入：{parent}")


def _atomic_write(target: Path, content: str) -> None:
    """以 ``0600`` 权限原子替换目标，避免密钥短暂暴露或留下半写文件。"""
    _validate_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def sync_env(example_path: str | Path = ".env.example", env_path: str | Path = ".env") -> Path:
    """读取模板和已有配置、完成合并并原子写回，供 CLI 或其他维护脚本调用。"""
    example = Path(example_path).expanduser()
    target = Path(env_path).expanduser()
    if example.resolve() == target.resolve():
        raise ValueError("模板与输出不能是同一个文件")
    if not example.is_file():
        raise FileNotFoundError(f"dotenv 模板不存在：{example}")
    _validate_target(target)
    example_text = _read_text(example)
    current_text = _read_text(target) if target.exists() else ""
    merged = merge_env_text(example_text, current_text, example_path=example, env_path=target)
    _atomic_write(target, merged)
    return target


def _parser() -> argparse.ArgumentParser:
    """构造命令行解析器，供入口函数和 CLI 测试复用。"""
    parser = argparse.ArgumentParser(description="以 .env.example 为蓝本同步 .env")
    parser.add_argument("--example", default=".env.example", help="dotenv 模板路径（默认：.env.example）")
    parser.add_argument("--env", default=".env", help="dotenv 输出路径（默认：.env）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行 dotenv 同步命令，并仅输出不含配置值的结果摘要。"""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        target = sync_env(arguments.example, arguments.env)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"已根据 {arguments.example} 生成 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
