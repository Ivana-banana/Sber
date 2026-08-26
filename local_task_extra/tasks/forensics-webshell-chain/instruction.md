# Web telemetry investigation

Inspect every artifact below `/app/evidence`. Produce `/app/incident_report.txt` with exactly four non-empty `key=value` lines and no other text. The required keys are `attacker_ip`, `compromised_user`, `webshell_path`, and `exfil_bytes`.

The report concerns the successful chain that starts with a URL-encoded upload, reaches an application execution audit event, and is permitted by the edge. Decode URL encoding where necessary. Do not rely on filename order.
