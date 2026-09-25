import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {
    'applicant_id': 'A-900',
    'case_type': 'family',
    'received_day': 100,
    'deadline_days': 30,
    'response_day': 110,
    'representation_active': True,
    'required_documents': ['passport', 'sponsor_letter'],
    'primary_agent': 'lead-lawyer',
    'co_agents': ['assistant-1'],
}

INTAKE = Actor("intake-1", "intake_officer")
LEAD = Actor("lead-lawyer", "legal_rep")
ASSISTANT = Actor("assistant-1", "legal_rep")
OUTSIDER = Actor("other-lawyer", "legal_rep")
OFFICER = Actor("officer-1", "case_officer")
NEW_LEAD = Actor("new-lawyer", "legal_rep")


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create_case(self, reference="IMM-30001", data=None):
        return self.service.create(INTAKE, reference, data or dict(CREATE_DATA))

    def test_create_records_assignment_in_audit(self):
        record = self.create_case()
        self.assertEqual(record["payload"]["primary_agent"], "lead-lawyer")
        self.assertEqual(record["payload"]["co_agents"], ["assistant-1"])
        self.assertIsNone(record["payload"]["pending_transfer"])
        timeline = self.service.timeline(INTAKE, record["id"])
        self.assertEqual(timeline[0]["action"], "created")
        self.assertEqual(timeline[0]["details"]["primary_agent"], "lead-lawyer")
        self.assertEqual(timeline[0]["details"]["co_agents"], ["assistant-1"])

    def test_create_requires_primary_and_distinct_co_agents(self):
        data = dict(CREATE_DATA)
        del data["primary_agent"]
        with self.assertRaises(ValidationError):
            self.create_case(data=data)
        bad = dict(CREATE_DATA)
        bad["co_agents"] = ["lead-lawyer"]
        with self.assertRaises(ValidationError):
            self.create_case(data=bad)

    def test_co_agent_can_respond_and_view_but_not_decide(self):
        record = self.create_case()
        record = self.service.act(LEAD, record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        record = self.service.act(OFFICER, record["id"], record["version"], "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补充收入证明"})
        record = self.service.act(ASSISTANT, record["id"], record["version"], "respond", {"response_day": 120, "documents": ["income_proof"]})
        self.assertEqual(record["state"], "response_received")
        timeline = self.service.timeline(ASSISTANT, record["id"])
        self.assertTrue(any(event["action"] == "respond" for event in timeline))
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("assistant-1", "supervisor"), record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "材料充分"})
        with self.assertRaises(PermissionDenied):
            self.service.act(ASSISTANT, record["id"], record["version"], "appeal", {"appeal_day": 125, "appeal_reason": "异议"})

    def test_unassigned_lawyer_cannot_act_or_view(self):
        record = self.create_case()
        record = self.service.act(LEAD, record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        record = self.service.act(OFFICER, record["id"], record["version"], "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补充收入证明"})
        with self.assertRaises(PermissionDenied):
            self.service.act(OUTSIDER, record["id"], record["version"], "respond", {"response_day": 120, "documents": ["income_proof"]})
        with self.assertRaises(PermissionDenied):
            self.service.get_record(OUTSIDER, record["id"])
        with self.assertRaises(PermissionDenied):
            self.service.timeline(OUTSIDER, record["id"])

    def test_transfer_accept_switches_primary_immediately(self):
        record = self.create_case()
        record = self.service.act(LEAD, record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        record = self.service.act(LEAD, record["id"], record["version"], "transfer", {"to_user": "new-lawyer", "note": "离职交接"})
        self.assertEqual(record["payload"]["primary_agent"], "lead-lawyer")
        self.assertEqual(record["payload"]["pending_transfer"]["to_user"], "new-lawyer")
        record = self.service.act(OFFICER, record["id"], record["version"], "request_evidence", {"evidence_request_day": 115, "allowed_days": 10, "evidence_request": "补充收入证明"})
        record = self.service.act(LEAD, record["id"], record["version"], "respond", {"response_day": 120, "documents": ["income_proof"]})
        self.assertEqual(record["state"], "response_received")
        self.service.get_record(NEW_LEAD, record["id"])
        with self.assertRaises(PermissionDenied):
            self.service.act(OUTSIDER, record["id"], record["version"], "accept_transfer", {})
        record = self.service.act(NEW_LEAD, record["id"], record["version"], "accept_transfer", {})
        self.assertEqual(record["payload"]["primary_agent"], "new-lawyer")
        self.assertIsNone(record["payload"]["pending_transfer"])
        self.assertIn({"user_id": "lead-lawyer", "capacity": "primary"}, record["payload"]["former_agents"])
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor("lead-lawyer", "supervisor"), record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "材料充分"})
        record = self.service.act(Actor("new-lawyer", "supervisor"), record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "材料充分"})
        self.assertEqual(record["state"], "decided")
        timeline = self.service.timeline(LEAD, record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("transfer", actions)
        self.assertIn("accept_transfer", actions)

    def test_transfer_decline_returns_case_to_original(self):
        record = self.create_case()
        record = self.service.act(LEAD, record["id"], record["version"], "transfer", {"to_user": "new-lawyer"})
        record = self.service.act(NEW_LEAD, record["id"], record["version"], "decline_transfer", {})
        self.assertEqual(record["payload"]["primary_agent"], "lead-lawyer")
        self.assertIsNone(record["payload"]["pending_transfer"])
        record = self.service.act(LEAD, record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        self.assertEqual(record["state"], "submitted")
        timeline = self.service.timeline(INTAKE, record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("transfer", actions)
        self.assertIn("decline_transfer", actions)

    def test_transfer_guards(self):
        record = self.create_case()
        with self.assertRaises(PermissionDenied):
            self.service.act(OUTSIDER, record["id"], record["version"], "transfer", {"to_user": "new-lawyer"})
        with self.assertRaises(ValidationError):
            self.service.act(LEAD, record["id"], record["version"], "transfer", {"to_user": "lead-lawyer"})
        record = self.service.act(LEAD, record["id"], record["version"], "transfer", {"to_user": "new-lawyer"})
        with self.assertRaises(Conflict):
            self.service.act(LEAD, record["id"], record["version"], "transfer", {"to_user": "assistant-1"})
        record = self.service.act(NEW_LEAD, record["id"], record["version"], "decline_transfer", {})
        with self.assertRaises(Conflict):
            self.service.act(NEW_LEAD, record["id"], record["version"], "accept_transfer", {})
        record = self.service.act(Actor("boss", "supervisor"), record["id"], record["version"], "transfer", {"to_user": "new-lawyer"})
        self.assertEqual(record["payload"]["pending_transfer"]["initiated_by"], "boss")

    def test_closed_case_cannot_be_transferred(self):
        record = self.create_case()
        record = self.service.act(LEAD, record["id"], record["version"], "submit", {"documents": ["passport", "sponsor_letter"]})
        record = self.service.act(Actor("lead-lawyer", "case_officer"), record["id"], record["version"], "decide", {"decision": "granted", "decision_reason": "材料充分"})
        record = self.service.act(Actor("lead-lawyer", "supervisor"), record["id"], record["version"], "close", {"closure_note": "办结归档"})
        self.assertEqual(record["state"], "closed")
        with self.assertRaises(Conflict):
            self.service.act(LEAD, record["id"], record["version"], "transfer", {"to_user": "new-lawyer"})

    def test_accept_removes_new_primary_from_co_agents(self):
        data = dict(CREATE_DATA)
        data["co_agents"] = ["assistant-1", "new-lawyer"]
        record = self.create_case(data=data)
        record = self.service.act(LEAD, record["id"], record["version"], "transfer", {"to_user": "new-lawyer"})
        record = self.service.act(NEW_LEAD, record["id"], record["version"], "accept_transfer", {})
        self.assertEqual(record["payload"]["primary_agent"], "new-lawyer")
        self.assertEqual(record["payload"]["co_agents"], ["assistant-1"])
