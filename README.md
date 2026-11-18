# ACQOS status

Public status page and external monitoring for Acquisition OS.

- Page: https://smb-acquisition-ai.github.io/acqos-status/
- Checks run every 3 hours in GitHub Actions (`.github/workflows/monitor.yml`)
  and record coarse states in `data/`.
- Incidents and planned maintenance are GitHub issues labelled `incident` or
  `maintenance`. Only maintainers' issues and comments are published.

Do not put credentials, internal hostnames, IP addresses, error output or
business records in this repository, its issues or its workflow logs. It is
public.

The operating reference is maintained in the private ACQOS repository at
`docs/operations/monitoring.md`.

Tests: `python3 -m unittest monitor.test_monitor`
