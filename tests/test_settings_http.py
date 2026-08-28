"""Settings HTTP 路由、授权和兼容边界测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api
import backend.settings_http as settings_http
from backend.config import Settings
from backend.scope import LocalScopeResolver
from backend.settings_http import ConcurrencyUpdateRequest, settings_router


def _route_snapshot(application: FastAPI) -> list[tuple[str, tuple[str, ...], str, bool, tuple[str, ...]]]:
    """提取 Settings 路由的稳定元数据，供模块级和宿主应用对照。"""
    paths = {"/settings", "/backend/settings", "/backend/settings/concurrency"}
    return [
        (
            route.path,
            tuple(sorted(route.methods or ())),
            route.name,
            route.include_in_schema,
            tuple(route.tags or ()),
        )
        for route in application.routes
        if getattr(route, "path", None) in paths
    ]


def _settings_app(tmp_path: Path, *, token: str | None = "settings-test-token") -> tuple[FastAPI, Path]:
    """创建不启动数据库生命周期的最小 Settings HTTP 测试应用。"""
    settings = Settings(
        _env_file=None,
        data_root=tmp_path / "data",
        database_url="postgresql+psycopg://example/example",
        opencode_concurrency=2,
        agent_scope_concurrency=1,
        agent_backpressure=8,
        settings_admin_token=token,
    )
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("# preserve\nOTHER=value\nMEMEMEOW_OPENCODE_CONCURRENCY=2\n", encoding="utf-8")
    settings._dotenv_path = dotenv_path

    application = FastAPI()
    application.router.routes.extend(settings_router.routes)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_request, _exc: RequestValidationError) -> JSONResponse:
        """将最小应用的校验失败收敛为公共 API 的 400 错误形状。"""
        return JSONResponse(status_code=400, content={"error": "invalid_request", "message": "请求参数校验失败"})

    @application.exception_handler(HTTPException)
    async def http_error_handler(_request, exc: HTTPException) -> JSONResponse:
        """复现公共 API 对 Settings HTTPException 的脱敏响应处理。"""
        detail = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else {"error": "http_error", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)

    application.state.settings = settings
    application.state.opencode = SimpleNamespace(runtime_probe=lambda: {"verified": True})
    application.state.search_engine = SimpleNamespace(has_cache=lambda: True)
    return application, dotenv_path


def test_settings_route_snapshot_is_preserved_for_module_and_custom_app() -> None:
    """模块级入口和显式 scope 宿主都保留相同的六个 Settings 路由。"""
    expected = [
        ("/settings", ("GET",), "backend_settings", False, ("system",)),
        ("/backend/settings", ("GET",), "backend_settings", True, ("system",)),
        ("/settings", ("PATCH",), "update_backend_settings", False, ("system",)),
        ("/backend/settings", ("PATCH",), "update_backend_settings", True, ("system",)),
        ("/backend/settings/concurrency", ("POST",), "update_backend_concurrency", True, ("system",)),
        ("/backend/settings", ("POST",), "update_backend_concurrency", False, ("system",)),
    ]
    assert _route_snapshot(api.app) == expected
    assert _route_snapshot(api.create_app(scope_resolver=LocalScopeResolver("local"))) == expected
    openapi_paths = api.app.openapi()["paths"]
    assert sorted(openapi_paths["/backend/settings"]) == ["get", "patch"]
    assert sorted(openapi_paths["/backend/settings/concurrency"]) == ["post"]
    assert "/settings" not in openapi_paths


def test_settings_request_model_keeps_aliases_and_strict_positive_integer() -> None:
    """四个旧输入别名继续映射到同一正整数并发字段。"""
    for field in ("opencode_concurrency", "agent_concurrency", "concurrency", "value"):
        assert ConcurrencyUpdateRequest.model_validate({field: 128}).opencode_concurrency == 128
    for payload in (
        {"value": True},
        {"value": "4"},
        {"value": 0},
        {"value": -1},
        {"value": 4, "unexpected": True},
        {},
    ):
        with pytest.raises(ValidationError):
            ConcurrencyUpdateRequest.model_validate(payload)


def test_invalid_http_payload_does_not_call_dotenv_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """请求体校验失败时不能进入 token 或 dotenv 写入边界。"""
    application, _dotenv_path = _settings_app(tmp_path)
    called = False

    def fail_if_called(*_args, **_kwargs):
        """若校验顺序错误则让测试直接失败。"""
        nonlocal called
        called = True
        raise AssertionError("invalid payload reached dotenv writer")

    monkeypatch.setattr(settings_http, "update_dotenv_concurrency", fail_if_called)
    response = TestClient(application).patch(
        "/backend/settings",
        json={"value": "4"},
        headers={"X-Settings-Admin-Token": "settings-test-token"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert called is False


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Settings-Admin-Token": "settings-test-token"},
        {"X-MemeMeow-Settings-Token": "settings-test-token"},
        {"Authorization": "Bearer settings-test-token"},
        {"Authorization": "bEaReR settings-test-token"},
    ],
)
def test_settings_authorization_headers_remain_compatible(tmp_path: Path, headers: dict[str, str]) -> None:
    """兼容的两个 Header 和大小写不敏感 Bearer 都可以保存待重启值。"""
    application, dotenv_path = _settings_app(tmp_path)
    response = TestClient(application).patch("/backend/settings", json={"value": 4}, headers=headers)
    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert response.json()["pending"] == {"opencode_concurrency": 4}
    assert response.json()["restart_required"] is True
    assert "settings_admin_token" not in response.json()
    assert dotenv_path.read_text(encoding="utf-8").endswith("MEMEMEOW_OPENCODE_CONCURRENCY=4\n")


def test_settings_header_precedence_and_invalid_credentials_fail_closed(tmp_path: Path) -> None:
    """主 Header 存在错误值时不能回退到第二 Header 或 Bearer。"""
    application, dotenv_path = _settings_app(tmp_path)
    client = TestClient(application)
    original = dotenv_path.read_text(encoding="utf-8")
    for headers in (
        {},
        {"X-Settings-Admin-Token": "wrong"},
        {"X-MemeMeow-Settings-Token": "wrong"},
        {"Authorization": "Basic settings-test-token"},
        {"X-Settings-Admin-Token": "wrong", "X-MemeMeow-Settings-Token": "settings-test-token"},
        {"X-Settings-Admin-Token": "wrong", "Authorization": "Bearer settings-test-token"},
    ):
        response = client.patch("/backend/settings", json={"value": 4}, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"] == "settings_forbidden"
        assert dotenv_path.read_text(encoding="utf-8") == original


def test_settings_environment_override_is_rejected_before_file_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境覆盖存在时即使授权成功也不能修改 dotenv。"""
    application, dotenv_path = _settings_app(tmp_path)
    original = dotenv_path.read_text(encoding="utf-8")
    original_mtime = dotenv_path.stat().st_mtime_ns
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", "9")
    response = TestClient(application).post(
        "/backend/settings/concurrency",
        json={"value": 4},
        headers={"Authorization": "Bearer settings-test-token"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "settings_environment_override"
    assert dotenv_path.read_text(encoding="utf-8") == original
    assert dotenv_path.stat().st_mtime_ns == original_mtime


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/settings", "get"),
        ("/backend/settings", "get"),
    ],
)
def test_settings_get_legacy_and_canonical_paths_return_same_masked_projection(tmp_path: Path, path: str, method: str) -> None:
    """canonical 与 legacy GET 返回同一脱敏 Settings 投影。"""
    application, _dotenv_path = _settings_app(tmp_path)
    response = getattr(TestClient(application), method)(path)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "config_version",
        "deployment",
        "deployment_only",
        "editable",
        "effective",
        "effective_value",
        "environment_overrides",
        "pending",
        "pending_value",
        "read_only",
        "readonly",
        "restart_required",
        "safe_adjustable",
        "settings_version",
    }
    assert body["effective"]["opencode_concurrency"] == 2
    assert body["editable"]["opencode_concurrency"]["maximum"] is None
    assert body["readonly"]["runtime_ready"] is True
    assert "settings_admin_token" not in body
    assert "dotenv_path" not in body


def test_settings_legacy_post_and_unsupported_method_keep_contract(tmp_path: Path) -> None:
    """隐藏 POST 兼容入口继续工作，其它 method 仍返回 405。"""
    application, _dotenv_path = _settings_app(tmp_path)
    client = TestClient(application)
    response = client.post(
        "/backend/settings",
        json={"agent_concurrency": 3},
        headers={"X-MemeMeow-Settings-Token": "settings-test-token"},
    )
    assert response.status_code == 200
    assert response.json()["pending"] == {"opencode_concurrency": 3}
    assert client.put("/backend/settings").status_code == 405


def test_settings_update_errors_keep_stable_mappings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置值错误和文件写入错误分别映射到既有稳定错误。"""
    application, _dotenv_path = _settings_app(tmp_path)
    headers = {"X-Settings-Admin-Token": "settings-test-token"}

    def invalid(*_args, **_kwargs):
        """模拟并发配置校验失败。"""
        raise ValueError("opencode_concurrency_out_of_range")

    monkeypatch.setattr(settings_http, "update_dotenv_concurrency", invalid)
    response = TestClient(application).patch("/settings", json={"value": 4}, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] == "settings_update_invalid"

    def failed(*_args, **_kwargs):
        """模拟受控配置文件无法原子写入。"""
        raise OSError("write failed")

    monkeypatch.setattr(settings_http, "update_dotenv_concurrency", failed)
    response = TestClient(application).post("/backend/settings/concurrency", json={"value": 4}, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"] == "settings_update_failed"


def test_api_reexports_settings_http_symbols_without_reverse_import() -> None:
    """旧 api import 保持可用，Settings HTTP 模块不反向依赖 api。"""
    assert api.ConcurrencyUpdateRequest is ConcurrencyUpdateRequest
    assert api.backend_settings.__module__ == "backend.settings_http"
    assert api.update_backend_settings.__module__ == "backend.settings_http"
    assert api.update_backend_concurrency.__module__ == "backend.settings_http"
    source = Path(settings_http.__file__).read_text(encoding="utf-8")
    assert "import api" not in source
    assert "from api" not in source


def test_unconfigured_settings_admin_token_stays_read_only(tmp_path: Path) -> None:
    """未配置设置管理 token 时更新接口保持 403 且不写文件。"""
    application, dotenv_path = _settings_app(tmp_path, token=None)
    original = dotenv_path.read_text(encoding="utf-8")
    response = TestClient(application).patch(
        "/backend/settings",
        json={"value": 4},
        headers={"X-Settings-Admin-Token": "settings-test-token"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "settings_forbidden"
    assert dotenv_path.read_text(encoding="utf-8") == original
