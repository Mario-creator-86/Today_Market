# Market Intelligence & Decision Hub

The authoritative specification is ../masterprompt.md. Build a factual decision-support system; never execute real trades. Facts, interpretation, observed reactions and deterministic decisions remain separate. Missing or stale critical data produces WAIT, never invented prices or confidence.

Implementation order follows the fourteen phases in PROGRESS.md. Live integrations require provider entitlement and connectivity. No credentials are present in the repository. Local development uses SQLite when PostgreSQL is unavailable; production uses PostgreSQL, TimescaleDB, pgvector and Redis.

Acceptance: safe decision gates, reproducible snapshots, provider health, persisted evidence, typed API, operational dashboard, UTC storage/Bucharest display, regression tests and documented launch commands. External integration validation and statistical calibration must be reported separately from offline tests.
