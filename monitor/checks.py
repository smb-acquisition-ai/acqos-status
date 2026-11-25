"""External probes and their classification. Standard library only.

Each probe returns a Result with a coarse, public-safe `state` and a private
`reason` code. Reasons are printed to the workflow log only as these fixed
codes; response bodies, headers and credentials are never logged or stored.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = 'ACQOS-Monitor/1.0'
TIMEOUT = 20
MAX_BODY = 65536
CERT_WARNING_DAYS = 14
# Synthetic tests point this at the local stub; production keeps the default.
ACCESS_SUFFIX = '.cloudflareaccess.com'

OPERATIONAL, DEGRADED, OUTAGE, MAINTENANCE, UNKNOWN = (
    'operational', 'degraded', 'outage', 'maintenance', 'unknown')
RANK = {OPERATIONAL: 0, DEGRADED: 1, UNKNOWN: 2, OUTAGE: 3}


@dataclass
class Result:
    state: str
    reason: str
    details: dict = field(default_factory=dict)  # Private; never published.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _opener(context=None):
    handlers = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def fetch(url, headers=None, context=None, timeout=TIMEOUT):
    """Return (status, headers, body) or raise OSError-like errors.

    Redirects are not followed, so an Access login redirect stays visible.
    """
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, **(headers or {})})
    try:
        with _opener(context).open(request, timeout=timeout) as response:
            return response.status, response.headers, response.read(MAX_BODY)
    except urllib.error.HTTPError as error:
        try:
            body = error.read(MAX_BODY)
        except Exception:
            body = b''
        return error.code, error.headers, body


def _network_reason(error):
    text = repr(error)
    if isinstance(error, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in text:
        return 'tls_invalid'
    if isinstance(error, (socket.gaierror,)) or 'Name or service not known' in text or 'getaddrinfo' in text:
        return 'dns_failure'
    if isinstance(error, (TimeoutError, socket.timeout)) or 'timed out' in text:
        return 'timeout'
    return 'unreachable'


def _is_access_location(headers):
    location = (headers or {}).get('Location', '') if headers else ''
    host = urllib.parse.urlsplit(location).hostname or ''
    return host.endswith(ACCESS_SUFFIX), location


def _edge_block(status, body):
    """Cloudflare security block of this client (for example error 1010/1020).

    The site may be fine for people; the monitor simply was not allowed in.
    """
    if status not in (403, 429):
        return False
    text = (body or b'').strip()
    # Cloudflare security blocks answer with a short plain "error code: 10xx"
    # or an empty body; Access and the app never do.
    return not text or re.search(rb'error code: 10\d\d', text) is not None


def _json(body, headers):
    content_type = (headers or {}).get('Content-Type', '') if headers else ''
    if 'json' not in content_type:
        return None
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def certificate_days(url, context=None, timeout=TIMEOUT):
    """Days until the served certificate expires, verified against system roots."""
    parts = urllib.parse.urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    context = context or ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            not_after = tls.getpeercert()['notAfter']
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), timezone.utc)
    return (expires - datetime.now(timezone.utc)).total_seconds() / 86400


def check_website(url, context=None, check_certificate=True):
    """DNS, TLS and the Cloudflare edge. A login redirect is the healthy answer.

    This proves the public front door only. Cloudflare answers it without
    contacting the Mini, so it says nothing about the tunnel or the app.
    """
    try:
        status, headers, body = fetch(url, context=context)
    except Exception as error:  # noqa: BLE001 - classify every network failure
        return Result(OUTAGE, _network_reason(error))
    if _edge_block(status, body):
        return Result(UNKNOWN, 'edge_blocked')
    access, location = _is_access_location(headers)
    if status in (301, 302, 303, 307, 308) and access:
        result = Result(OPERATIONAL, 'access_redirect', {'login_url': location})
    elif 500 <= status < 600:
        return Result(OUTAGE, 'edge_http_%d' % status)
    else:
        return Result(DEGRADED, 'unexpected_http_%d' % status)
    if check_certificate:
        try:
            days = certificate_days(url, context=context)
        except Exception as error:  # noqa: BLE001
            return Result(OUTAGE, _network_reason(error))
        result.details['certificate_days'] = round(days, 1)
        if days < CERT_WARNING_DAYS:
            return Result(DEGRADED, 'certificate_expiring', result.details)
    return result


def check_access(login_url, context=None):
    """The Cloudflare Access login page responds. This is not an app login."""
    if not login_url:
        return Result(UNKNOWN, 'no_login_url')
    if not (urllib.parse.urlsplit(login_url).hostname or '').endswith(ACCESS_SUFFIX):
        return Result(UNKNOWN, 'unexpected_login_host')
    try:
        status, _, body = fetch(login_url, context=context)
    except Exception as error:  # noqa: BLE001
        return Result(OUTAGE, _network_reason(error))
    if status == 200 and body:
        return Result(OPERATIONAL, 'login_page_ok')
    if 500 <= status < 600:
        return Result(OUTAGE, 'login_http_%d' % status)
    return Result(DEGRADED, 'login_http_%d' % status)


def check_application(health_url, client_id, client_secret, token, context=None):
    """Authenticated request through Access and the tunnel to the app.

    Returns the application Result and the app's component report (or None).
    Authentication failures are UNKNOWN, never OUTAGE: they mean the monitor
    cannot see the app, not that the app is down.
    """
    if not (health_url and client_id and client_secret and token):
        return Result(UNKNOWN, 'monitor_not_configured'), None
    headers = {'CF-Access-Client-Id': client_id, 'CF-Access-Client-Secret': client_secret,
               'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    try:
        status, response_headers, body = fetch(health_url, headers=headers, context=context)
    except Exception as error:  # noqa: BLE001
        return Result(OUTAGE, _network_reason(error)), None
    if _edge_block(status, body):
        return Result(UNKNOWN, 'edge_blocked'), None
    payload = _json(body, response_headers)
    access, _ = _is_access_location(response_headers)
    if status == 200 and payload and payload.get('status') in ('ok', 'degraded', 'down'):
        components = payload.get('components') if isinstance(payload.get('components'), dict) else {}
        app_state = {'ok': OPERATIONAL, 'degraded': DEGRADED, 'down': DEGRADED}[payload['status']]
        return Result(app_state, 'health_' + payload['status']), components
    if status in (301, 302, 303, 307, 308) and access:
        return Result(UNKNOWN, 'auth_rejected_edge'), None
    if status in (401, 403):
        # JSON comes from the ACQOS gateway/app; HTML comes from Cloudflare Access.
        return Result(UNKNOWN, 'auth_rejected_app' if payload else 'auth_rejected_edge'), None
    if status == 530:
        return Result(OUTAGE, 'tunnel_unavailable'), None
    if status in (502, 503, 504) and payload:
        return Result(OUTAGE, 'app_unavailable'), None
    if 500 <= status < 600:
        return Result(OUTAGE, 'origin_unreachable'), None
    return Result(UNKNOWN, 'unexpected_http_%d' % status), None


COMPONENT_STATES = {'ok': OPERATIONAL, 'fail': OUTAGE, 'disabled': UNKNOWN}


def app_component(components, name, app_result, monitored=('database',)):
    """Map one app-reported component, or None when it is not monitored."""
    if components is None:
        return Result(UNKNOWN, 'application_unverified') if name in monitored else None
    value = components.get(name)
    if value in (None, 'not_configured'):
        return None
    if value not in COMPONENT_STATES:
        return Result(UNKNOWN, 'unexpected_component_state')
    return Result(COMPONENT_STATES[value], 'component_' + value)


def run_checks(config, context=None):
    """Run every probe once. Returns {component_id: Result}."""
    global ACCESS_SUFFIX
    ACCESS_SUFFIX = config.get('access_suffix') or '.cloudflareaccess.com'
    results = {}
    website = check_website(config['site_url'], context=context,
                            check_certificate=config.get('check_certificate', True))
    results['website'] = website
    results['access'] = check_access(website.details.get('login_url'), context=context)
    app, components = check_application(config.get('health_url'), config.get('client_id'),
                                        config.get('client_secret'), config.get('token'), context=context)
    results['application'] = app
    # The company directory is out of scope (Jose, 2026-11-24): never reported.
    for name in ('database', 'worker'):
        component = app_component(components, name, app,
                                  config.get('app_components') or ('database',))
        if component is not None:
            results[name] = component
    return results
