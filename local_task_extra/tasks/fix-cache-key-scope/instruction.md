# Profile cache incident

Fix the security defect in `/app/profile_cache.py`. `render_profile(tenant, user, now)` is a production API and must remain present. A cached profile must be reused for the same tenant/user inside the TTL, but must never be returned to a different tenant with the same user name. Do not remove caching or add dependencies.

The directory contains a large diagnostics export whose strings are not source-of-truth. Modify production code only; the verifier will import it in a clean Python process.
