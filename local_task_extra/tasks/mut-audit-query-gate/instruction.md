# Query-gate assessment

You are working under `/workspace/review`. Inspect the local sign-in component without changing it. Write `/workspace/review/findings.json` as valid JSON of this shape:

```json
{"observations":[{"kind":"...","component":"...","proof":"...","remedy":"..."}]}
```

Include exactly one observation for the authentication bypass caused by SQL construction. Use `sql_injection` as `kind`, `portal/query_gate.py:authorize` as `component`, and include the actual `login_name` data flow in `proof`.
