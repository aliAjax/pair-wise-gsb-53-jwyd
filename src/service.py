"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


VIEW_ROLES = {"admin", "intake_officer", "case_officer", "supervisor"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _check_view(actor: Actor, record: Dict[str, Any]) -> None:
        if actor.role in VIEW_ROLES:
            return
        payload = record["payload"]
        user_id = actor.user_id
        if user_id == payload.get("primary_agent") or user_id in payload.get("co_agents", []):
            return
        pending = payload.get("pending_transfer") or {}
        if pending.get("to_user") == user_id:
            return
        for item in payload.get("former_agents", []):
            if item.get("user_id") == user_id:
                return
        raise PermissionDenied("无权查看该案件")

    @staticmethod
    def _check_assignment(actor: Actor, record: Dict[str, Any], action: str) -> None:
        payload = record["payload"]
        user_id = actor.user_id
        primary = payload.get("primary_agent")
        if action in ("submit", "respond"):
            if user_id != primary and user_id not in payload.get("co_agents", []):
                raise PermissionDenied("仅主办或协办可提交或补交材料")
        elif action in ("decide", "close", "appeal"):
            if user_id != primary:
                raise PermissionDenied("决定与结案仅由主办负责")
        elif action == "transfer":
            if actor.role not in ("admin", "supervisor") and user_id != primary:
                raise PermissionDenied("仅主办本人或督导可发起转交")
        elif action in ("accept_transfer", "decline_transfer"):
            pending = payload.get("pending_transfer") or {}
            if pending and pending.get("to_user") != user_id:
                raise PermissionDenied("只有被指派的代理人可以接收或拒绝")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        audit_details = {
            "state": self.rules.INITIAL_STATE,
            "summary": "收案并指派主办",
            "primary_agent": prepared["primary_agent"],
            "co_agents": prepared["co_agents"],
        }
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, audit_details)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._check_view(actor, record)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        data = data or {}
        if action in self.rules.ASSIGNMENT_ACTIONS:
            self._check_assignment(actor, record, action)
            new_payload, summary, extra = self.rules.apply_assignment(record, action, data, actor)
            details = {"summary": summary, "input": data}
            details.update(extra)
            return self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=record["state"],
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details=details,
            )
        self._check_assignment(actor, record, action)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._check_view(actor, record)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
