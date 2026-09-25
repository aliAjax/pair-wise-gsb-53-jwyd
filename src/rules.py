"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}, 'transfer': {'legal_rep', 'case_officer', 'supervisor'}, 'accept_transfer': {'legal_rep', 'case_officer', 'supervisor'}, 'decline_transfer': {'legal_rep', 'case_officer', 'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}
ASSIGNMENT_ACTIONS = {'transfer', 'accept_transfer', 'decline_transfer'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    ASSIGNMENT_ACTIONS = ASSIGNMENT_ACTIONS

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        primary = text(p, "primary_agent")
        co_agents = []
        for name in text_list(p, "co_agents", 0):
            if name == primary:
                raise ValidationError("协办不能与主办相同")
            if name not in co_agents:
                co_agents.append(name)
        p["co_agents"] = co_agents
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        p["pending_transfer"] = None
        p["former_agents"] = []
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if int(data.get("response_day", p["response_day"])) > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = int(data["response_day"])
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def apply_assignment(self, record: Dict[str, Any], action: str, data: Dict[str, Any], actor: Actor) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
        """经办人指派动作：不改变案件状态，只调整主办/协办并返回审计细节。"""
        p = dict(record["payload"])
        data = dict(data or {})
        primary = p.get("primary_agent", "")
        if action == "transfer":
            if record["state"] == "closed":
                raise Conflict("案件已结案，不能转交")
            if p.get("pending_transfer"):
                raise Conflict("已有待接收的转交，请先等待接收或拒绝")
            to_user = text(data, "to_user")
            if to_user == primary:
                raise ValidationError("新主办不能是当前主办")
            p["pending_transfer"] = {"to_user": to_user, "initiated_by": actor.user_id, "note": optional_text(data, "note")}
            summary = "已指派新主办，等待接收"
            details = {"from_primary": primary, "to_user": to_user, "initiated_by": actor.user_id}
        elif action == "accept_transfer":
            pending = p.get("pending_transfer") or {}
            to_user = pending.get("to_user", "")
            if not to_user:
                raise Conflict("没有待接收的转交")
            former = [item for item in p.get("former_agents", []) if item.get("user_id") not in (primary, to_user)]
            former.append({"user_id": primary, "capacity": "primary"})
            p["former_agents"] = former
            p["primary_agent"] = to_user
            p["co_agents"] = [name for name in p.get("co_agents", []) if name != to_user]
            p["pending_transfer"] = None
            summary = "新主办已接收，主办身份立即切换"
            details = {"from_primary": primary, "to_primary": to_user}
        elif action == "decline_transfer":
            pending = p.get("pending_transfer") or {}
            to_user = pending.get("to_user", "")
            if not to_user:
                raise Conflict("没有待接收的转交")
            p["pending_transfer"] = None
            summary = "新主办拒绝接收，案件归还原主办"
            details = {"declined_by": to_user, "primary_agent": primary}
        else:
            raise ValidationError("未知的经办人操作")
        return p, summary, details
