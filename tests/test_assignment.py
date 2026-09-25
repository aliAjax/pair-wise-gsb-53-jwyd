import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


LEAD = 'lawyer-lead'
CO = 'lawyer-co'
NEW_LEAD = 'lawyer-new'
CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30, 'response_day': 110, 'representation_active': True, 'required_documents': ['passport', 'sponsor_letter'], 'lead_rep': LEAD, 'co_reps': [CO]}


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self):
        return self.service.create(Actor('creator', 'intake_officer'), 'IMM-1', CREATE_DATA)

    def _submit(self, record, user=LEAD):
        return self.service.act(Actor(user, 'legal_rep'), record['id'], record['version'], 'submit', {'documents': ['passport', 'sponsor_letter']})

    def _request_evidence(self, record):
        return self.service.act(Actor('officer-1', 'case_officer'), record['id'], record['version'], 'request_evidence', {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补充收入证明'})

    def _respond(self, record, user=LEAD):
        return self.service.act(Actor(user, 'legal_rep'), record['id'], record['version'], 'respond', {'response_day': 120, 'documents': ['income_proof']})

    def test_co_rep_can_respond_but_not_decide_or_close(self):
        record = self._create()
        with self.assertRaises(PermissionDenied):
            self._submit(record, user=CO)
        record = self._submit(record)
        record = self._request_evidence(record)
        with self.assertRaises(PermissionDenied):
            self._respond(record, user='lawyer-outsider')
        record = self._respond(record, user=CO)
        self.assertEqual(record['state'], 'response_received')
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor(CO, 'case_officer'), record['id'], record['version'], 'decide', {'decision': 'granted', 'decision_reason': '材料充分'})
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor(CO, 'supervisor'), record['id'], record['version'], 'close', {'closure_note': '归档'})
        record = self.service.act(Actor(LEAD, 'case_officer'), record['id'], record['version'], 'decide', {'decision': 'granted', 'decision_reason': '材料充分'})
        self.assertEqual(record['state'], 'decided')

    def test_transfer_accept_switches_lead_immediately(self):
        record = self._create()
        record = self._submit(record)
        record = self._request_evidence(record)
        record = self.service.act(Actor(LEAD, 'legal_rep'), record['id'], record['version'], 'transfer', {'new_lead': NEW_LEAD, 'note': '离职交接'})
        self.assertEqual(record['payload']['pending_transfer']['candidate'], NEW_LEAD)
        self.assertEqual(record['payload']['lead_rep'], LEAD)
        record = self._respond(record)
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor(NEW_LEAD, 'case_officer'), record['id'], record['version'], 'decide', {'decision': 'granted', 'decision_reason': '材料充分'})
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor('lawyer-outsider', 'legal_rep'), record['id'], record['version'], 'accept', {})
        record = self.service.act(Actor(NEW_LEAD, 'legal_rep'), record['id'], record['version'], 'accept', {})
        payload = record['payload']
        self.assertEqual(payload['lead_rep'], NEW_LEAD)
        self.assertEqual(payload['former_leads'], [LEAD])
        self.assertIsNone(payload['pending_transfer'])
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor(LEAD, 'case_officer'), record['id'], record['version'], 'decide', {'decision': 'granted', 'decision_reason': '材料充分'})
        record = self.service.act(Actor(NEW_LEAD, 'case_officer'), record['id'], record['version'], 'decide', {'decision': 'granted', 'decision_reason': '材料充分'})
        self.assertEqual(record['state'], 'decided')
        actions = [event['action'] for event in self.service.timeline(Actor(LEAD, 'legal_rep'), record['id'])]
        self.assertIn('transfer', actions)
        self.assertIn('accept', actions)

    def test_transfer_decline_returns_case_to_original_lead(self):
        record = self._create()
        record = self.service.act(Actor(LEAD, 'legal_rep'), record['id'], record['version'], 'transfer', {'new_lead': NEW_LEAD})
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor('lawyer-outsider', 'legal_rep'), record['id'], record['version'], 'decline', {})
        record = self.service.act(Actor(NEW_LEAD, 'legal_rep'), record['id'], record['version'], 'decline', {})
        self.assertEqual(record['payload']['lead_rep'], LEAD)
        self.assertIsNone(record['payload']['pending_transfer'])
        with self.assertRaises(Conflict):
            self.service.act(Actor(NEW_LEAD, 'legal_rep'), record['id'], record['version'], 'accept', {})
        record = self._submit(record)
        self.assertEqual(record['state'], 'submitted')
        actions = [event['action'] for event in self.service.timeline(Actor(LEAD, 'legal_rep'), record['id'])]
        self.assertIn('transfer', actions)
        self.assertIn('decline', actions)

    def test_assign_co_updates_co_reps_and_is_audited(self):
        record = self._create()
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor(CO, 'legal_rep'), record['id'], record['version'], 'assign_co', {'co_reps': ['co-2']})
        with self.assertRaises(ValidationError):
            self.service.act(Actor(LEAD, 'legal_rep'), record['id'], record['version'], 'assign_co', {'co_reps': [LEAD]})
        record = self.service.act(Actor(LEAD, 'legal_rep'), record['id'], record['version'], 'assign_co', {'co_reps': ['co-2']})
        self.assertEqual(record['payload']['co_reps'], ['co-2'])
        record = self._submit(record)
        record = self._request_evidence(record)
        with self.assertRaises(PermissionDenied):
            self._respond(record, user=CO)
        record = self._respond(record, user='co-2')
        self.assertEqual(record['state'], 'response_received')
        actions = [event['action'] for event in self.service.timeline(Actor(LEAD, 'legal_rep'), record['id'])]
        self.assertIn('assign_co', actions)

    def test_transfer_validation_and_closed_case(self):
        record = self._create()
        with self.assertRaises(ValidationError):
            self.service.act(Actor(LEAD, 'legal_rep'), record['id'], record['version'], 'transfer', {'new_lead': LEAD})
        with self.assertRaises(PermissionDenied):
            self.service.act(Actor(CO, 'legal_rep'), record['id'], record['version'], 'transfer', {'new_lead': NEW_LEAD})
        record = self._submit(record)
        record = self.service.act(Actor(LEAD, 'case_officer'), record['id'], record['version'], 'decide', {'decision': 'granted', 'decision_reason': '材料充分'})
        record = self.service.act(Actor(LEAD, 'supervisor'), record['id'], record['version'], 'close', {'closure_note': '办结归档'})
        self.assertEqual(record['state'], 'closed')
        with self.assertRaises(Conflict):
            self.service.act(Actor(LEAD, 'legal_rep'), record['id'], record['version'], 'transfer', {'new_lead': NEW_LEAD})
        with self.assertRaises(Conflict):
            self.service.act(Actor(LEAD, 'legal_rep'), record['id'], record['version'], 'assign_co', {'co_reps': ['co-2']})
