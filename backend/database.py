"""PostgreSQL 持久化兼容 facade。

模型、engine、事务单元、资源装配、Repository 和文件一致性协调器分别由
backend.persistence 下的 canonical 模块实现。本模块只显式 re-export 历史
公共名称，保证 API、Worker、迁移脚本和宿主适配层无需同步修改旧 import。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.paths import SUPPORTED_EXTENSIONS, validate_business_storage_key
from backend.storage_security import StorageRootError, validate_controlled_root
from backend.persistence.engine import (
    CURRENT_SCHEMA_REVISION,
    DatabaseError,
    SCOPE_LOCAL,
    check_database,
    create_engine_for_settings,
    create_engine_for_url,
    database_url_from_env,
    ensure_optional_control_schema,
    initialize_local,
)
from backend.persistence.models import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    DeclarativeBase,
    EMBEDDING_DIMENSIONS,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    JSONB,
    Mapped,
    String,
    UniqueConstraint,
    Uuid,
    Vector,
    VISUAL_EMBEDDING_DIMENSIONS,
    UTC,
    OPTIONAL_CONTROL_TABLES,
    AgentCallbackRequest,
    Base,
    DerivedImageThumbnail,
    ImageProcessingAttempt,
    ImageProcessingJob,
    ImageProcessingStage,
    InstallationState,
    Meme,
    MemeCollection,
    MemeCollectionItem,
    MemeEmbedding,
    MemeTextEmbedding,
    MemeVisualEmbedding,
    OperationGrant,
    ReverseImageUsageEvent,
    Scope,
    ScopeContext,
    SearchGeneration,
    SearchHead,
    SearchMigrationState,
    StorageOperation,
    Task,
    TaskBatch,
    TaskBatchItem,
    TaskLaneResourceSlot,
    TaskLaneFairness,
    TaskLaneSlot,
    GLOBAL_LANE_RESOURCE_KEY,
    mapped_column,
    timezone,
    unicodedata,
    utcnow,
)
from backend.persistence.resources import DataEnvironment, DatabaseResources
from backend.persistence.repositories.callbacks import (
    AgentCallbackRequestRepository,
    InMemoryAgentCallbackRequestRepository,
    InMemoryCallbackRequest,
    InMemoryCallbackRequestRepository,
)
from backend.persistence.repositories.collections import CollectionRepository
from backend.persistence.repositories.memes import MemeRepository
from backend.persistence.repositories.reverse_image import ReverseImageUsageRepository
from backend.persistence.repositories.search import SearchRepository
from backend.persistence.repositories.tasks import IMAGE_PROCESSING_LANE_TYPES, TaskRepository, _validate_lane_capacities, validate_lane_resource_concurrency, validate_lane_resource_key
from backend.persistence.repositories.thumbnails import DerivedThumbnailRepository
from backend.persistence.repositories.visual_embeddings import VisualEmbeddingRepository, validate_visual_vector
from backend.persistence.storage import BlobStore, StorageCoordinator
from backend.persistence.unit_of_work import UnitOfWork


__all__ = [
    "AgentCallbackRequest",
    "AgentCallbackRequestRepository",
    "Base",
    "BigInteger",
    "BlobStore",
    "Boolean",
    "CURRENT_SCHEMA_REVISION",
    "CheckConstraint",
    "CollectionRepository",
    "DataEnvironment",
    "DerivedImageThumbnail",
    "DerivedThumbnailRepository",
    "DatabaseError",
    "DatabaseResources",
    "DateTime",
    "DeclarativeBase",
    "EMBEDDING_DIMENSIONS",
    "ForeignKey",
    "ForeignKeyConstraint",
    "IMAGE_PROCESSING_LANE_TYPES",
    "ImageProcessingAttempt",
    "ImageProcessingJob",
    "ImageProcessingStage",
    "Index",
    "InMemoryAgentCallbackRequestRepository",
    "InMemoryCallbackRequest",
    "InMemoryCallbackRequestRepository",
    "InstallationState",
    "Integer",
    "JSON",
    "JSONB",
    "Mapped",
    "Meme",
    "MemeCollection",
    "MemeCollectionItem",
    "MemeEmbedding",
    "MemeTextEmbedding",
    "MemeVisualEmbedding",
    "OPTIONAL_CONTROL_TABLES",
    "OperationGrant",
    "ReverseImageUsageEvent",
    "ReverseImageUsageRepository",
    "SCOPE_LOCAL",
    "Scope",
    "ScopeContext",
    "SearchGeneration",
    "SearchHead",
    "SearchMigrationState",
    "SearchRepository",
    "StorageCoordinator",
    "StorageOperation",
    "SUPPORTED_EXTENSIONS",
    "String",
    "Task",
    "TaskBatch",
    "TaskBatchItem",
    "TaskLaneResourceSlot",
    "TaskLaneFairness",
    "TaskLaneSlot",
    "GLOBAL_LANE_RESOURCE_KEY",
    "TaskRepository",
    "UTC",
    "UniqueConstraint",
    "UnitOfWork",
    "Uuid",
    "VISUAL_EMBEDDING_DIMENSIONS",
    "Vector",
    "VisualEmbeddingRepository",
    "_validate_lane_capacities",
    "validate_lane_resource_key",
    "validate_lane_resource_concurrency",
    "check_database",
    "create_engine_for_settings",
    "create_engine_for_url",
    "database_url_from_env",
    "ensure_optional_control_schema",
    "initialize_local",
    "mapped_column",
    "timezone",
    "unicodedata",
    "utcnow",
    "validate_visual_vector",
    "Any",
    "Iterator",
    "Path",
    "Session",
    "UUID",
    "contextmanager",
    "errno",
    "hashlib",
    "json",
    "os",
    "select",
    "StorageRootError",
    "validate_business_storage_key",
    "validate_controlled_root",
    "uuid",
]
