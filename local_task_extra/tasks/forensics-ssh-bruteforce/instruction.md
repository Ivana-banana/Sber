# SSH authentication triage

Read all files under `/app/case`. Write `/app/incident_report.txt` with **exactly three** non-empty `key=value` lines, no spaces around `=` and no commentary. Include each key once: `attacker_ip`, `compromised_user`, `first_success_utc`.

Attribute the user and source IP from the first successful authentication that followed failed attempts for the same account. Copy the timestamp verbatim from the event.
