# DeepSeek Search auxiliary provider

## Contract and limits

- Raw `POST https://api.deepseek.com/v1/tools/search`, with `query`, `count` (clamped 1..50), and `need_content: false`. No SDK, model calls, or adapter-level retries.
- Credential lookup uses the existing `get_provider_env("DEEPSEEK_API_KEY")` environment / Hermes `.env` path. No credentials in source or evidence.
- Queries must be 1..128 Unicode characters and not blank. Unsupported queries skip only DeepSeek with `unsupported_query` metadata before admission or budget charging. Other providers receive the original query. Operators are never truncated.
- Display description uses `snippet`, falling back to `summary`. Existing normalization, URL deduplication, weighted RRF, `source_providers`, and `provider_ranks` remain in use.
- Weight **0.70**, below Serper **1.40**; every existing provider weight unchanged.
- Existing process-shared admission coordinator: **no minimum interval**, **4 concurrent slots**. All Fusion instances in the same process share admission; unrelated processes/hosts using the same account do not. Upstream account throttles remain authoritative; HTTP 429 honors Retry-After and triggers shared cooldown. This is not an account-global distributed rate limiter.
- Existing persisted monthly budget: **10,000 attempted requests**. Charged at the existing pre-transport boundary, including failures/empties conservatively. This inherits existing process-lock/cache semantics: persistence survives restarts, but simultaneous independent processes are not an atomic account-wide spending ledger. Cancellation before HTTP does not spend a local budget unit.
- HTTP 401/402/403 use the existing 900-second account/auth circuit. Error metadata/logging uses safe status text, never response bodies or secrets.
- DeepSeek transport cap **2.0 seconds**, connect cap **0.75 seconds**; existing soft/quorum/hard collection deadlines unchanged. A slow call can miss fusion despite completing later.

## Design and verification

The adapter follows the public search endpoint contract. Existing HTTP adapters,
admission, cooldown and request-budget mechanisms are shared; no SDK or separate
retry loop is added. Four concurrent slots bound auxiliary load without imposing
an additional per-request sleep. Upstream throttles still apply.

References:
- https://api-docs.deepseek.com/guides/search/
- https://api-docs.deepseek.com/api/search
- https://hermes-agent.nousresearch.com/docs/developer-guide/web-search-provider-plugin

From the plugin checkout, using the Hermes Python environment:

```sh
python -m pytest -q -p no:cacheprovider tests
python -m pytest -q -p no:cacheprovider tests/test_deepseek.py
```

Offline tests cover availability, request/count bounds, Unicode query validation,
normalization, empty responses, auth/quota/throttle responses, error redaction,
shared admission, persisted local budgets, cancellation, rollover and ranking
contribution. They do not establish live API availability, billing or relevance.
New sessions/processes load code changes; long-running hosts need a restart.
