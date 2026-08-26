# Credential review — relay adapter

Review the source under `/app` without modifying it. Write `/app/audit_report.json` as UTF-8 JSON with one top-level key, `findings`, whose value is an array.

The report must contain **exactly one** finding for the credential that is usable by a production relay. The finding must contain the string keys `category`, `location`, `evidence`, `severity`, and `recommendation`. Use `hardcoded_secret` as its category and give concrete evidence; prose outside the JSON file is ignored.
