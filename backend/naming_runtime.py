# 独立命名服务的进程装配，供本地入口和账户适配入口共用。

import signal
from collections.abc import Callable

from loguru import logger
from sqlalchemy.orm import Session

from backend.background_naming import BackgroundNamingWorker, NamingConfig
from backend.config import Settings
from backend.persistence.engine import check_database, create_engine_for_settings
from backend.persistence.resources import DatabaseResources
from runtime_telemetry import start_telemetry


def run_naming_service(settings: Settings, config: NamingConfig, *, scope_pool: Callable[[Session, str], str | None] | None = None, require_local_scope: bool = True, expected_revision: str | None = None) -> None:
    """校验数据库并运行命名进程，接收终止信号后等待当前请求完成。"""
    telemetry = start_telemetry("mememeow-auto-rename")
    engine = create_engine_for_settings(settings)
    try:
        check_database(engine, expected_revision=expected_revision or settings.expected_database_revision, require_local_installation=require_local_scope)
        resources = DatabaseResources(engine, image_root=settings.image_root, data_root=settings.data_root, settings=settings, require_local_scope=require_local_scope)
        logger.add(settings.data_root / "logs" / "auto-rename.log", rotation="10 MB", retention=5, enqueue=True)
        worker = BackgroundNamingWorker(resources, config, scope_pool=scope_pool)

        def stop_service(signum: int, frame: object) -> None:
            """停止领取新任务，运行中的请求由 Worker 保存最终结果。"""
            worker.stopped.set()

        signal.signal(signal.SIGTERM, stop_service)
        signal.signal(signal.SIGINT, stop_service)
        logger.info("auto_rename_started pool={} capacity={} interval={} model={}", config.pool_id, config.capacity, config.interval_seconds, config.model)
        worker.run()
    finally:
        engine.dispose()
        if telemetry is not None:
            telemetry.close()


if __name__ == "__main__":
    run_naming_service(Settings.from_env(), NamingConfig.from_env())
