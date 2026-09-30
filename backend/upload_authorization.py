# 上传接收任务的授权关联，复用 operation policy 与持久 grant 仓储。

from __future__ import annotations

from typing import Any, Callable

from backend.operation_policy import OperationPolicyError, Operations


class UploadAuthorization:
    """以接收任务标识关联容量预留，图片登记后完成正式计量。"""

    def __init__(self, scope: Any, gateway: Any, store: Any, *, release: Callable[..., Any], bind: Callable[..., Any] | None = None):
        """接收可信 scope、策略仓储及宿主释放和容量绑定接口。"""
        self.scope = scope
        self.gateway = gateway
        self.store = store
        self.release = release
        self.bind = bind

    def request(self, payload: dict[str, Any]) -> Any:
        """从持久接收字段生成相同 attempt 内稳定的授权请求。"""
        task_id = payload.get("authorization_task_id", payload["receipt_id"])
        return self.gateway.request(self.scope, Operations.IMAGE_UPLOAD, f"upload-receipt:{task_id}", resource_id=payload["receipt_id"], task_id=task_id, source="upload", input_digest=payload["input_digest"])

    def reserve(self, payload: dict[str, Any]) -> None:
        """在上传接收或显式重试时预留一个图片名额。"""
        request = self.request(payload)
        existing = self.store.get(request)
        if existing is not None and existing.state == "committed":
            return
        if existing is not None and existing.state == "released":
            # 已释放的预留需要新的授权身份；已计量的身份始终复用。
            payload["authorization_task_id"] = payload["_claim_task_id"]
            from backend.upload_receipts import UploadReceipts

            receipts = UploadReceipts(self.store.resources, self.scope, None)
            with receipts.resources.environment(self.scope) as environment:
                receipt = environment.tasks.get(payload["receipt_id"], for_update=True)
                receipt.payload = {**receipt.payload, "authorization_task_id": payload["authorization_task_id"]}
            request = self.request(payload)
        self.store.acquire(request, self.gateway)

    def finish(self, payload: dict[str, Any], meme_id: str, owned: bool) -> None:
        """依据图片来源完成计量，重复已有图片时释放本次预留。"""
        association = self.store.get(self.request(payload))
        if association is None:
            raise OperationPolicyError("operation_grant_invalid")
        if association.state == "committed":
            return
        if not owned:
            self.release(association.grant)
            return
        if self.bind is not None and not self.bind(association.grant, meme_id):
            raise OperationPolicyError("operation_grant_invalid")
        result = self.gateway.commit(association.grant)
        if not result.ok or result.state not in {"committed", "already_committed"}:
            raise OperationPolicyError(result.reason or "operation_policy_unavailable")
        if not self.store.transition(association.grant, "committed"):
            raise OperationPolicyError("operation_grant_invalid")

    def discard(self, payload: dict[str, Any]) -> None:
        """在确认未登记图片后释放已有接收预留，不创建新的授权。"""
        if not payload.get("input_digest"):
            return
        association = self.store.get(self.request(payload))
        if association is not None and association.state in {"acquired", "unknown"}:
            # 调用方已确认未登记图片，恢复程序记录的 unknown 也能依据该事实释放。
            self.release(association.grant)
