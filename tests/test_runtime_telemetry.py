# 使用真实 OpenTelemetry SDK 检查内容保护、导出属性与执行上下文。
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from runtime_telemetry import SECONDS_HISTOGRAM_BOUNDARIES, _ProtectedExporter, correlation_attributes, duration_views, extract_carrier, protected_content, record_content


def test_correlation_copies_only_trusted_investigation_fields():
    """真实 SDK span 的关联快照仅包含账户与范围，不携带凭据或授权内容。"""
    provider = TracerProvider()
    with provider.get_tracer("telemetry-test").start_as_current_span("request", attributes={"account.id": "acceptance-account", "scope.id": "acceptance-scope", "authorization": "secret-value"}):
        assert correlation_attributes() == {"account.id": "acceptance-account", "scope.id": "acceptance-scope"}
    assert correlation_attributes() == {}
    provider.shutdown()


def test_content_preserves_debug_and_filters_credentials():
    """业务正文可调查，结构化凭据、认证文本和二进制内容被处理。"""
    result = protected_content({"prompt": "数据库等待", "api_key": "credential-value", "error": "request timeout; token=credential-value", "tool": {"authorization": "Bearer credential-value"}, "image_data": b"image bytes"})
    assert result["prompt"] == "数据库等待"
    assert "credential-value" not in str(result)
    assert "request timeout" in result["error"]
    assert "binary content omitted" in result["image_data"]


def test_real_sdk_exports_protected_attributes_events_and_status():
    """生成实际 SDK span，验证专用 exporter 处理自动事件与关联字段。"""
    memory = InMemorySpanExporter()
    exporter = _ProtectedExporter(memory)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("telemetry-test").start_as_current_span("sqlite.read") as span:
        span.set_attribute("task.id", "telemetry-test-task")
        span.set_attribute("authorization", "Bearer credential-value")
        span.add_event("sqlite.error", {"message": "database locked; password=credential-value"})
        record_content(span, "saved.record", {"text": "调查正文", "password": "credential-value"})
    records = memory.get_finished_spans()
    assert len(records) == 1
    assert records[0].attributes["task.id"] == "telemetry-test-task"
    assert "credential-value" not in str(records[0].attributes)
    assert "credential-value" not in str(records[0].events[0].attributes)
    assert "database locked" in records[0].events[0].attributes["message"]
    assert exporter.last_success > 0
    provider.shutdown()


def test_trace_carrier_does_not_supply_business_authorization():
    """W3C parser 只生成追踪上下文，不产生身份或授权字段。"""
    context = extract_carrier({"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01", "authorization": "Bearer credential-value"})
    assert trace.get_current_span(context).get_span_context().is_valid
    assert context.get("authorization") is None


def test_usage_tokens_remain_available():
    """保留已保存的用量字段，同时过滤用于认证的 token 字段。"""
    result = protected_content({"tokens": {"input": 100, "output": 20}, "session_token": "credential-value", "token": "credential-value"})
    assert result["tokens"] == {"input": 100, "output": 20}
    assert "credential-value" not in str(result)


def test_duration_histogram_keeps_subsecond_thresholds():
    """真实 SDK 使用秒数边界，将 200 毫秒前后的等待分别聚合。"""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], views=duration_views())
    histogram = provider.get_meter("telemetry-test").create_histogram("db_pool_wait", unit="s")
    histogram.record(0.19)
    histogram.record(0.21)
    data = reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics[0].data.data_points[0]
    assert data.explicit_bounds == SECONDS_HISTOGRAM_BOUNDARIES
    assert data.bucket_counts[5] == 1
    assert data.bucket_counts[6] == 1
    provider.shutdown()
