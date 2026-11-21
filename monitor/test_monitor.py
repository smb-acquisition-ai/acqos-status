"""Synthetic tests: no production endpoints, credentials or GitHub calls."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks  # noqa: E402
from checks import Result  # noqa: E402
import issues  # noqa: E402
import run  # noqa: E402
import stub_origin  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / 'site'


class StubCase(unittest.TestCase):
    def setUp(self):
        self.stub = stub_origin.start('healthy')
        self.addCleanup(self.stub.server_close)
        self.addCleanup(self.stub.shutdown)
        port = self.stub.server_address[1]
        self.config = {'site_url': 'http://127.0.0.1:%d/' % port,
                       'health_url': 'http://127.0.0.1:%d/agent-api/health' % port,
                       'client_id': 'cf-id', 'client_secret': 'cf-secret', 'token': stub_origin.TOKEN,
                       'check_certificate': False, 'access_suffix': 'localhost'}

    def states(self, **overrides):
        config = dict(self.config, **overrides)
        return {k: (v.state, v.reason) for k, v in checks.run_checks(config).items()}


class ClassificationTests(StubCase):
    def test_healthy(self):
        self.assertEqual(self.states(), {
            'website': ('operational', 'access_redirect'), 'access': ('operational', 'login_page_ok'),
            'application': ('operational', 'health_ok'), 'database': ('operational', 'component_ok'),
            'directory': ('operational', 'component_ok')})

    def test_login_page_alone_does_not_make_application_healthy(self):
        # Edge and login page answer while the tunnel is down.
        self.stub.mode = 'tunnel_down'
        result = self.states()
        self.assertEqual(result['website'][0], 'operational')
        self.assertEqual(result['access'][0], 'operational')
        self.assertEqual(result['application'], ('outage', 'tunnel_unavailable'))
        self.assertEqual(result['database'], ('unknown', 'application_unverified'))

    def test_origin_and_app_failures_are_outages(self):
        for mode, reason in (('origin_down', 'origin_unreachable'), ('app_error', 'app_unavailable')):
            self.stub.mode = mode
            self.assertEqual(self.states()['application'], ('outage', reason), mode)

    def test_database_failure_reported_by_app(self):
        self.stub.mode = 'db_down'
        result = self.states()
        self.assertEqual(result['application'][0], 'degraded')
        self.assertEqual(result['database'], ('outage', 'component_fail'))

    def test_auth_failures_are_unknown_not_outage(self):
        edge = self.states(client_secret='wrong')
        self.assertEqual(edge['application'], ('unknown', 'auth_rejected_edge'))
        app = self.states(token='synthetic-wrong-token-' + 'y' * 40)
        self.assertEqual(app['application'], ('unknown', 'auth_rejected_app'))
        self.assertEqual(app['database'][0], 'unknown')

    def test_edge_security_block_is_unknown_not_outage(self):
        self.stub.mode = 'blocked'
        result = self.states()
        self.assertEqual(result['website'], ('unknown', 'edge_blocked'))
        self.assertEqual(result['application'], ('unknown', 'edge_blocked'))
        self.stub.mode = 'blocked_empty'
        self.assertEqual(self.states()['website'], ('unknown', 'edge_blocked'))

    def test_edge_and_login_failures(self):
        self.stub.mode = 'edge_down'
        result = self.states()
        self.assertEqual(result['website'][0], 'outage')
        self.assertEqual(result['access'], ('unknown', 'no_login_url'))
        self.stub.mode = 'login_down'
        self.assertEqual(self.states()['access'][0], 'outage')

    def test_unreachable_host_and_missing_credentials(self):
        result = self.states(site_url='http://127.0.0.1:9/', health_url='http://127.0.0.1:9/agent-api/health')
        self.assertEqual(result['website'][0], 'outage')
        self.assertEqual(result['application'][0], 'outage')
        self.assertEqual(self.states(token='')['application'], ('unknown', 'monitor_not_configured'))


class RunTests(StubCase):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.store = issues.MemoryStore(log=lambda *_: None)
        self.sleeps = []
        self.clock_value = datetime(2026, 11, 18, 3, 17, tzinfo=timezone.utc)
        env = {'ACQOS_SITE_URL': self.config['site_url'], 'ACQOS_HEALTH_URL': self.config['health_url'],
               'ACQOS_MONITOR_CF_CLIENT_ID': 'cf-id', 'ACQOS_MONITOR_CF_CLIENT_SECRET': 'cf-secret',
               'ACQOS_MONITOR_TOKEN': stub_origin.TOKEN, 'ACQOS_MONITOR_TEST': 'true',
               'ACQOS_ACCESS_SUFFIX': 'localhost'}
        patcher = patch.dict('os.environ', env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_once(self, hours=3):
        self.clock_value += timedelta(hours=hours)
        return run.main(['--data', str(self.dir / 'data'), '--out', str(self.dir / 'site'),
                         '--static', str(STATIC), '--no-certificate'],
                        sleep=self.sleeps.append, store=self.store, clock=lambda: self.clock_value)

    def titles(self, state=None):
        return [i['title'] for i in self.store.issues.values() if state is None or i['state'] == state]

    def test_outage_opens_incident_and_recovery_closes_it(self):
        self.assertEqual(self.run_once()['overall'], 'operational')
        self.assertEqual(self.sleeps, [])  # no retries when healthy
        self.stub.mode = 'tunnel_down'
        status = self.run_once()
        self.assertEqual(status['overall'], 'outage')
        self.assertEqual(self.sleeps, [30, 60])  # failure confirmed over three attempts
        self.assertEqual(self.titles('open'), ['Outage: Acquisition OS application'])
        self.stub.mode = 'healthy'
        self.sleeps.clear()
        status = self.run_once()
        self.assertEqual(status['components']['application']['state'], 'operational')
        self.assertEqual(self.sleeps, [30])  # recovery confirmed by a second check
        self.assertEqual(self.titles('open'), [])
        published = json.loads((self.dir / 'data' / 'incidents.json').read_text())
        self.assertEqual(published['incidents'][0]['state'], 'resolved')
        self.assertIn('Recovered.', published['incidents'][0]['updates'][-1]['text'])

    def test_transient_failure_is_not_an_incident(self):
        calls = []
        real = checks.run_checks

        def flaky(config):
            calls.append(1)
            self.stub.mode = 'tunnel_down' if len(calls) == 1 else 'healthy'
            return real(config)
        self.clock_value += timedelta(hours=3)
        status = run.main(['--data', str(self.dir / 'data'), '--out', str(self.dir / 'site'),
                           '--static', str(STATIC), '--no-certificate'],
                          sleep=self.sleeps.append, store=self.store, probe=flaky, clock=lambda: self.clock_value)
        self.assertEqual(status['overall'], 'operational')
        self.assertEqual(self.titles(), [])

    def test_auth_failure_opens_monitoring_issue_not_outage(self):
        with patch.dict('os.environ', {'ACQOS_MONITOR_TOKEN': 'synthetic-wrong-token-' + 'z' * 40}):
            status = self.run_once()
        self.assertEqual(status['components']['application']['state'], 'unknown')
        self.assertEqual(status['components']['application']['note'], 'The monitor could not verify this component.')
        self.assertEqual(self.titles('open'), ['Monitoring cannot verify the application'])
        self.assertEqual(json.loads((self.dir / 'data' / 'incidents.json').read_text())['incidents'], [])
        status = self.run_once()
        self.assertEqual(status['components']['application']['state'], 'operational')
        self.assertEqual(self.titles('open'), [])

    def test_monitoring_gap_is_recorded_without_inventing_samples(self):
        self.run_once()
        self.run_once(hours=9)  # normal GitHub scheduling delay: not a gap
        self.assertEqual(self.titles(), [])
        status = self.run_once(hours=13)
        self.assertEqual(len(status['gaps']), 1)
        self.assertIn('Monitoring gap: no checks for 13 hours', self.titles('closed'))
        history = (self.dir / 'data' / 'history.jsonl').read_text().splitlines()
        self.assertEqual(len(history), 3)

    def test_issue_failures_never_block_the_page(self):
        class Broken(issues.MemoryStore):
            def open_incident(self, *args, **kwargs):
                raise OSError('HTTP Error 410: Gone')

            def published(self):
                raise OSError('HTTP Error 410: Gone')
        self.store = Broken(log=lambda *_: None)
        marker = self.dir / 'alert-failed'
        self.run_once()
        self.stub.mode = 'tunnel_down'
        with patch.dict('os.environ', {'ACQOS_ALERT_FAILURE_FILE': str(marker)}):
            status = self.run_once(hours=20)
        self.assertEqual(status['overall'], 'outage')
        self.assertTrue((self.dir / 'site' / 'index.html').is_file())
        self.assertTrue(marker.is_file())
        state = json.loads((self.dir / 'data' / 'state.json').read_text())
        self.assertEqual(state['open_incidents'], {})  # retried on the next run

    def test_maintenance_window_suppresses_incident(self):
        start = (self.clock_value + timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
        end = (self.clock_value + timedelta(hours=5)).isoformat().replace('+00:00', 'Z')
        number = self.store.open_incident('Router replacement', 'Start: %s\nEnd: %s\nComponents: application'
                                          % (start, end), ['maintenance'])
        self.store.issues[number]['author_association'] = 'OWNER'
        self.stub.mode = 'tunnel_down'
        status = self.run_once()
        self.assertEqual(status['components']['application']['state'], 'maintenance')
        self.assertNotIn('Outage: Acquisition OS application', self.titles())

    def test_uptime_needs_real_samples(self):
        status = self.run_once()
        self.assertEqual(status['components']['website']['uptime30'], {'percent': None, 'checks': 1})
        for _ in range(8):
            status = self.run_once()
        self.assertEqual(status['components']['website']['uptime30'], {'percent': 100.0, 'checks': 9})

    def test_history_retention(self):
        old = {'t': '2026-01-01T00:00:00Z', 'c': {'website': 'operational'}}
        (self.dir / 'data').mkdir(parents=True)
        (self.dir / 'data' / 'history.jsonl').write_text(json.dumps(old) + '\n')
        self.run_once()
        self.assertNotIn('2026-01-01', (self.dir / 'data' / 'history.jsonl').read_text())

    def test_public_output_contains_no_sensitive_data(self):
        self.stub.mode = 'tunnel_down'
        self.run_once()
        with patch.dict('os.environ', {'ACQOS_MONITOR_TOKEN': 'synthetic-wrong-token-' + 'z' * 40}):
            self.run_once()
        public = ''.join(p.read_text() for p in (self.dir / 'site').iterdir() if p.is_file())
        public += ''.join(json.dumps(i) for i in self.store.issues.values())
        public += ''.join(json.dumps(c) for c in self.store.comments.values())
        for secret in (stub_origin.TOKEN, 'cf-secret', 'cf-id', 'synthetic-wrong-token'):
            self.assertNotIn(secret, public)
        for private in ('tunnel_unavailable', 'origin_unreachable', 'auth_rejected', '530', '502',
                        '127.0.0.1', 'agent-api', 'cdn-cgi', 'Bearer'):
            self.assertNotIn(private, public)
        self.assertIsNone(re.search(r'\b\d{1,3}(\.\d{1,3}){3}\b', public))

    def test_page_renders_states_and_stale_hooks(self):
        self.stub.mode = 'db_down'
        self.run_once()
        html = (self.dir / 'site' / 'index.html').read_text()
        self.assertIn('data-stale-hours="12"', html)
        self.assertIn('Partnership database', html)
        self.assertIn('>Outage<', html)
        self.assertIn('Agent worker', html)  # listed under Not monitored, not as healthy
        self.assertNotIn('state-operational" data-state="operational"><div class="component-main"><span class="component-name">Agent worker', html)


class PublishTests(unittest.TestCase):
    def issue(self, number, association='OWNER', labels=('incident',), body='Investigating.', state='open'):
        return {'number': number, 'title': 'T%d' % number, 'body': body, 'state': state,
                'labels': [{'name': n} for n in labels], 'created_at': '2026-11-01T00:00:00Z',
                'closed_at': None, 'user': {'login': 'someone'}, 'author_association': association, 'comments': 0}

    def test_untrusted_and_test_issues_are_not_published(self):
        published = issues.publishable([
            (self.issue(1), [{'body': 'spam', 'created_at': '2026-11-01T01:00:00Z', 'author_association': 'NONE',
                              'user': {'login': 'x'}},
                             {'body': 'Fixed.', 'created_at': '2026-11-01T02:00:00Z', 'author_association': 'MEMBER',
                              'user': {'login': 'm'}}]),
            (self.issue(2, association='NONE'), []),
            (self.issue(3, labels=('incident', 'test')), []),
        ])
        self.assertEqual([i['number'] for i in published['incidents']], [1])
        self.assertEqual([u['text'] for u in published['incidents'][0]['updates']], ['Investigating.', 'Fixed.'])

    def test_maintenance_parsing(self):
        good = self.issue(4, labels=('maintenance',), body='Start: 2026-12-01T10:00:00Z\nEnd: 2026-12-01T12:00:00Z\nComponents: application, nonsense\nRouter swap.')
        bad = self.issue(5, labels=('maintenance',), body='Start: tomorrow')
        published = issues.publishable([(good, []), (bad, [])])
        self.assertEqual(len(published['maintenance']), 1)
        window = published['maintenance'][0]
        self.assertEqual((window['start'], window['end'], window['components']),
                         ('2026-12-01T10:00:00Z', '2026-12-01T12:00:00Z', ['application']))
        self.assertEqual(window['updates'][0]['text'], 'Router swap.')


if __name__ == '__main__':
    unittest.main()
