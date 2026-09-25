import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


LEAD = 'lawyer-lead'
CO = 'lawyer-co'
CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter'], 'lead_rep': LEAD, 'co_reps': [CO]}
FLOW = [('submit', Actor(LEAD, 'legal_rep'), {'documents': ['passport', 'sponsor_letter']}, 'submitted'), ('request_evidence', Actor('officer-1', 'case_officer'), {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补充收入证明'}, 'evidence_requested'), ('respond', Actor(CO, 'legal_rep'), {'response_day': 120, 'documents': ['income_proof']}, 'response_received'), ('decide', Actor(LEAD, 'case_officer'), {'decision': 'granted', 'decision_reason': '材料充分'}, 'decided')]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "intake_officer"), "IMM-29001", CREATE_DATA)
        self.assertEqual(record["state"], "draft")
        self.assertEqual(record["payload"]["lead_rep"], LEAD)
        self.assertEqual(record["payload"]["co_reps"], [CO])
        for action, actor, data, expected_state in FLOW:
            record = self.service.act(actor, record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "intake_officer"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 1)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])
