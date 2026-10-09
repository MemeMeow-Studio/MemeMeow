# 使用 OpenCode 数据库的本地验收副本核验 SDK 用量，原始执行数据保持只读。
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from opentelemetry import metrics
from opentelemetry.context import Context
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from executor.sqlite_telemetry import SQLiteTelemetry, read_saved_records
from runtime_telemetry import record_cache_decision


def main() -> None:
    """读取真实步骤并更新验收副本，验证重复、基线、修订和字段诊断。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    arguments = parser.parse_args()
    database = arguments.database.resolve()
    if ".local-real-test" not in database.parts:
        raise ValueError("visualization_acceptance_requires_local_database_copy")
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    with closing(sqlite3.connect(database)) as connection:
        session_id, directory = connection.execute("SELECT id, directory FROM session WHERE parent_id IS NULL ORDER BY time_created LIMIT 1").fetchone()
    records = read_saved_records(database, directory=Path(directory), session_id=session_id)
    steps = [record for record in records if record["kind"] == "part" and record["data"].get("type") == "step-finish"]
    assert steps and all(record.get("message_id") for record in steps)

    def totals(name: str) -> float:
        """读取真实 SDK counter 的累积值，保留所有来源类别。"""
        data = reader.get_metrics_data()
        return sum(point.value for resource in data.resource_metrics for scope in resource.scope_metrics for metric in scope.metrics if metric.name == name for point in metric.data.data_points)

    def collector(started_at: float) -> SQLiteTelemetry:
        """绑定当前数据库的主 session，直接执行已有只读采集入口。"""
        return SQLiteTelemetry(database=database, directory=Path(directory), session=lambda: session_id, attributes={}, context=Context(), started_at=started_at, secrets=())

    observed = collector(0)
    observed._poll()
    assert abs(totals("agent_saved_cost") - sum(record["data"]["cost"] for record in steps)) < 1e-9
    original_cost = totals("agent_saved_cost")
    original_tokens = totals("agent_saved_tokens")
    observed._poll()
    assert totals("agent_saved_cost") == original_cost
    assert totals("agent_saved_tokens") == original_tokens
    restored = collector(time.time())
    restored._poll()
    assert totals("agent_saved_cost") == original_cost
    assert totals("agent_saved_tokens") == original_tokens
    record = steps[-1]
    content = dict(record["data"])

    def update() -> None:
        """只更新有明确验收目录标识的数据库副本，触发真实记录修订。"""
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE part SET data=?, time_updated=? WHERE id=?", (json.dumps(content), int(time.time() * 1000), record["id"]))
            connection.commit()

    content["tokens"]["input"] += 7
    content["cost"] += 0.003
    update()
    observed._poll()
    assert abs(totals("agent_saved_cost") - original_cost - 0.003) < 1e-9
    assert totals("agent_saved_tokens") == original_tokens + 7
    content["tokens"]["input"] += 7
    content["cost"] += 0.003
    update()
    observed.session = lambda: None
    observed._poll(final=True)
    observed.session = lambda: session_id
    observed._poll()
    assert abs(totals("agent_saved_cost") - original_cost - 0.003) < 1e-9
    assert totals("agent_saved_tokens") == original_tokens + 7
    content["tokens"]["input"] -= 1
    content.pop("cost")
    update()
    observed._poll()
    assert totals("agent_usage_issues") >= 2
    for result in ("hit", "miss", "refresh"):
        record_cache_decision(provider="monitoring-acceptance", result=result)
    assert totals("runtime_cache_decisions") == 3
    print(json.dumps({"steps": len(steps), "checks": ["real_sqlite", "message_association", "step_only_usage", "repeat", "resume_baseline", "positive_revision", "recovery_baseline", "decrease_and_missing", "cache_sdk"]}))
    provider.shutdown()


if __name__ == "__main__":
    main()
