"""Incidents and maintenance as GitHub issues in this repository.

- Label `incident`: an incident. The monitor opens these automatically (with
  `auto`); maintainers may open them by hand. Comments are updates; closing
  the issue resolves it.
- Label `maintenance`: planned maintenance. The body must contain
  `Start: <ISO-8601 UTC>` and `End: <ISO-8601 UTC>` lines and may contain
  `Components: application, database`. Closing it cancels the window.
- Label `monitoring`: problems with the monitor itself. Not shown on the page.
- Label `test`: synthetic and alert tests. Never shown on the production page.

Only issues and comments from repository owners, members, collaborators or
this workflow's bot are published. Anyone can open issues on a public
repository; their text never reaches the page.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import urllib.request

TRUSTED = {'OWNER', 'MEMBER', 'COLLABORATOR'}
BOT = 'github-actions[bot]'
MAX_TEXT = 2000
WINDOW_DAYS = 90
COMPONENT_IDS = ('website', 'access', 'application', 'database', 'directory', 'worker')


def _trusted(item):
    user = item.get('user') or {}
    return item.get('author_association') in TRUSTED or user.get('login') == BOT


def _utc(text):
    try:
        value = datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _field(body, name):
    match = re.search(r'^\s*%s:\s*(.+?)\s*$' % name, body or '', re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else None


def components_of(body):
    raw = _field(body, 'Components')
    if not raw:
        return []
    return [c for c in (part.strip().lower() for part in raw.split(',')) if c in COMPONENT_IDS]


def _clean(body):
    lines = [line for line in (body or '').splitlines()
             if not re.match(r'^\s*(Components|Start|End):', line, re.IGNORECASE)]
    return '\n'.join(lines).strip()[:MAX_TEXT]


def publishable(issues_with_comments, test=False):
    """Convert raw issues to the public incident/maintenance document."""
    incidents, maintenance = [], []
    for issue, comments in issues_with_comments:
        labels = {label['name'] if isinstance(label, dict) else label for label in issue.get('labels', [])}
        if ('test' in labels) != test or 'pull_request' in issue or not _trusted(issue):
            continue
        updates = [{'at': issue['created_at'], 'text': _clean(issue.get('body'))}]
        updates += [{'at': c['created_at'], 'text': _clean(c.get('body'))} for c in comments if _trusted(c)]
        updates = [u for u in updates if u['text']]
        entry = {'number': issue['number'], 'title': issue['title'][:200],
                 'state': 'resolved' if issue['state'] == 'closed' else 'open',
                 'createdAt': issue['created_at'], 'closedAt': issue.get('closed_at'),
                 'components': components_of(issue.get('body')), 'automatic': 'auto' in labels,
                 'updates': updates}
        if 'maintenance' in labels:
            start, end = _utc(_field(issue.get('body'), 'Start') or ''), _utc(_field(issue.get('body'), 'End') or '')
            if not start or not end or end <= start:
                continue  # Unparseable windows are ignored rather than guessed.
            entry.update(start=start, end=end, state='cancelled' if issue['state'] == 'closed' else 'scheduled')
            maintenance.append(entry)
        elif 'incident' in labels:
            incidents.append(entry)
    incidents.sort(key=lambda i: i['createdAt'], reverse=True)
    maintenance.sort(key=lambda m: m['start'])
    return {'incidents': incidents,
            'maintenance': [m for m in maintenance if m['state'] == 'scheduled'],
            'pastMaintenance': [m for m in maintenance if m['state'] != 'scheduled']}


class GitHubStore:
    def __init__(self, token, repository, api='https://api.github.com', prefix='', test=False, log=print):
        self.token, self.repository, self.api = token, repository, api.rstrip('/')
        self.prefix, self.test, self.log = prefix, test, log

    def _call(self, method, path, body=None):
        request = urllib.request.Request(
            self.api + path, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ACQOS-Monitor/1.0',
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b'null')

    def open_incident(self, title, body, labels, close=False):
        labels = list(labels) + (['test'] if self.test else [])
        issue = self._call('POST', '/repos/%s/issues' % self.repository,
                           {'title': self.prefix + title, 'body': body, 'labels': labels})
        if close:
            self._call('PATCH', '/repos/%s/issues/%d' % (self.repository, issue['number']),
                       {'state': 'closed', 'state_reason': 'completed'})
        return issue['number']

    def resolve(self, number, comment):
        self._call('POST', '/repos/%s/issues/%d/comments' % (self.repository, number), {'body': comment})
        self._call('PATCH', '/repos/%s/issues/%d' % (self.repository, number),
                   {'state': 'closed', 'state_reason': 'completed'})

    def published(self):
        since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        found = {}
        for label in ('incident', 'maintenance'):
            for issue in self._call('GET', '/repos/%s/issues?state=all&labels=%s&since=%s&per_page=100'
                                    % (self.repository, label, since)):
                found[issue['number']] = issue
        pairs = []
        for issue in found.values():
            comments = self._call('GET', '/repos/%s/issues/%d/comments?per_page=100'
                                  % (self.repository, issue['number'])) if issue.get('comments') else []
            pairs.append((issue, comments))
        return publishable(pairs, test=self.test)


class MemoryStore:
    """Local runs and tests: records actions instead of calling GitHub."""
    def __init__(self, log=print, test=False):
        self.log, self.test, self.issues, self.comments = log, test, {}, {}

    def open_incident(self, title, body, labels, close=False):
        number = len(self.issues) + 1
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        self.issues[number] = {'number': number, 'title': title, 'body': body, 'state': 'closed' if close else 'open',
                               'labels': [{'name': n} for n in list(labels) + (['test'] if self.test else [])],
                               'created_at': now, 'closed_at': now if close else None,
                               'user': {'login': BOT}, 'author_association': 'NONE'}
        self.comments[number] = []
        self.log('would open issue: %s' % title)
        return number

    def resolve(self, number, comment):
        if number not in self.issues:  # Opened by an earlier local process.
            self.log('would resolve issue %d' % number)
            return
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        self.comments[number].append({'body': comment, 'created_at': now, 'user': {'login': BOT},
                                      'author_association': 'NONE'})
        self.issues[number].update(state='closed', closed_at=now)
        self.log('would resolve issue %d' % number)

    def published(self):
        return publishable([(i, self.comments[n]) for n, i in self.issues.items()], test=self.test)


def from_environment(prefix='', log=print):
    test = os.environ.get('ACQOS_MONITOR_TEST') == 'true'
    token, repository = os.environ.get('GITHUB_TOKEN'), os.environ.get('GITHUB_REPOSITORY')
    if token and repository:
        return GitHubStore(token, repository, os.environ.get('GITHUB_API_URL', 'https://api.github.com'),
                           prefix=prefix, test=test, log=log)
    log('GITHUB_TOKEN not set: issue actions are recorded locally only')
    return MemoryStore(log=log, test=test)
