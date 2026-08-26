# Inventory filter hardening

Review `/app/catalog/filter.py`. The service normally listens on **9182**, but the verifier imports the production module directly. Fix `match_names(db_path, phrase)` without changing its signature or adding dependencies. Valid substring searches must keep their alphabetical results. A crafted `phrase` must not widen the query.
