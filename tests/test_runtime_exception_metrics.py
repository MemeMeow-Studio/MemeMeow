# 使用真实 SDK 验证异常事件的统计单位、时间、接口身份与调查内容保护。
import json

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from runtime_telemetry import ExceptionRecordProcessor


def test_real_exception_events_keep_units_labels_and_trace_identity():
    """真实异常分别保存在父子 span，统计每条事件且过滤日志中的凭据。"""
    reader = InMemoryMetricReader()
    metrics_provider = MeterProvider(metric_readers=[reader])
    logs_provider = LoggerProvider()
    exporter = InMemoryLogRecordExporter()
    logs_provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    provider = TracerProvider()
    processor = ExceptionRecordProcessor(metrics_provider, logs_provider, ("acceptance-credential",))
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("exception-acceptance")
    try:
        with tracer.start_as_current_span("GET /acceptance/{item_id}", kind=trace.SpanKind.SERVER, attributes={"http.route": "/acceptance/{item_id}", "http.request.method": "GET"}) as parent:
            with tracer.start_as_current_span("provider_call"):
                raise ValueError("invalid response token=acceptance-credential")
    except ValueError:
        pass
    records = exporter.get_finished_logs()
    assert len(records) == 2
    assert all(record.log_record.trace_id == parent.get_span_context().trace_id for record in records)
    content = [json.loads(record.log_record.body) for record in records]
    assert {item["http_route"] for item in content} == {"unavailable", "/acceptance/{item_id}"}
    assert all(item["exception_type"] == "ValueError" for item in content)
    assert "acceptance-credential" not in str(content)
    assert all(item["trace_id"] == format(parent.get_span_context().trace_id, "032x") for item in content)
    measurements = {metric.name: metric.data.data_points for resource in reader.get_metrics_data().resource_metrics for scope in resource.scope_metrics for metric in scope.metrics}
    assert sum(point.value for point in measurements["runtime_exceptions"]) == 2
    assert all(point.value > 0 for point in measurements["runtime_exception_last_seen"])
    assert all("trace_id" not in point.attributes for points in measurements.values() for point in points)
    with tracer.start_as_current_span("successful_operation"):
        pass
    assert len(exporter.get_finished_logs()) == 2
    provider.shutdown()
    logs_provider.shutdown()
    metrics_provider.shutdown()
