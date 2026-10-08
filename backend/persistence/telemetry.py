# 持久化连接观测，在实际 QueuePool 获取边界记录等待，不申请额外连接。
from __future__ import annotations

import time

from opentelemetry import metrics
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy.engine import Engine
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.pool import QueuePool


class TelemetryQueuePool(QueuePool):
    """保留 QueuePool 行为，测量获取成功与获取超时的实际等待。"""

    def _do_get(self):
        """在已有获取路径写入 SDK 内存测量，原异常继续传播。"""
        started = time.monotonic()
        result = "succeeded"
        try:
            return super()._do_get()
        except SQLAlchemyTimeoutError:
            result = "timeout"
            metrics.get_meter("mememeow.database").create_counter("db_pool_acquire_timeouts", unit="{timeout}").add(1)
            raise
        finally:
            metrics.get_meter("mememeow.database").create_histogram("db_pool_wait", unit="s").record(time.monotonic() - started, {"result": result})


def instrument_engine(engine: Engine) -> None:
    """为现有 Engine 注册 SQL span 与连接池 gauges，只读取池内存状态。"""
    SQLAlchemyInstrumentor().instrument(engine=engine)
    meter = metrics.get_meter("mememeow.database")
    for name, getter in (("checked_out", engine.pool.checkedout), ("checked_in", engine.pool.checkedin), ("size", engine.pool.size)):
        meter.create_observable_gauge(f"db_pool_{name}", unit="{connection}", callbacks=[lambda options, read=getter: [metrics.Observation(read())]])
