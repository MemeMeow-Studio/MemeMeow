# 使用所属开发 PostgreSQL 验证 Task 提交、保存点和回滚的真实采集行为。
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import timedelta
from uuid import uuid4

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from backend.persistence.models import Scope, ScopeContext, Task, utcnow
from backend.persistence.repositories.tasks import TaskRepository
from backend.persistence.task_telemetry import instrument_task_sessions


def main() -> None:
    """创建带验收标识的真实记录，核实提交成功后的计数且保留数据。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-container", required=True)
    parser.add_argument("--database-container", required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--database-port", required=True, type=int)
    arguments = parser.parse_args()
    containers = json.loads(subprocess.check_output(["docker", "inspect", arguments.api_container, arguments.database_container]))
    for container in containers:
        labels = container["Config"]["Labels"]
        assert labels["com.docker.compose.project"] == arguments.compose_project
        assert labels["com.docker.compose.project.working_dir"] == arguments.workspace
    environment = dict(entry.split("=", 1) for entry in containers[0]["Config"]["Env"])
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    engine = create_engine(make_url(environment["MEMEMEOW_DATABASE_URL"]).set(host="127.0.0.1", port=arguments.database_port))
    factory = sessionmaker(engine, expire_on_commit=False)
    instrument_task_sessions(factory)
    identifier = "monitoring-acceptance-" + uuid4().hex

    def counts() -> dict[tuple[str, str], int]:
        """读取真实 SDK 聚合，标签来源为已提交 Task 字段。"""
        result = {}
        data = reader.get_metrics_data()
        if data is not None:
            for resource in data.resource_metrics:
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        if metric.name == "task_terminals":
                            for point in metric.data.data_points:
                                result[(point.attributes["result"], point.attributes["reason"])] = point.value
        return result

    with factory() as session:
        session.add(Scope(id=identifier))
        session.commit()
        task = Task(id=identifier, scope_id=identifier, task_type="monitoring_acceptance", status="running", lease_owner="monitoring-acceptance", lease_expires_at=utcnow() + timedelta(minutes=5))
        session.add(task)
        session.commit()
        assert counts() == {}
        task.status = "failed"
        task.error = {"error": "agent_reservation_expired"}
        session.flush()
        assert counts() == {}
        session.rollback()
        assert counts() == {}
        task = session.get(Task, identifier)
        with session.begin_nested():
            task.status = "failed"
            task.error = {"error": "agent_reservation_expired"}
            session.flush()
        assert counts() == {}
        session.rollback()
        assert counts() == {}
        task = session.get(Task, identifier)
        nested = session.begin_nested()
        task.status = "failed"
        task.error = {"error": "discarded_savepoint"}
        session.flush()
        nested.rollback()
        repository = TaskRepository(session, ScopeContext(identifier))
        assert repository.cancel(task.id, error={"error": "task_cancelled"}, message="monitoring-acceptance 取消")
        session.commit()
        assert counts() == {("cancelled", "task_cancelled"): 1}
        task.message = "monitoring-acceptance 提交后的更新"
        session.commit()
        assert counts() == {("cancelled", "task_cancelled"): 1}
        bulk = Task(id=identifier + "-bulk", scope_id=identifier, task_type="monitoring_acceptance", status="running", lease_owner="monitoring-acceptance", lease_expires_at=utcnow() + timedelta(minutes=5))
        session.add(bulk)
        session.commit()
        repository = TaskRepository(session, ScopeContext(identifier))
        assert repository.update_fenced(bulk.id, 0, "monitoring-acceptance", status="failed", error={"error": "agent_reservation_deadline_invalid"})
        assert counts() == {("cancelled", "task_cancelled"): 1}
        session.commit()
        assert counts() == {("cancelled", "task_cancelled"): 1, ("failed", "agent_reservation_deadline_invalid"): 1}
    provider.shutdown()
    engine.dispose()
    print(json.dumps({"scope": identifier, "checks": ["commit", "rollback", "savepoint_commit", "savepoint_rollback", "unchanged_terminal", "fenced_sql_update"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
