"""反向图片 provider 响应结构的离线契约测试。"""

from __future__ import annotations

import inspect

import pytest

from api import internal_reverse_image_search
from backend.reverse_image import ReverseImageError, _is_empty


def test_provider_empty_result_requires_declared_result_container() -> None:
    """明确声明空候选列表才是合法空结果。"""
    assert _is_empty({"search_metadata": {"status": "Success"}, "visual_matches": []}) is True


@pytest.mark.parametrize("response", ({}, {"search_metadata": {"status": "Success"}, "unexpected": []}, {"visual_matches": {}}))
def test_provider_unknown_or_wrong_result_shape_is_invalid(response: dict[str, object]) -> None:
    """完全未知结构或候选字段类型错误不能被当作空结果缓存。"""
    with pytest.raises(ReverseImageError) as error:
        _is_empty(response)
    assert error.value.code == "reverse_image_provider_invalid"


def test_internal_reverse_image_request_id_and_digest_are_optional() -> None:
    """内部 callback 兼容旧显式 ID，同时允许服务端生成权威身份。"""
    parameters = inspect.signature(internal_reverse_image_search).parameters
    assert parameters["request_id"].default.default is None
    assert parameters["input_digest"].default.default is None
