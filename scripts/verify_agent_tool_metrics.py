# 使用真实 OpenCode SQLite 的验收副本核验工具计数、恢复基线和字段诊断。
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from executor.sqlite_telemetry import SQLiteTelemetry, read_saved_records

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """读取指定真实数据库并重放保存记录，核验 SDK 输出，只修改独立验收副本。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    arguments = parser.parse_args()
    source = arguments.database.resolve(strict=True)
    output = ROOT / ".local-real-test/agent-tools" / uuid4().hex
    output.mkdir(parents=True)
    database = output / "opencode.db"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(database)) as copy:
        original.backup(copy)
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    spans = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    trace.set_tracer_provider(tracer)

    def values(name: str) -> Counter:
        """从真实 SDK 累积数据取得按工具和结果分组的数值。"""
        result = Counter()
        data = reader.get_metrics_data()
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for instrument in scope.metrics:
                    if instrument.name == name:
                        for point in instrument.data.data_points:
                            result[(point.attributes.get("tool"), point.attributes.get("result"), point.attributes.get("reason"))] += point.value
        return result

    try:
        with closing(sqlite3.connect(database)) as connection:
            roots = connection.execute("SELECT id, directory FROM session WHERE parent_id IS NULL ORDER BY time_created").fetchall()
        expected = Counter()
        observed_tools = []
        child_tools = 0
        observers = []
        for session_id, directory in roots:
            records = read_saved_records(database, directory=Path(directory), session_id=session_id)
            tools = [record for record in records if record["kind"] == "part" and record["data"].get("type") == "tool"]
            if not tools:
                continue
            observer = SQLiteTelemetry(database=database, directory=Path(directory), session=lambda bound=session_id: bound, attributes={}, context=Context(), started_at=min(record["created_ms"] for record in records) / 1000 - 1, secrets=())
            observers.append(observer)
            observer._poll()
            assert observer.last_success > 0 and observer.failed_since is None
            for record in tools:
                content = record["data"]
                name = content.get("tool")
                assert isinstance(name, str) and name.strip(), "real_tool_name_missing"
                expected.setdefault((name, "succeeded", None), 0)
                expected.setdefault((name, "failed", None), 0)
                status = content["state"]["status"]
                if status in {"completed", "error"}:
                    expected[(name, "failed" if status == "error" else "succeeded", None)] += 1
                child_tools += record["session_id"] != session_id
            observed_tools.extend(tools)
            before = values("agent_saved_terminals")
            observer._poll()
            assert values("agent_saved_terminals") == before, "repeat_poll_added_terminals"
            resumed = SQLiteTelemetry(database=database, directory=Path(directory), session=lambda bound=session_id: bound, attributes={}, context=Context(), started_at=time.time(), secrets=())
            resumed._poll()
            assert resumed.last_success > 0
            assert values("agent_saved_terminals") == before, "resume_added_existing_terminals"
        actual = Counter({key: value for key, value in values("agent_saved_terminals").items() if key[0] is not None})
        assert actual == expected, {"expected": dict(expected), "actual": dict(actual)}
        assert set(expected) <= set(actual), "tool_result_zero_series_missing"
        assert child_tools > 0, "real_child_session_tools_required"
        assert any(key[1] == "failed" and value > 0 for key, value in expected.items()), "real_tool_errors_required"
        assert any(key[1] == "succeeded" and value > 0 for key, value in expected.items()), "real_tool_completions_required"
        tool_spans = [span for span in spans.get_finished_spans() if span.name == "agent.saved.tool"]
        assert tool_spans and all(span.attributes.get("tool.name") for span in tool_spans)
        assert any(span.attributes.get("tool.status") == "error" for span in tool_spans)
        assert all("saved.record" in span.attributes for span in tool_spans)

        observer = observers[0]
        before = values("agent_saved_terminals")
        session = observer.session
        observer.session = lambda: None
        observer._poll(final=True)
        assert observer.failed_since is not None
        observer.session = session
        observer._poll()
        assert observer.failed_since is None
        assert values("agent_saved_terminals") == before, "recovery_added_existing_terminals"

        record = next(record for record in observed_tools if record["session_id"] == observer.session())
        content = dict(record["data"])
        for reason, value in (("missing", None), ("invalid", 17)):
            if value is None:
                content.pop("tool")
            else:
                content["tool"] = value
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE part SET data=?, time_updated=? WHERE id=?", (json.dumps(content), int(time.time() * 1000), record["id"]))
                connection.commit()
            observer._poll()
            assert values("agent_tool_record_issues")[(None, None, reason)] == 1
            assert values("agent_saved_terminals") == before, "tool_name_revision_added_terminals"
        report = {"source": str(source), "database_copy": str(database), "tool_records": len(observed_tools), "child_tool_records": child_tools, "tool_names": sorted({key[0] for key in expected}), "failed_terminals": sum(value for key, value in expected.items() if key[1] == "failed"), "checks": ["real_sqlite_snapshot", "per_tool_results", "zero_result_series", "repeat_poll", "resume_baseline", "child_sessions", "pending_running_excluded", "recovery_baseline", "missing_invalid_names", "tool_trace_attributes"]}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    finally:
        provider.shutdown()
        tracer.shutdown()


if __name__ == "__main__":
    main()
