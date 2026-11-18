"""One monitoring run: probe, confirm, record, alert, render.

Thresholds (see the operating reference for rationale):
- A non-operational result is re-checked twice (after 30 s and 60 s). The
  component keeps the best result seen, so only a failure that persists for
  about 90 s counts.
- A component in outage recovers only after a second operational check 30 s
  after the first.
- A gap longer than STALE_HOURS since the previous run is recorded as a
  monitoring gap. No samples are invented for it.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks  # noqa: E402
from checks import OPERATIONAL, DEGRADED, OUTAGE, MAINTENANCE, UNKNOWN, RANK  # noqa: E402
import issues as issue_store  # noqa: E402
import render  # noqa: E402

INTERVAL_HOURS = 3
STALE_HOURS = 7
RETENTION_DAYS = 90
RETRY_DELAYS = (30, 60)
RECOVERY_DELAY = 30

COMPONENTS = {
    'website': 'Website (jgdesk.com)',
    'access': 'Sign-in (Cloudflare Access)',
    'application': 'Acquisition OS application',
    'database': 'Partnership database',
    'directory': 'Company directory',
    'worker': 'Agent worker',
}
# Components whose outage opens an incident. Others show on the page only.
INCIDENT_COMPONENTS = ('website', 'access', 'application', 'database', 'directory')

# Fixed public notes. Private reason codes never reach the page.
PUBLIC_NOTES = {
    'auth_rejected_edge': 'The monitor could not verify this component.',
    'auth_rejected_app': 'The monitor could not verify this component.',
    'edge_blocked': 'The monitor could not verify this component.',
    'monitor_not_configured': 'Not monitored yet.',
    'application_unverified': 'Not verified while the application is unavailable.',
    'component_disabled': 'Disabled.',
    'certificate_expiring': 'Certificate renewal due.',
}


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def parse_iso(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=1, sort_keys=True) + '\n')


def best(a, b):
    return a if RANK.get(a.state, 9) <= RANK.get(b.state, 9) else b


def confirmed_results(config, previous, sleep=time.sleep, probe=checks.run_checks):
    results = probe(config)
    for delay in RETRY_DELAYS:
        if all(r.state == OPERATIONAL for r in results.values()):
            break
        sleep(delay)
        again = probe(config)
        for name, result in again.items():
            results[name] = best(results[name], result) if name in results else result
        if again['application'].reason.startswith('health_'):
            # The app answered: it is authoritative about what it monitors.
            for name in [n for n in results if n not in again]:
                del results[name]
    recovering = [n for n, r in results.items() if r.state == OPERATIONAL and previous.get(n) == OUTAGE]
    if recovering:
        sleep(RECOVERY_DELAY)
        again = probe(config)
        for name in recovering:
            if again.get(name) is None or again[name].state != OPERATIONAL:
                results[name] = again.get(name) or checks.Result(UNKNOWN, 'recovery_unconfirmed')
    return results


def apply_maintenance(results, windows, at):
    active = set()
    for window in windows:
        if window['start'] <= iso(at) < window['end']:
            active.update(window['components'] or COMPONENTS)
    for name in list(results):
        if name in active:
            results[name] = checks.Result(MAINTENANCE, 'maintenance', results[name].details)
    return active


def overall(states):
    values = list(states.values())
    if not values:
        return UNKNOWN
    if OUTAGE in values:
        return OUTAGE
    if DEGRADED in values:
        return DEGRADED
    if MAINTENANCE in values:
        return MAINTENANCE
    if UNKNOWN in values:
        return UNKNOWN
    return OPERATIONAL


def prune_history(lines, at):
    cutoff = iso(at - timedelta(days=RETENTION_DAYS))
    return [line for line in lines if line['t'] >= cutoff]


def uptime(history, name, at, days):
    cutoff = iso(at - timedelta(days=days))
    samples = [line['c'].get(name) for line in history if line['t'] >= cutoff]
    counted = [s for s in samples if s in (OPERATIONAL, DEGRADED, OUTAGE)]
    if len(counted) < 8:
        return {'percent': None, 'checks': len(counted)}
    up = sum(1 for s in counted if s != OUTAGE)
    return {'percent': round(100 * up / len(counted), 2), 'checks': len(counted)}


def read_history(path):
    lines = []
    try:
        for raw in Path(path).read_text().splitlines():
            if raw.strip():
                lines.append(json.loads(raw))
    except (OSError, ValueError):
        pass
    return lines


def write_history(path, lines):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(''.join(json.dumps(line, sort_keys=True) + '\n' for line in lines))


def sync_incidents(store, state, results, active_maintenance, at, log):
    """Open an issue per component outage; comment and close on recovery."""
    open_auto = state.setdefault('open_incidents', {})
    for name in INCIDENT_COMPONENTS:
        result = results.get(name)
        if result is None:
            continue
        number = open_auto.get(name)
        if result.state == OUTAGE and number is None and name not in active_maintenance:
            number = store.open_incident(
                'Outage: %s' % COMPONENTS[name],
                'The monitor detected an outage and will post here when it recovers.\n\nComponents: %s'
                % name, ['incident', 'auto'])
            open_auto[name] = number
            log('incident opened for %s' % name)
        elif result.state == OPERATIONAL and number is not None:
            store.resolve(number, 'Recovered. %s is operational as of %s.' % (COMPONENTS[name], iso(at)))
            del open_auto[name]
            log('incident resolved for %s' % name)
    # Monitoring identity problems: one internal issue, never an outage.
    auth = results.get('application')
    auth_failed = auth is not None and auth.reason in ('auth_rejected_edge', 'auth_rejected_app', 'edge_blocked')
    number = state.get('open_monitoring_auth')
    if auth_failed and number is None:
        state['open_monitoring_auth'] = store.open_incident(
            'Monitoring cannot verify the application',
            'The monitoring identity was rejected (%s). The application may be healthy; '
            'its status shows as unknown until the monitoring credential works again.\n\nDetected: %s'
            % ({'auth_rejected_edge': 'at the sign-in layer', 'edge_blocked': 'by edge security rules'}
               .get(auth.reason, 'by the application'), iso(at)),
            ['monitoring'])
        log('monitoring authentication issue opened')
    elif not auth_failed and number is not None and auth is not None and auth.state != UNKNOWN:
        store.resolve(number, 'Monitoring credential accepted again as of %s.' % iso(at))
        state['open_monitoring_auth'] = None
        log('monitoring authentication issue resolved')


def record_gap(store, state, previous_time, at, log):
    if previous_time is None:
        return None
    gap = at - parse_iso(previous_time)
    if gap <= timedelta(hours=STALE_HOURS):
        return None
    store.open_incident(
        'Monitoring gap: no checks for %d hours' % (gap.total_seconds() // 3600),
        'No monitoring checks ran between %s and %s. Status for that period is unknown; '
        'uptime excludes it.' % (previous_time, iso(at)), ['monitoring', 'gap'], close=True)
    log('monitoring gap recorded')
    return {'from': previous_time, 'to': iso(at)}


def main(argv=None, sleep=time.sleep, probe=checks.run_checks, store=None, clock=now_utc):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data')
    parser.add_argument('--out', default='_site')
    parser.add_argument('--static', default=str(Path(__file__).resolve().parents[1] / 'site'))
    parser.add_argument('--no-certificate', action='store_true')
    parser.add_argument('--title-prefix', default='')
    args = parser.parse_args(argv)

    data = Path(args.data)
    config = {'site_url': os.environ.get('ACQOS_SITE_URL', 'https://jgdesk.com/'),
              'health_url': os.environ.get('ACQOS_HEALTH_URL', ''),
              'client_id': os.environ.get('ACQOS_MONITOR_CF_CLIENT_ID', ''),
              'client_secret': os.environ.get('ACQOS_MONITOR_CF_CLIENT_SECRET', ''),
              'token': os.environ.get('ACQOS_MONITOR_TOKEN', ''),
              'check_certificate': not args.no_certificate,
              'access_suffix': os.environ.get('ACQOS_ACCESS_SUFFIX') if os.environ.get('ACQOS_MONITOR_TEST') == 'true' else None}
    log = lambda message: print('[monitor] ' + message, flush=True)  # noqa: E731
    store = store or issue_store.from_environment(prefix=args.title_prefix, log=log)

    state = load_json(data / 'state.json', {})
    previous_status = load_json(data / 'status.json', {})
    previous_states = {k: v['state'] for k, v in previous_status.get('components', {}).items()}
    history = read_history(data / 'history.jsonl')
    known = [n for n in ('database', 'directory', 'worker') if n in previous_states]
    config['app_components'] = tuple(known) or ('database', 'directory')

    started = clock()
    gap = record_gap(store, state, previous_status.get('generatedAt'), started, log)
    published = store.published()  # incidents + maintenance windows from issues

    results = confirmed_results(config, previous_states, sleep=sleep, probe=probe)
    at = clock()
    active = apply_maintenance(results, published['maintenance'], at)
    for name, result in sorted(results.items()):
        log('%s: %s (%s)' % (name, result.state, result.reason))  # fixed codes only

    sync_incidents(store, state, results, active, at, log)
    published = store.published()

    states = {name: r.state for name, r in results.items()}
    history.append({'t': iso(at), 'c': states})
    history = prune_history(history, at)
    status = {
        'generatedAt': iso(at),
        'intervalHours': INTERVAL_HOURS,
        'staleAfterHours': STALE_HOURS,
        'overall': overall(states),
        'components': {
            name: {'name': COMPONENTS[name], 'state': r.state,
                   'note': PUBLIC_NOTES.get(r.reason, ''), 'checkedAt': iso(at),
                   'uptime30': uptime(history, name, at, 30),
                   'uptime90': uptime(history, name, at, 90)}
            for name, r in results.items()},
        'notMonitored': [COMPONENTS[n] for n in COMPONENTS if n not in results],
    }
    if gap:
        state.setdefault('gaps', []).append(gap)
    state['gaps'] = [g for g in state.get('gaps', []) if g['to'] >= iso(at - timedelta(days=RETENTION_DAYS))]
    status['gaps'] = state['gaps']

    write_json(data / 'status.json', status)
    write_json(data / 'incidents.json', published)
    write_json(data / 'state.json', state)
    write_history(data / 'history.jsonl', history)
    render.build(status, published, Path(args.static), Path(args.out), data)
    log('overall: %s' % status['overall'])
    return status


if __name__ == '__main__':
    main()
