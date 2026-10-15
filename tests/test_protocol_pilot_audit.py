import unittest
from scripts.analyze_protocol_pilot import audit_result


class WorkflowAuditTests(unittest.TestCase):
    def case(self, worker_status):
        return {'arm': 'D-full', 'status': 'complete', 'error': '', 'summary': {
            'collaboration': {'worker_results': [{'worker': 'security', 'status': worker_status}],
                              'lead': {'final': {'action': 'final'}}}}}

    def test_tool_success_does_not_hide_failed_worker(self):
        original = self.case('failed')
        audited = audit_result(original)
        self.assertEqual('partial', audited['status'])
        self.assertEqual('complete', original['status'])
        self.assertEqual('complete', audited['recorded_status'])

    def test_completed_revision_is_not_treated_as_failed_worker(self):
        self.assertEqual('complete', audit_result(self.case('completed'))['status'])

    def test_missing_final_synthesis_is_incomplete(self):
        case = self.case('completed')
        case['summary']['collaboration']['lead'] = {}
        self.assertEqual('partial', audit_result(case)['status'])

    def test_single_arm_does_not_require_worker_results(self):
        self.assertEqual('complete', audit_result({'arm': 'A-single', 'status': 'complete',
                                                   'error': '', 'summary': {}})['status'])
