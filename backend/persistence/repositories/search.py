"""SearchRepository 的 scope 绑定持久化访问。

该模块位于持久化 Repository 边界，只负责 generation/head、迁移控制面、legacy 与
incremental 文本 embedding 查询及向量排序；视觉向量、任务、文件存储和资源装配由
其它模块负责，backend.database 继续提供历史兼容导出。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.paths import validate_business_storage_key
from backend.persistence.engine import DatabaseError
from backend.persistence.models import (
    EMBEDDING_DIMENSIONS,
    Meme,
    MemeEmbedding,
    MemeTextEmbedding,
    ScopeContext,
    SearchGeneration,
    SearchHead,
    SearchMigrationState,
    Task,
    utcnow,
)


class SearchRepository:
    """按 scope 管理 generation、head 和 pgvector 查询。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def _assert_claim(self, claim: tuple[str, int, str] | None) -> None:
        """在当前事务中锁定并验证 Worker claim，阻止过期任务写入索引。"""
        if claim is None:
            return
        task_id, claim_generation, owner = claim
        now = utcnow()
        task = self.session.scalar(
            select(Task)
            .where(
                Task.scope_id == self.scope.scope_id,
                Task.id == task_id,
                Task.claim_generation == claim_generation,
                Task.lease_owner == owner,
                Task.status == "running",
                Task.lease_expires_at > now,
            )
            .with_for_update()
        )
        if task is None:
            raise DatabaseError("claim_expired")

    def assert_claim(self, claim: tuple[str, int, str] | None) -> None:
        """验证可选 Worker claim，供跨模块 generation 工作在写入前调用。"""
        self._assert_claim(claim)

    def active_generation(self, model: str) -> SearchGeneration | None:
        """返回当前 scope/model 的 active generation。"""
        head = self.session.scalar(select(SearchHead).where(SearchHead.scope_id == self.scope.scope_id, SearchHead.model == model))
        if not head or not head.active_generation_id:
            return None
        generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == head.active_generation_id, SearchGeneration.model == model, SearchGeneration.status == "active"))
        return generation

    def migration_state(self, model: str | None = None) -> SearchMigrationState | None:
        """读取当前 scope 的迁移状态，并避免跨模型复用 epoch。"""
        state = self.session.scalar(select(SearchMigrationState).where(SearchMigrationState.scope_id == self.scope.scope_id))
        if state is None or model is None or state.model is None or state.model == model:
            return state
        return None

    def begin_incremental_backfill(self, model: str, *, total_count: int = 0, legacy_generation_id: UUID | str | None = None) -> SearchMigrationState:
        """以新 epoch 开始当前 scope 的增量向量回填，并冻结旧 generation 来源。"""
        if not isinstance(model, str) or not model.strip():
            raise DatabaseError("migration_model_invalid")
        model = model.strip()
        if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
            raise DatabaseError("migration_count_invalid")
        normalized_total = total_count
        state = self.migration_state()
        if state is None:
            state = SearchMigrationState(scope_id=self.scope.scope_id, model=model, mode="backfill", epoch=1, total_count=normalized_total)
            self.session.add(state)
        else:
            state.model = model
            state.mode = "backfill"
            state.epoch += 1
            state.completed_count = 0
            state.total_count = normalized_total
        # 新 epoch 没有合法旧 generation 时必须清掉上一次模型的引用，避免
        # 空 generation 边界下把旧模型的行当作当前迁移来源。
        state.legacy_generation_id = None
        if legacy_generation_id is None:
            head = self.session.scalar(select(SearchHead).where(SearchHead.scope_id == self.scope.scope_id, SearchHead.model == model).with_for_update())
            legacy_generation_id = head.active_generation_id if head is not None else None
        if legacy_generation_id is not None:
            try:
                identifier = UUID(str(legacy_generation_id))
            except (TypeError, ValueError) as exc:
                raise DatabaseError("migration_generation_invalid") from exc
            generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == identifier, SearchGeneration.model == model, SearchGeneration.status == "active"))
            if generation is None:
                raise DatabaseError("migration_generation_invalid")
            state.legacy_generation_id = identifier
        state.updated_at = utcnow()
        self.session.flush()
        return state

    def record_incremental_backfill(self, *, epoch: int, completed_count: int, total_count: int | None = None, model: str | None = None) -> bool:
        """仅更新同一迁移 epoch 的回填进度，拒绝旧 Worker 覆盖新 epoch。"""
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return False
        requested_epoch = epoch
        if requested_epoch < 1:
            return False
        if model is not None and (not isinstance(model, str) or not model.strip()):
            return False
        filters = [SearchMigrationState.scope_id == self.scope.scope_id, SearchMigrationState.mode == "backfill", SearchMigrationState.epoch == requested_epoch]
        if model is not None:
            filters.append(SearchMigrationState.model == str(model).strip())
        state = self.session.scalar(select(SearchMigrationState).where(*filters).with_for_update())
        if state is None:
            return False
        if isinstance(completed_count, bool) or not isinstance(completed_count, int) or completed_count < 0:
            return False
        requested_count = completed_count
        if total_count is not None and (isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0):
            return False
        requested_total = total_count if total_count is not None else state.total_count
        if not isinstance(requested_total, int) or requested_total < 0 or requested_count > requested_total:
            return False
        if state.completed_count < 0 or state.total_count < 0:
            return False
        # 回填进度只能前进；旧 worker 不能把新 epoch 的已完成计数回拨。
        if requested_count < state.completed_count:
            return False
        if total_count is not None and requested_total < state.total_count:
            return False
        state.completed_count = requested_count
        if total_count is not None:
            state.total_count = requested_total
        state.updated_at = utcnow()
        self.session.flush()
        return True

    def switch_incremental_only(self, *, epoch: int, model: str | None = None) -> bool:
        """在同一事务中将完成的回填 epoch 原子切换为增量唯一来源。"""
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return False
        requested_epoch = epoch
        if requested_epoch < 1:
            return False
        if model is not None and (not isinstance(model, str) or not model.strip()):
            return False
        filters = [SearchMigrationState.scope_id == self.scope.scope_id, SearchMigrationState.mode == "backfill", SearchMigrationState.epoch == requested_epoch]
        if model is not None:
            filters.append(SearchMigrationState.model == str(model).strip())
        state = self.session.scalar(select(SearchMigrationState).where(*filters).with_for_update())
        if state is None or state.completed_count < state.total_count:
            return False
        state.mode = "incremental_only"
        state.updated_at = utcnow()
        self.session.flush()
        return True

    @staticmethod
    def _metadata_hash(meme: Meme) -> str | None:
        """读取 Meme 事务性维护的七字段语义 hash。"""
        if isinstance(meme.search_metadata_hash, str) and len(meme.search_metadata_hash) == 64:
            return meme.search_metadata_hash
        try:
            from backend.metadata import MemeContext, semantic_document_hash

            return semantic_document_hash(MemeContext.model_validate(meme.meme_context or {}))
        except (TypeError, ValueError):
            return None

    def _incremental_rows(self, model: str) -> list[tuple[MemeTextEmbedding, Meme]]:
        """读取当前 scope 中通过 SHA、语境和 metadata hash 校验的单图向量。"""
        rows = self.session.execute(
            select(MemeTextEmbedding, Meme)
            .join(Meme, (Meme.scope_id == MemeTextEmbedding.scope_id) & (Meme.id == MemeTextEmbedding.meme_id))
            .where(
                MemeTextEmbedding.scope_id == self.scope.scope_id,
                MemeTextEmbedding.embedding_model_version == model,
                MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                MemeTextEmbedding.status == "ready",
                MemeTextEmbedding.embedding.is_not(None),
                Meme.context_status.in_(("partial", "ready")),
                Meme.sha256 == MemeTextEmbedding.image_sha256,
                Meme.search_metadata_hash == MemeTextEmbedding.metadata_hash,
            )
            # 不在去重和 metadata 校验前截断结果；历史 hash 或损坏行可能占据
            # 前部，固定 limit 会让后面的有效 Meme 永远无法参与查询。
            .order_by(MemeTextEmbedding.meme_id.asc(), MemeTextEmbedding.updated_at.desc())
        ).all()
        valid: list[tuple[MemeTextEmbedding, Meme]] = []
        seen: set[UUID] = set()
        for row, meme in rows:
            if meme.id in seen or self._metadata_hash(meme) != row.metadata_hash:
                continue
            try:
                if len(row.embedding or []) != EMBEDDING_DIMENSIONS:
                    continue
            except TypeError:
                continue
            seen.add(meme.id)
            valid.append((row, meme))
        return valid

    def _legacy_rows(self, model: str) -> list[tuple[MemeEmbedding, Meme]]:
        """逐条校验迁移回退 generation 的 scope、版本、语境和安全 storage key。"""
        # 迁移状态一旦存在就代表旧 generation 来源已经被控制面冻结；即使
        # legacy_generation_id 为空，也不能重新读取会随时变化的 SearchHead。
        state = self.session.scalar(select(SearchMigrationState).where(SearchMigrationState.scope_id == self.scope.scope_id))
        if state is not None:
            if state.model is not None and state.model != model:
                return []
            generation_id = state.legacy_generation_id
        else:
            head = self.session.scalar(select(SearchHead).where(SearchHead.scope_id == self.scope.scope_id, SearchHead.model == model))
            generation_id = head.active_generation_id if head is not None else None
        if generation_id is None:
            return []
        generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == generation_id, SearchGeneration.model == model, SearchGeneration.status == "active"))
        if generation is None or generation.dimensions != EMBEDDING_DIMENSIONS:
            return []
        rows = self.session.execute(
            select(MemeEmbedding, Meme)
            .join(Meme, (Meme.scope_id == MemeEmbedding.scope_id) & (Meme.id == MemeEmbedding.meme_id))
            .where(
                MemeEmbedding.scope_id == self.scope.scope_id,
                MemeEmbedding.generation_id == generation_id,
                MemeEmbedding.item_status == "ready",
                MemeEmbedding.meme_revision == Meme.revision,
                MemeEmbedding.image_sha256 == Meme.sha256,
                Meme.context_status.in_(("partial", "ready")),
            )
            # generation 与 meme_id 是复合主键；不能引用不存在的单列 id。
            .order_by(MemeEmbedding.meme_id.asc())
        ).all()
        valid: list[tuple[MemeEmbedding, Meme]] = []
        seen: set[UUID] = set()
        for row, meme in rows:
            if meme.id in seen or not isinstance(meme.storage_key, str):
                continue
            try:
                validate_business_storage_key(meme.storage_key)
                if row.dimensions != EMBEDDING_DIMENSIONS or len(row.embedding or []) != EMBEDDING_DIMENSIONS:
                    continue
            except (TypeError, ValueError):
                continue
            if self._metadata_hash(meme) != row.metadata_hash:
                continue
            seen.add(meme.id)
            valid.append((row, meme))
        return valid

    def source_mode(self, model: str) -> str:
        """返回当前唯一运行时来源；回填期间不暴露旧 generation。"""
        state = self.migration_state(model)
        if state is not None and state.mode != "incremental_only":
            return "not_ready"
        return "incremental"

    def has_incremental(self, model: str) -> bool:
        """判断当前 scope 是否至少有一条可检索的单图向量。"""
        if self.source_mode(model) != "incremental":
            return False
        statement = (
            select(MemeTextEmbedding.meme_id)
            .join(Meme, (Meme.scope_id == MemeTextEmbedding.scope_id) & (Meme.id == MemeTextEmbedding.meme_id))
            .where(
                MemeTextEmbedding.scope_id == self.scope.scope_id,
                MemeTextEmbedding.embedding_model_version == model,
                MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                MemeTextEmbedding.status == "ready",
                MemeTextEmbedding.embedding.is_not(None),
                Meme.context_status.in_(("partial", "ready")),
                Meme.sha256 == MemeTextEmbedding.image_sha256,
                Meme.search_metadata_hash == MemeTextEmbedding.metadata_hash,
            )
            .limit(1)
        )
        return self.session.scalar(statement) is not None

    def has_legacy(self, model: str) -> bool:
        """兼容控制面查询；旧 generation 永不作为运行时来源。"""
        return False

    def valid_text_embedding_ids(self, model: str, memes: Sequence[Meme]) -> set[UUID]:
        """返回给定图片中具有当前有效文本向量的 Meme ID。

        依据当前迁移来源复用逐条校验结果；每个 Meme 都必须同时满足 scope、图片
        SHA、metadata hash、模型、维度和向量状态约束，不能由 scope 级缓存状态推断。
        """
        if not isinstance(model, str) or not model.strip():
            return set()
        meme_ids = {meme.id for meme in memes if isinstance(getattr(meme, "id", None), UUID)}
        if not meme_ids:
            return set()

        if not hasattr(self, "session"):
            return {meme.id for _row, meme in self._incremental_rows(model) if meme.id in meme_ids}
        rows = self.session.execute(
            select(MemeTextEmbedding.meme_id)
            .join(Meme, (Meme.scope_id == MemeTextEmbedding.scope_id) & (Meme.id == MemeTextEmbedding.meme_id))
            .where(
                MemeTextEmbedding.scope_id == self.scope.scope_id,
                MemeTextEmbedding.meme_id.in_(meme_ids),
                MemeTextEmbedding.embedding_model_version == model.strip(),
                MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                MemeTextEmbedding.status == "ready",
                MemeTextEmbedding.embedding.is_not(None),
                Meme.context_status.in_(("partial", "ready")),
                Meme.sha256 == MemeTextEmbedding.image_sha256,
                Meme.search_metadata_hash == MemeTextEmbedding.metadata_hash,
            )
        )
        return {row[0] for row in rows}

    def create_generation(self, model: str, source_snapshot_hash: str) -> SearchGeneration:
        """创建 building generation，维度固定为 1024。"""
        generation = SearchGeneration(scope_id=self.scope.scope_id, model=model, dimensions=EMBEDDING_DIMENSIONS, source_snapshot_hash=source_snapshot_hash, status="building")
        self.session.add(generation)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise DatabaseError("generation_in_progress") from exc
        return generation

    def abandon_building(self, model: str, *, claim: tuple[str, int, str] | None = None) -> int:
        """在持有任务 claim 时隔离上次崩溃遗留的 building generation。"""
        self._assert_claim(claim)
        rows = list(
            self.session.scalars(
                select(SearchGeneration)
                .where(
                    SearchGeneration.scope_id == self.scope.scope_id,
                    SearchGeneration.model == model,
                    SearchGeneration.status == "building",
                )
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.status = "failed"
        if rows:
            self.session.flush()
        return len(rows)

    def add_item(self, generation: SearchGeneration, meme: Meme, *, semantic_document: str, metadata_hash: str, embedding: Sequence[float] | None, item_status: str = "pending") -> MemeEmbedding:
        """向 generation 写入单条固定维度向量及来源指纹。"""
        if embedding is not None and len(embedding) != EMBEDDING_DIMENSIONS:
            raise DatabaseError("embedding_dimensions_mismatch")
        item = MemeEmbedding(scope_id=self.scope.scope_id, generation_id=generation.id, meme_id=meme.id, embedding=list(embedding) if embedding is not None else None, semantic_document=semantic_document, semantic_document_hash=hashlib.sha256(semantic_document.encode()).hexdigest(), metadata_hash=metadata_hash, image_sha256=meme.sha256, meme_revision=meme.revision, item_status=item_status)
        self.session.add(item)
        self.session.flush()
        return item

    def add_snapshot_item(self, generation_id: UUID, *, meme_id: UUID, meme_revision: int, image_sha256: str, semantic_document: str, metadata_hash: str) -> MemeEmbedding:
        """将短事务快照写入 generation，外部 embedding 完成前保持 pending。"""
        generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == generation_id).with_for_update())
        if generation is None or generation.status != "building":
            raise DatabaseError("generation_not_building")
        item = MemeEmbedding(scope_id=self.scope.scope_id, generation_id=generation_id, meme_id=meme_id, embedding=None, semantic_document=semantic_document, semantic_document_hash=hashlib.sha256(semantic_document.encode()).hexdigest(), metadata_hash=metadata_hash, image_sha256=image_sha256, meme_revision=meme_revision, item_status="pending")
        self.session.add(item)
        self.session.flush()
        return item

    def set_item_embedding(self, generation_id: UUID, meme_id: UUID, embedding: Sequence[float], *, item_status: str = "ready", claim: tuple[str, int, str] | None = None) -> None:
        """在独立短事务中写回单条 embedding，并验证可选 Worker claim。"""
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise DatabaseError("embedding_dimensions_mismatch")
        self._assert_claim(claim)
        result = self.session.execute(
            update(MemeEmbedding)
            .where(MemeEmbedding.scope_id == self.scope.scope_id, MemeEmbedding.generation_id == generation_id, MemeEmbedding.meme_id == meme_id, MemeEmbedding.item_status == "pending")
            .values(embedding=list(embedding), item_status=item_status)
        )
        if result.rowcount != 1:
            raise DatabaseError("generation_item_missing")

    def fail_generation(self, generation_id: UUID, *, error: str, claim: tuple[str, int, str] | None = None) -> None:
        """将仍在构建的 generation 隔离为 failed，不触碰旧 active head。"""
        self._assert_claim(claim)
        generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == generation_id).with_for_update())
        if generation is not None and generation.status == "building":
            generation.status = "failed"
            generation.activated_at = None
            self.session.flush()

    def _generation_source(self, generation_id: UUID) -> list[tuple[str, int, str, str, str]]:
        """读取 generation 中按 meme_id 排序的固定源集合。"""
        rows = self.session.execute(select(MemeEmbedding).where(MemeEmbedding.scope_id == self.scope.scope_id, MemeEmbedding.generation_id == generation_id).order_by(MemeEmbedding.meme_id)).scalars()
        return [(str(row.meme_id), int(row.meme_revision), row.image_sha256, row.metadata_hash, row.semantic_document_hash) for row in rows]

    def _current_indexable_sources(self, memes: Sequence[Meme]) -> list[tuple[str, int, str, str, str]]:
        """从锁定的 Meme 集合重算完整源快照，捕获语境和扩展字段变化。"""
        from backend.metadata import MemeContext, Provenance, SidecarMetadata, semantic_document

        result: list[tuple[str, int, str, str, str]] = []
        for meme in memes:
            if meme.context_status not in {"partial", "ready"}:
                continue
            try:
                context = MemeContext.model_validate(meme.meme_context or {})
                text_value = semantic_document(context)
                if not text_value:
                    continue
                payload: dict[str, Any] = {
                    "schema_version": meme.metadata_schema_version,
                    "image": {
                        "relative_path": meme.storage_key,
                        "extension": meme.extension,
                        "size_bytes": meme.size_bytes,
                        "sha256": meme.sha256,
                    },
                    "context_status": meme.context_status,
                    "meme_context": context.model_dump(mode="json", exclude_none=False),
                    "provenance": Provenance.model_validate(meme.provenance or {}).model_dump(mode="json", exclude_none=False),
                }
                payload.update(meme.extensions or {})
                metadata = SidecarMetadata.model_validate(payload)
                serialized = json.dumps(metadata.model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                metadata_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            except (TypeError, ValueError):
                continue
            result.append((str(meme.id), int(meme.revision), meme.sha256, metadata_hash, hashlib.sha256(text_value.encode()).hexdigest()))
        return sorted(result)

    def activate(self, generation: SearchGeneration, *, expected_source_hash: str | None = None, expected_items: list[tuple[str, int, str, str, str]] | None = None, claim: tuple[str, int, str] | None = None) -> None:
        """在单事务中校验状态、向量完整性、源快照和 Worker claim 后原子切换 head。"""
        self._assert_claim(claim)
        managed_generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == generation.id).with_for_update())
        if managed_generation is None:
            raise DatabaseError("generation_not_found")
        generation = managed_generation
        if generation.scope_id != self.scope.scope_id or generation.dimensions != EMBEDDING_DIMENSIONS or generation.status != "building":
            raise DatabaseError("generation_invalid")
        missing = self.session.scalar(select(func.count()).select_from(MemeEmbedding).where(MemeEmbedding.scope_id == self.scope.scope_id, MemeEmbedding.generation_id == generation.id, MemeEmbedding.item_status != "ready"))
        if missing:
            raise DatabaseError("generation_incomplete")
        current_items = self._generation_source(generation.id)
        if expected_items is not None and current_items != expected_items:
            raise DatabaseError("source_changed")
        if expected_source_hash is not None and generation.source_snapshot_hash != expected_source_hash:
            raise DatabaseError("source_changed")
        current_memes = list(self.session.scalars(select(Meme).where(Meme.scope_id == self.scope.scope_id).with_for_update()))
        current_by_id = {str(item.id): item for item in current_memes}
        if self._current_indexable_sources(current_memes) != current_items:
            raise DatabaseError("source_changed")
        head = self.session.scalar(select(SearchHead).where(SearchHead.scope_id == self.scope.scope_id, SearchHead.model == generation.model).with_for_update())
        if head is None:
            head = SearchHead(scope_id=self.scope.scope_id, model=generation.model)
            self.session.add(head)
        if head.active_generation_id and head.active_generation_id != generation.id:
            old = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == head.active_generation_id))
            if old:
                old.status = "retired"
        head.active_generation_id = generation.id
        head.active_generation_model = generation.model
        generation.status = "active"
        generation.activated_at = utcnow()
        self.session.flush()

    def query(self, model: str, vector: Sequence[float], limit: int = 5) -> list[tuple[UUID, float]]:
        """执行当前增量来源的 pgvector 余弦距离查询。"""
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise DatabaseError("embedding_dimensions_mismatch")
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm <= 0:
            raise DatabaseError("embedding_zero_norm")
        if self.source_mode(model) != "incremental":
            raise DatabaseError("cache_not_ready")
        return self.query_incremental(model, vector, limit)

    def _query_legacy_validated(self, model: str, vector: Sequence[float], limit: int) -> list[tuple[UUID, float]]:
        """对通过旧 generation 逐条校验的向量执行单一来源排序。"""
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm <= 0:
            raise DatabaseError("embedding_zero_norm")
        ranked: list[tuple[UUID, float]] = []
        for row, _meme in self._legacy_rows(model):
            try:
                candidate = [float(item) for item in row.embedding or []]
                candidate_norm = math.sqrt(sum(item * item for item in candidate))
                if len(candidate) != EMBEDDING_DIMENSIONS or not math.isfinite(candidate_norm) or candidate_norm <= 0:
                    continue
                score = sum(left * right for left, right in zip(values, candidate)) / (norm * candidate_norm)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            ranked.append((row.meme_id, float(score)))
        ranked.sort(key=lambda item: (-item[1], str(item[0])))
        return ranked[: max(1, min(int(limit), 100))]

    def query_incremental(self, model: str, vector: Sequence[float], limit: int = 5) -> list[tuple[UUID, float]]:
        """让 pgvector 在数据库内完成当前有效向量的有界余弦排序。"""
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise DatabaseError("embedding_dimensions_mismatch")
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm <= 0:
            raise DatabaseError("embedding_zero_norm")
        state = self.migration_state(model) if hasattr(self, "session") else None
        if state is not None and state.mode != "incremental_only":
            raise DatabaseError("cache_not_ready")
        if not hasattr(self, "session"):
            ranked: list[tuple[UUID, float]] = []
            for row, meme in self._incremental_rows(model):
                candidate = [float(item) for item in row.embedding or []]
                if len(candidate) != EMBEDDING_DIMENSIONS:
                    continue
                candidate_norm = math.sqrt(sum(item * item for item in candidate))
                if not math.isfinite(candidate_norm) or candidate_norm <= 0:
                    continue
                ranked.append((meme.id, sum(left * right for left, right in zip(values, candidate)) / (norm * candidate_norm)))
            ranked.sort(key=lambda item: (-item[1], str(item[0])))
            if not ranked:
                raise DatabaseError("cache_not_ready")
            return ranked[: max(1, min(int(limit), 100))]
        distance = MemeTextEmbedding.embedding.cosine_distance(values).label("distance")
        rows = self.session.execute(
            select(Meme.id, distance)
            .join(Meme, (Meme.scope_id == MemeTextEmbedding.scope_id) & (Meme.id == MemeTextEmbedding.meme_id))
            .where(
                MemeTextEmbedding.scope_id == self.scope.scope_id,
                MemeTextEmbedding.embedding_model_version == model,
                MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                MemeTextEmbedding.status == "ready",
                MemeTextEmbedding.embedding.is_not(None),
                Meme.context_status.in_(("partial", "ready")),
                Meme.sha256 == MemeTextEmbedding.image_sha256,
                Meme.search_metadata_hash == MemeTextEmbedding.metadata_hash,
            )
            .order_by(distance.asc(), Meme.id.asc())
            .limit(max(1, min(int(limit), 100)))
        ).all()
        if not rows:
            raise DatabaseError("cache_not_ready")
        return [(meme_id, float(1.0 - distance_value)) for meme_id, distance_value in rows]
