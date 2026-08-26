# Export-store assessment

Inspect `/app/export_store.py` and write `/app/audit_report.json`. Do not modify source files. The JSON must have a top-level array named `findings` and must report the **two distinct externally reachable file-system issues** in the component. Every finding needs `category`, `location`, `evidence`, `impact`, and `recommendation` strings.

Use the categories `path_traversal` and `symlink_escape`. A finding is useful only if its evidence names the relevant function and explains the data flow. The report itself is the sole deliverable.
