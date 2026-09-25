"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, PermissionDenied, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}
TERMINAL_STATES = {'closed'}
# 协办可办理的动作（补交材料），其余案件动作仅主办负责
CO_REP_ACTIONS = {'respond'}
LEAD_ONLY_ACTIONS = {'submit', 'appeal', 'decide', 'close'}
# 分工调整动作：不改变案件状态，但全部写入办理记录
ASSIGNMENT_ACTIONS = {'assign_co', 'transfer', 'accept', 'decline'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        if role == "admin":
            return True
        if action in ASSIGNMENT_ACTIONS:
            return self.known_role(role)
        return role in ACTION_ROLES.get(action, set())

    def is_assignment_action(self, action: str) -> bool:
        return action in ASSIGNMENT_ACTIONS

    @staticmethod
    def _clean_co_reps(data: Dict[str, Any], lead: str) -> list:
        co_reps: list = []
        for item in text_list(data, "co_reps"):
            if item not in co_reps:
                co_reps.append(item)
        if lead in co_reps:
            raise ValidationError("主办不能同时担任协办")
        return co_reps

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        lead = text(p, "lead_rep")
        p["co_reps"] = self._clean_co_reps(p, lead)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        p["former_leads"] = []
        p["pending_transfer"] = None
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

    def require_assignment_transition(self, record: Dict[str, Any], action: str) -> None:
        if record["state"] in TERMINAL_STATES:
            raise Conflict("案件已归档，不能调整分工")
        if action in {"accept", "decline"} and not record["payload"].get("pending_transfer"):
            raise Conflict("当前没有待接收的转交")

    def check_assignment_scope(self, record: Dict[str, Any], actor: Actor, action: str) -> None:
        if actor.role == "admin":
            return
        payload = record["payload"]
        lead = payload.get("lead_rep")
        if action in CO_REP_ACTIONS:
            if actor.user_id != lead and actor.user_id not in payload.get("co_reps", []):
                raise PermissionDenied("仅主办或协办可补交材料")
        elif action in LEAD_ONLY_ACTIONS:
            if actor.user_id != lead:
                raise PermissionDenied("仅主办可执行该操作")

    def check_assignment_actor(self, record: Dict[str, Any], actor: Actor, action: str) -> None:
        payload = record["payload"]
        if action in {"assign_co", "transfer"}:
            if actor.role != "admin" and actor.user_id != payload.get("lead_rep"):
                raise PermissionDenied("仅主办可调整案件分工")
        elif action in {"accept", "decline"}:
            candidate = (payload.get("pending_transfer") or {}).get("candidate")
            if actor.user_id != candidate:
                raise PermissionDenied("仅被指派的代理人可接收或拒绝")

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action in ASSIGNMENT_ACTIONS:
            new_state = record["state"]
            if action == "assign_co":
                co_reps = self._clean_co_reps(data, p["lead_rep"])
                changes["co_reps"] = co_reps
                summary = "协办分工已更新：" + (", ".join(co_reps) if co_reps else "无")
            elif action == "transfer":
                candidate = text(data, "new_lead")
                if candidate == p["lead_rep"]:
                    raise ValidationError("新主办不能与现任主办相同")
                changes["pending_transfer"] = {"candidate": candidate, "note": optional_text(data, "note")}
                summary = "案件已指派给%s，等待接收" % candidate
            elif action == "accept":
                candidate = (p.get("pending_transfer") or {}).get("candidate", "")
                former = list(p.get("former_leads", []))
                if p["lead_rep"] not in former:
                    former.append(p["lead_rep"])
                changes["lead_rep"] = candidate
                changes["former_leads"] = former
                changes["co_reps"] = [item for item in p.get("co_reps", []) if item != candidate]
                changes["pending_transfer"] = None
                summary = "%s已接收案件，成为主办" % candidate
            elif action == "decline":
                candidate = (p.get("pending_transfer") or {}).get("candidate", "")
                changes["pending_transfer"] = None
                summary = "%s已拒绝接收，案件归还原主办" % candidate
            p.update(changes)
            return new_state, p, summary
        new_state = self.require_transition(record, action)
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
