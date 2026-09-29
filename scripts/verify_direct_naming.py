# 在现有开发数据库中使用独立验收 scope 检查命名容量和文本任务事务。

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from backend.background_naming import BackgroundNamingWorker, NamingConfig
from backend.config import Settings
from backend.embedding_input import build_text_input
from backend.metadata import MemeContext
from backend.persistence.engine import DatabaseError, create_engine_for_settings
from backend.persistence.models import ImageNamingPool, Meme, Scope, Task, TaskBatch, utcnow
from backend.persistence.resources import DatabaseResources
from backend.persistence.storage import StorageCoordinator
from backend.services.metadata import PostgresMetadataService
from backend.services.search import PostgresSearchService


def main() -> None:
    """保存独立测试数据，验证持久容量、轮询、人工保护、恢复及缓存提交。"""
    parser = argparse.ArgumentParser(description="使用现有开发数据库验证后台命名的持久调度")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--model-env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    settings = Settings.from_env()
    address = make_url(settings.database_url)
    assert address.host in {"localhost", "127.0.0.1"} and address.port == 5434
    engine = create_engine_for_settings(settings)
    resources = DatabaseResources(engine, image_root=settings.image_root, data_root=settings.data_root, settings=settings)
    suffix = uuid4().hex
    scopes = [f"test-direct-naming-{suffix}-{index}" for index in range(3)]
    pool_id = f"test-direct-naming-{suffix}"
    config = replace(NamingConfig.from_env(args.model_env_file), pool_id=pool_id)
    with resources.factory.begin() as session:
        session.add_all(Scope(id=scope_id) for scope_id in scopes)
    content = args.image.read_bytes()
    records = []
    for index, scope_id in enumerate(scopes):
        record = StorageCoordinator(resources, scope_id=scope_id).upload(content, extension=".png",
            display_name="a" * 32, context=MemeContext().model_dump(mode="json"),
            provenance={"upload_receipt_id": f"test-{suffix}-{index}"})
        records.append(record)

    def scope_pool(session, scope_id):
        """验收任务池只处理本次创建的 scope。"""
        return pool_id if scope_id in scopes else None

    workers = [BackgroundNamingWorker(resources, config, scope_pool=scope_pool) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            reserved = list(executor.map(lambda worker: worker.reserve_next_user(), workers))
        assert sum(map(len, reserved)) == 1
        task_id = next(value[0] for value in reserved if value)
        with resources.factory() as session:
            first = session.get(Task, task_id)
            assert first.scope_id == scopes[0]
            first_meme = first.target_meme_id
            pool = session.get(ImageNamingPool, pool_id)
            assert pool.cursor_scope_id == scopes[0] and pool.next_check_at > utcnow()
        with resources.environment(scopes[0]) as environment:
            try:
                environment.tasks.submit(task_type="derived_thumbnail_generation", payload={"meme_id": str(first_meme), "image_sha256": records[0].sha256})
            except DatabaseError as exc:
                assert exc.code == "image_processing_conflict"
            else:
                raise AssertionError("thumbnail_conflict_missing")
        with resources.environment(scopes[0]) as environment:
            environment.memes.update_display_name(first_meme, "人工验收名称")
            children = list(environment.uow.session.scalars(select(Task).where(Task.scope_id == scopes[0], Task.task_type == "text_embedding_generation")))
            assert len(children) == 1
            child_id = children[0].id
            assert environment.tasks.claim(task_id=child_id, owner="test-text") is None
        workers[0].execute(scopes[0], task_id)
        with resources.environment(scopes[0]) as environment:
            task = environment.tasks.get(task_id)
            assert task.status == "failed" and task.error["error"] == "target_changed"
            record = environment.memes.get(first_meme)
            assert record.name_source == "manual" and record.display_name == "人工验收名称"
            child = environment.tasks.claim(task_id=child_id, owner="test-text")
            assert child is not None
            environment.tasks.fail_fenced(child.id, child.claim_generation, "test-text", error={"error": "acceptance_no_embedding_requested"}, message="验收结束", retry=False)
        with resources.factory.begin() as session:
            session.get(ImageNamingPool, pool_id).next_check_at = utcnow() - timedelta(seconds=1)
        second_ids = workers[1].reserve_next_user()
        assert len(second_ids) == 1
        with resources.factory() as session:
            assert session.get(Task, second_ids[0]).scope_id == scopes[1]

        def unavailable_scope(session, scope_id):
            """账户已失效时返回无任务池资格。"""
            return None

        denied = BackgroundNamingWorker(resources, config, scope_pool=unavailable_scope)
        try:
            denied.execute(scopes[1], second_ids[0])
        finally:
            denied.executor.shutdown()
        with resources.factory() as session:
            task = session.get(Task, second_ids[0])
            assert task.status == "failed" and task.attempt_count == 1
            assert task.error["error"] == "auto_rename_scope_ineligible"
        with resources.factory.begin() as session:
            session.get(ImageNamingPool, pool_id).next_check_at = utcnow() - timedelta(seconds=1)
        third_ids = workers[0].reserve_next_user()
        with resources.environment(scopes[2]) as environment:
            running = environment.tasks.claim(task_id=third_ids[0], owner="test-interrupted", allow_name_generation=True)
            running.lease_expires_at = utcnow() - timedelta(seconds=1)
            running.payload = {**running.payload, "request_deadline": (utcnow() + timedelta(seconds=30)).isoformat()}
        assert workers[0].recover_interrupted() == 0
        with resources.factory.begin() as session:
            running = session.get(Task, third_ids[0])
            running.payload = {**running.payload, "request_deadline": (utcnow() - timedelta(seconds=60)).isoformat()}
        assert workers[0].recover_interrupted() == 1
        assert workers[1].recover_interrupted() == 0
        with resources.environment(scopes[2]) as environment:
            assert not environment.tasks.update_fenced(third_ids[0], running.claim_generation, "test-interrupted", status="succeeded")
            cache = environment.tasks.submit(task_type="cache_generation", payload={}, dedupe_key="cache_generation")
            claim = environment.tasks.claim(task_id=cache.id, owner="test-cache")
        search = PostgresSearchService(settings, resources, PostgresMetadataService(resources, scope_id=scopes[2]), scope_id=scopes[2])
        result = search.generate_cache(lambda *_: None, claim=(claim.id, claim.claim_generation, "test-cache"))
        assert result["waiting_for_text_count"] == 1 and result["generated_count"] == 0 and not result["_pending"]
        with resources.environment(scopes[2]) as environment:
            environment.tasks.update_fenced(claim.id, claim.claim_generation, "test-cache", status="succeeded", result=result)
            environment.tasks.add_batch_item("acceptance-batch", claim.id)
            environment.tasks.seal_batch("acceptance-batch")
            assert environment.tasks.finalize_batch("acceptance-batch")
            assert not environment.tasks.finalize_batch("acceptance-batch")
            batch = environment.uow.session.get(TaskBatch, {"scope_id": scopes[2], "batch_id": "acceptance-batch"})
            assert batch.summary["succeeded_count"] == 1
        print(json.dumps({"scopes": scopes, "pool_id": pool_id, "checks": ["two_instances_one_interval", "thumbnail_conflict", "manual_priority", "text_waits_for_naming", "authorization_failure_terminal", "persistent_user_cursor", "interrupted_deadline", "expired_claim_rejected", "waiting_text_cache", "batch_summary_idempotent"]}, ensure_ascii=False))
    finally:
        for worker in workers:
            worker.executor.shutdown()
        engine.dispose()


if __name__ == "__main__":
    main()
