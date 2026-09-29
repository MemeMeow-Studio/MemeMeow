# 验证共享名称文本规则及等待状态，不使用数据库或模型替代对象。

import hashlib

import pytest

from backend.embedding_input import TEXT_INPUT_VERSION, build_text_input, filename_text


@pytest.mark.parametrize("length", [32, 40, 64])
def test_complete_hash_waits_without_context(length):
    """完整十六进制名称无语境时等待文本，图片输入不受此规则影响。"""
    result = build_text_input("A" * length + ".PNG", {}, "pending")
    assert result.status == "waiting_for_text"
    assert result.fingerprint is None
    assert result.text == ""


@pytest.mark.parametrize("name", ["", " \t ", "---___...", " _ - . .png"])
def test_separators_wait(name):
    """空白及纯分隔符不贡献名称文本。"""
    assert build_text_input(name, {}, "pending").status == "waiting_for_text"


@pytest.mark.parametrize("name, expected", [("abczqwerty.png", "abczqwerty"), ("下班啦.JPG", "下班啦"), ("ＡＢＣ　猫.png", "ABC 猫"), ("a" * 31, "a" * 31)])
def test_readable_names(name, expected):
    """普通名称与 Unicode 名称在 pending 时也能生成版本化输入。"""
    assert filename_text(name) == expected
    result = build_text_input(name, {}, "pending")
    assert result.text == f"名称：{expected}"
    assert result.fingerprint == hashlib.sha256(f"{TEXT_INPUT_VERSION}\n{result.text}".encode()).hexdigest()


def test_context_can_supply_text_after_name_filter():
    """名称过滤后仍保留有效语境白名单，并排除研究辅助内容。"""
    context = {"summary": "猫咪挥手", "search_queries": ["不可进入输入"], "uncertainties": ["尚未确认"]}
    result = build_text_input("a" * 32, context, "ready")
    assert result.text == "摘要：猫咪挥手"
    assert result.status == "eligible"


def test_waiting_is_recomputed_from_current_input():
    """更新名称或有效语境后重新取得资格，pending 不使用语境内容。"""
    assert build_text_input("a" * 32, {"summary": "猫咪"}, "pending").status == "waiting_for_text"
    assert build_text_input("今天不想上班", {}, "pending").status == "eligible"
    assert build_text_input("a" * 32, {"summary": "猫咪"}, "partial").status == "eligible"


def test_invalid_context_and_invalid_identity():
    """无效语境允许使用名称，图片身份异常阻止全部文本输入。"""
    assert build_text_input("猫咪", {"subjects": 123}, "ready").text == "名称：猫咪"
    assert build_text_input("猫咪", {}, "repair_required").status == "repair_required"
