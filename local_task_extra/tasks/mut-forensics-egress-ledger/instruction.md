# Case 42: partner-ledger egress

Review all material under `/var/lib/case-42`. Write `/var/lib/case-42/conclusion.env` as UTF-8 text with exactly five non-empty `key=value` lines. Use each key once: `client_ip`, `principal`, `object_name`, `logical_bytes`, `event_time`.

The attributable event is the first export audit whose `decision=permit` ledger entry matches the same `trace`, and whose flow record carries an `xff`. Copy the audit timestamp exactly. The retained `hint` file is not evidence.
