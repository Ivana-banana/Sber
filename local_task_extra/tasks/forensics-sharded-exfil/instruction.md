# Case 88: shard recovery

Review all artifacts below `/app/shards`. Write `/app/incident_report.txt` with exactly five `key=value` lines: `attacker_ip`, `compromised_user`, `dataset`, `exfil_bytes`, `first_malicious_event_utc`. No spaces, blank lines, or commentary are allowed.

A disk snapshot is incomplete. The attributable primary exfiltration is the first complete chain where an edge event with `decision=ALLOW` matches an audit record with `event=bulk_export`; derive the client IP from the corresponding flow record's `xff`. PTR notes and lines marked `DECOY` are context only. Read all shard files.
