"""Render the public status page as static HTML from public data only."""
from html import escape
import json
from pathlib import Path
import shutil

LABELS = {'operational': 'Operational', 'degraded': 'Degraded', 'outage': 'Outage',
          'maintenance': 'Maintenance', 'unknown': 'Unknown'}
HEADLINES = {'operational': 'All monitored systems operational',
             'degraded': 'Some systems degraded',
             'outage': 'Service disruption',
             'maintenance': 'Maintenance in progress',
             'unknown': 'Status partly unknown'}
ORDER = ('website', 'access', 'application', 'database', 'directory', 'worker')
ICONS = {  # 24-unit outline icons; shape differs per state, not only colour.
    'operational': '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    'degraded': '<path d="M12 4l9 16H3z"/><path d="M12 10v4"/><path d="M12 17.2v.1"/>',
    'outage': '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    'maintenance': '<path d="M14.5 6.5a4 4 0 0 0-5 5L4 17l3 3 5.5-5.5a4 4 0 0 0 5-5l-2.5 2.5-2.5-.5-.5-2.5z"/>',
    'unknown': '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.4"/><path d="M12 17v.1"/>',
}


def icon(state):
    return ('<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">%s</svg>'
            % ICONS.get(state, ICONS['unknown']))


def when(value):
    if not value:
        return ''
    return '<time datetime="%s">%s</time>' % (escape(value), escape(value.replace('T', ' ').replace('Z', ' UTC')))


def text_block(text):
    return ''.join('<p>%s</p>' % escape(part).replace('\n', '<br>') for part in text.split('\n\n') if part.strip())


def uptime_text(item):
    up = item.get('uptime30') or {}
    if up.get('percent') is None:
        return 'Uptime: not enough data yet (%d checks in 30 days)' % up.get('checks', 0)
    return 'Uptime %.2f%% over 30 days (%d checks)' % (up['percent'], up['checks'])


def incident_html(entry, open_=False):
    updates = ''.join('<li>%s%s</li>' % (when(u['at']), text_block(u['text'])) for u in reversed(entry['updates']))
    status = 'Resolved %s' % when(entry['closedAt']) if entry['state'] == 'resolved' else 'Ongoing'
    if entry.get('start'):
        status = '%s to %s' % (when(entry['start']), when(entry['end']))
        if entry['state'] == 'cancelled':
            status += ' · Cancelled'
    return ('<details class="incident"%s><summary><span class="incident-title">%s</span>'
            '<span class="incident-meta">%s</span></summary><ol class="updates">%s</ol></details>'
            % (' open' if open_ else '', escape(entry['title']), status, updates))


def page(status, published):
    components = status.get('components', {})
    overall = status.get('overall', 'unknown')
    rows = []
    for name in ORDER:
        item = components.get(name)
        if not item:
            continue
        state = item['state']
        note = '<span class="note">%s</span>' % escape(item['note']) if item.get('note') else ''
        rows.append(
            '<li class="component state-%s" data-state="%s"><div class="component-main">'
            '<span class="component-name">%s</span>%s</div><div class="component-state">%s'
            '<span class="state-label">%s</span></div><div class="component-meta">%s</div></li>'
            % (state, state, escape(item['name']), note, icon(state), LABELS[state], escape(uptime_text(item))))
    not_monitored = ''.join('<li>%s</li>' % escape(n) for n in status.get('notMonitored', []))
    active = [i for i in published.get('incidents', []) if i['state'] == 'open']
    resolved = [i for i in published.get('incidents', []) if i['state'] == 'resolved']
    maintenance = published.get('maintenance', [])
    gaps = ''.join('<li>No checks from %s to %s</li>' % (when(g['from']), when(g['to']))
                   for g in reversed(status.get('gaps', [])))

    sections = []
    if active:
        sections.append('<section aria-labelledby="active"><h2 id="active">Active incidents</h2>%s</section>'
                        % ''.join(incident_html(i, open_=True) for i in active))
    if maintenance:
        sections.append('<section aria-labelledby="planned"><h2 id="planned">Planned maintenance</h2>%s</section>'
                        % ''.join(incident_html(m, open_=True) for m in maintenance))
    sections.append('<section aria-labelledby="components"><h2 id="components">Components</h2>'
                    '<ul class="components">%s</ul>%s</section>'
                    % (''.join(rows) or '<li class="empty">No checks recorded yet.</li>',
                       '<h3>Not monitored</h3><ul class="plain">%s</ul>' % not_monitored if not_monitored else ''))
    sections.append('<section aria-labelledby="history"><h2 id="history">Recent incidents</h2>%s</section>'
                    % (''.join(incident_html(i) for i in resolved) or '<p class="empty">No incidents in the last 90 days.</p>'))
    if gaps:
        sections.append('<section aria-labelledby="gaps"><h2 id="gaps">Monitoring gaps</h2><ul class="plain">%s</ul></section>' % gaps)

    generated = status.get('generatedAt', '')
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>ACQOS status</title>
<link rel="stylesheet" href="status.css">
<script src="status.js" defer></script>
</head>
<body data-generated="{escape(generated)}" data-stale-hours="{int(status.get('staleAfterHours', 7))}">
<main>
<header>
<h1>ACQOS status</h1>
<div class="overall state-{overall}" role="status" aria-live="polite">
{icon(overall)}<div><p class="headline">{HEADLINES[overall]}</p>
<p class="checked">Last checked {when(generated) or 'never'}</p></div>
</div>
<div class="stale" id="stale" hidden role="alert">{icon('unknown')}<p>Monitoring data is out of date. Current status is unknown.</p></div>
</header>
{''.join(sections)}
<footer><p>Checked every {int(status.get('intervalHours', 3))} hours from outside the ACQOS network. Status shows as unknown when the last check is more than {int(status.get('staleAfterHours', 7))} hours old.</p></footer>
</main>
</body>
</html>
'''


def build(status, published, static_dir, out_dir, data_dir=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ('status.css', 'status.js'):
        shutil.copyfile(static_dir / name, out_dir / name)
    (out_dir / 'index.html').write_text(page(status, published))
    (out_dir / 'status.json').write_text(json.dumps(status, indent=1, sort_keys=True))
    (out_dir / 'incidents.json').write_text(json.dumps(published, indent=1, sort_keys=True))
    (out_dir / '.nojekyll').write_text('')
