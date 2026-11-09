import copy
import unittest
from importlib.util import find_spec


class FakeCompose:
    def __init__(self):
        self.data = {'waiting': 2, 'nodes': [
            {'name': 'node-1', 'available': True, 'running': 1, 'queued': 1},
            {'name': 'node-2', 'available': True, 'running': 1, 'queued': 1},
            {'name': 'node-3', 'available': False, 'running': 0, 'queued': 0},
        ]}
        self.services = set()
        self.starts = 0

    def snapshot(self): return copy.deepcopy(self.data)
    def running_services(self): return self.services
    def start_extra(self):
        self.starts += 1
        self.services.update({'redis3', 'worker3'})


class AutoScaleTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeCompose()

    def scale(self, **kwargs):
        self.assertIsNotNone(find_spec('deploy.autoscale'), 'Missing bounded scaling experiment')
        from deploy.autoscale import scale_once
        return scale_once(self.gateway, **kwargs)

    def test_dry_run_plans_but_does_not_start(self):
        self.assertEqual(self.scale()['decision'], 'would-start-node-3')
        self.assertEqual(self.gateway.starts, 0)

    def test_apply_is_bounded_and_idempotent(self):
        self.assertEqual(self.scale(apply=True)['decision'], 'started-node-3')
        self.scale(apply=True)
        self.assertEqual(self.gateway.starts, 1)

    def test_two_node_cap_prevents_any_launch(self):
        self.assertEqual(self.scale(max_nodes=2, apply=True)['decision'], 'at-limit')
        self.assertEqual(self.gateway.starts, 0)

    def test_idle_unavailable_or_no_backlog_does_not_scale(self):
        for key, value in [('running', 0), ('available', False)]:
            self.gateway = FakeCompose()
            self.gateway.data['nodes'][0][key] = value
            self.assertEqual(self.scale(apply=True)['decision'], 'no-pressure')
            self.assertEqual(self.gateway.starts, 0)
        self.gateway = FakeCompose()
        self.gateway.data['waiting'] = 0
        self.assertEqual(self.scale(apply=True)['decision'], 'no-pressure')

    def test_bad_telemetry_and_invalid_limits_fail_closed(self):
        for data in [{}, {'waiting': -1, 'nodes': []}, {'waiting': '2', 'nodes': []}, {'waiting': 1, 'nodes': [{'name': 'node-1'}]}]:
            self.gateway.data = data
            with self.assertRaises(ValueError): self.scale(apply=True)
            self.assertEqual(self.gateway.starts, 0)
        for limit in [0, 1, 4]:
            with self.assertRaises(ValueError): self.scale(max_nodes=limit, apply=True)

    def test_running_extra_or_active_third_node_is_not_started_again(self):
        self.gateway.services.add('worker3')
        self.scale(apply=True)
        self.assertEqual(self.gateway.starts, 0)
        self.gateway.services.clear()
        self.gateway.data['nodes'][2]['available'] = True
        self.scale(apply=True)
        self.assertEqual(self.gateway.starts, 0)
