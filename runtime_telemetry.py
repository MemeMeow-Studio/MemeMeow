# 公共运行观测模块，为 API 与 executor 提供独立的 OTLP 导出和内容保护。
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.threading import ThreadingInstrumentor
from opentelemetry.instrumentation.urllib import URLLibInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExportResult, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode

_CREDENTIAL_KEY = re.compile(r"(?i)(password|passwd|secret|token|authorization|cookie|api[_-]?key|credential)")
_CREDENTIAL_TEXT = re.compile(r"(?i)(?:bearer\s+[^\s\"\x27,;]+|(?:api[_-]?key|password|token|secret|authorization)\s*[=:]\s*[^\s,;]+)")
_BINARY_KEY = re.compile(r"(?i)(base64|image_url|image_data|file_content|binary)")
_PROPAGATOR = TraceContextTextMapPropagator()
_LOCK = Lock()
_RUNTIME: TelemetryRuntime | None = None


@dataclass(frozen=True)
class TelemetryConfig:
    """进程观测配置；endpoint 只由宿主环境提供。"""

    enabled: bool = False
    endpoint: str = ""
    service: str = "mememeow-api"
    environment: str = "development"

    @classmethod
    def from_env(cls, service: str = "mememeow-api") -> TelemetryConfig:
        """读取并校验宿主配置，配置错误在启动位置报告。"""
        enabled = os.getenv("MEMEMEOW_TELEMETRY_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("MEMEMEOW_TELEMETRY_ENABLED 必须为 true 或 false")
        endpoint = os.getenv("MEMEMEOW_TELEMETRY_ENDPOINT", "")
        if enabled == "true":
            parsed = urlsplit(endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("MEMEMEOW_TELEMETRY_ENDPOINT 必须为不包含凭据或查询参数的 HTTP(S) 地址")
        return cls(enabled == "true", endpoint.rstrip("/"), service, os.getenv("MEMEMEOW_TELEMETRY_ENVIRONMENT", "development"))


def protected_content(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """过滤结构化凭据与已知凭据值，保留 debug 文本和错误类别。"""
    if isinstance(value, Mapping):
        return {
            str(key): "[credential filtered]" if _CREDENTIAL_KEY.search(str(key)) else
            "[binary content omitted; use business file reference]" if _BINARY_KEY.search(str(key)) else
            protected_content(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [protected_content(item, secrets) for item in value]
    if isinstance(value, bytes):
        return {"content_status": "binary_reference_required", "size_bytes": len(value)}
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[credential filtered]")
        return _CREDENTIAL_TEXT.sub("[credential filtered]", result)
    return value


def environment_credentials() -> tuple[str, ...]:
    """取得进程中已配置的凭据值，供专用内容过滤使用。"""
    return tuple(value for key, value in os.environ.items() if _CREDENTIAL_KEY.search(key) and value)


def record_content(span: trace.Span, name: str, value: Any, *, secrets: tuple[str, ...] = ()) -> None:
    """向当前调查 span 写入已取得的结构化内容，不生成二进制副本。"""
    if span.is_recording():
        span.set_attribute(name, json.dumps(protected_content(value, secrets + environment_credentials()), ensure_ascii=False, default=str))
        span.set_attribute("content.source", "python_parsed")
        span.set_attribute("content.completeness", "available_fields")


class _ProtectedExporter(SpanExporter):
    """导出前保护自动 instrumentation 属性并记录实际发送状态。"""

    def __init__(self, delegate: SpanExporter):
        self.delegate = delegate
        self.last_success = 0.0
        self.last_failure = 0.0

    def export(self, spans: Any) -> SpanExportResult:
        """处理 SDK 已完成的 span；发送失败和业务结果分别记录。"""
        from opentelemetry.sdk.trace import ReadableSpan

        secrets = environment_credentials()
        cleaned = [ReadableSpan(
            name=span.name, context=span.context, parent=span.parent,
            resource=span.resource, attributes={**protected_content(dict(span.attributes or {}), secrets), "content.dropped_attributes": span.dropped_attributes, "content.dropped_events": span.dropped_events},
            events=[Event(event.name, protected_content(dict(event.attributes or {}), secrets), event.timestamp) for event in span.events],
            links=span.links, kind=span.kind, status=Status(span.status.status_code, protected_content(span.status.description, secrets)),
            start_time=span.start_time, end_time=span.end_time, instrumentation_scope=span.instrumentation_scope,
        ) for span in spans]
        result = self.delegate.export(cleaned)
        if result == SpanExportResult.SUCCESS:
            self.last_success = time.time()
        else:
            self.last_failure = time.time()
            logger.error("telemetry_trace_export_failed: OTLP exporter 返回 {}", result.name)
        return result

    def shutdown(self) -> None:
        """关闭 SDK 管理的 HTTP exporter。"""
        self.delegate.shutdown()


class _MetricExporter(OTLPMetricExporter):
    """在标准 metric exporter 上保留实际导出成功与失败时间。"""

    last_success = 0.0
    last_failure = 0.0

    def export(self, metrics_data: Any, timeout_millis: float = 10000, **kwargs: Any) -> MetricExportResult:
        """通过官方 exporter 发送 metrics 并报告发送状态。"""
        result = super().export(metrics_data, timeout_millis=timeout_millis, **kwargs)
        if result == MetricExportResult.SUCCESS:
            self.last_success = time.time()
        else:
            self.last_failure = time.time()
            logger.error("telemetry_metric_export_failed: OTLP exporter 返回 {}", result.name)
        return result


class TelemetryRuntime:
    """管理 SDK 生命周期，提供内存测量与后台导出状态。"""

    def __init__(self, config: TelemetryConfig):
        self.config = config
        resource = Resource.create({"service.name": config.service, "deployment.environment.name": config.environment})
        self.span_exporter = _ProtectedExporter(OTLPSpanExporter(endpoint=f"{config.endpoint}/v1/traces", timeout=5))
        self.tracer_provider = TracerProvider(resource=resource, sampler=ALWAYS_ON, span_limits=SpanLimits(max_attributes=1024, max_events=1024, max_attribute_length=None))
        self.tracer_provider.add_span_processor(BatchSpanProcessor(self.span_exporter, max_queue_size=2048, max_export_batch_size=128, schedule_delay_millis=2000, export_timeout_millis=5000))
        self.metric_exporter = _MetricExporter(endpoint=f"{config.endpoint}/v1/metrics", timeout=5)
        reader = PeriodicExportingMetricReader(self.metric_exporter, export_interval_millis=10000, export_timeout_millis=5000)
        self.meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = self.meter_provider.get_meter("mememeow.runtime")
        meter.create_observable_gauge("telemetry_export_last_success", unit="s", callbacks=[lambda options: [metrics.Observation(self.span_exporter.last_success, {"signal": "traces"}), metrics.Observation(self.metric_exporter.last_success, {"signal": "metrics"})]])
        meter.create_observable_gauge("telemetry_export_last_failure", unit="s", callbacks=[lambda options: [metrics.Observation(self.span_exporter.last_failure, {"signal": "traces"}), metrics.Observation(self.metric_exporter.last_failure, {"signal": "metrics"})]])

    def close(self) -> None:
        """等待有界导出并关闭 SDK，关闭错误由调用位置报告。"""
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def start_telemetry(service: str = "mememeow-api") -> TelemetryRuntime | None:
    """应用启动时创建一次进程 SDK；关闭配置具有明确状态。"""
    global _RUNTIME
    config = TelemetryConfig.from_env(service)
    if not config.enabled:
        return None
    with _LOCK:
        if _RUNTIME is None:
            _RUNTIME = TelemetryRuntime(config)
            trace.set_tracer_provider(_RUNTIME.tracer_provider)
            metrics.set_meter_provider(_RUNTIME.meter_provider)
            HTTPXClientInstrumentor().instrument()
            RequestsInstrumentor().instrument()
            URLLibInstrumentor().instrument()
            ThreadingInstrumentor().instrument()
        return _RUNTIME


def trace_carrier() -> dict[str, str]:
    """生成 W3C 上下文，执行协议的授权仍由现有凭据负责。"""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier


def extract_carrier(carrier: Mapping[str, str]) -> Context:
    """读取仅用于关联的 W3C traceparent 与 tracestate。"""
    return _PROPAGATOR.extract(dict(carrier))


@contextmanager
def observed_operation(operation: str, *, unit: str, attributes: Mapping[str, Any] | None = None, context: Context | None = None) -> Iterator[trace.Span]:
    """测量已知执行边界，保留具体错误并继续原调用的异常行为。"""
    started = time.monotonic()
    meter = metrics.get_meter("mememeow.runtime")
    labels = {"operation": operation, "unit": unit, **{key: value for key, value in (attributes or {}).items() if key in {"provider", "model"} and isinstance(value, str)}}
    with trace.get_tracer("mememeow.runtime").start_as_current_span(operation, context=context, attributes=protected_content(dict(attributes or {}), environment_credentials())) as span:
        result = "succeeded"
        try:
            yield span
        except BaseException as error:
            result = "timeout" if isinstance(error, TimeoutError) else "failed"
            span.set_status(Status(StatusCode.ERROR, protected_content(str(error), environment_credentials())))
            span.set_attribute("error.type", type(error).__name__)
            raise
        finally:
            span.set_attribute("observation.unit", unit)
            span.set_attribute("operation.result", result)
            meter.create_counter("runtime_operations", unit="{operation}").add(1, {**labels, "result": result})
            meter.create_histogram("runtime_operation_duration", unit="s").record(time.monotonic() - started, labels)
