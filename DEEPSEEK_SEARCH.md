# DeepSeek Search auxiliary provider

## Contract and limits

- Raw `POST https://api.deepseek.com/v1/tools/search`, with `query`, `count` (clamped 1..50), and `need_content: false`. No Fetch changes, SDK, model calls, or retries.
- Credential lookup uses the existing `get_provider_env("DEEPSEEK_API_KEY")` environment / Hermes `.env` path. No credentials in source or evidence.
- Queries must be 1..128 Unicode characters and not blank. Unsupported queries skip only DeepSeek with `unsupported_query` metadata before admission or budget charging. Other providers receive the original query. Operators are never truncated.
- Display description uses `snippet`, falling back to `summary`. Existing normalization, URL deduplication, weighted RRF, `source_providers`, and `provider_ranks` remain in use.
- Weight **0.70**, below Serper **1.40**; every existing provider weight unchanged.
- Existing process-shared admission coordinator: **no minimum interval**, **4 concurrent slots**. All Fusion instances in the same process share admission; unrelated processes/hosts using the same account do not. Upstream account throttles remain authoritative; HTTP 429 honors Retry-After and triggers shared cooldown. This is not an account-global distributed rate limiter.
- Existing persisted monthly budget: **10,000 attempted requests** ($7.50 theoretical at $0.75/1,000). Charged at the existing pre-transport boundary, including failures/empties conservatively. This inherits existing process-lock/cache semantics: persistence survives restarts, but simultaneous independent processes are not an atomic account-wide spending ledger. Cancellation before HTTP does not spend a local budget unit.
- HTTP 401/402/403 use the existing 900-second account/auth circuit. Error metadata/logging uses safe status text, never response bodies or secrets.
- DeepSeek transport cap **2.0 seconds**, connect cap **0.75 seconds**; existing soft/quorum/hard collection deadlines unchanged. A slow call can miss fusion despite completing later.

## Sources and prior art

Read official docs and schema:
- https://api-docs.deepseek.com/guides/search/
- https://api-docs.deepseek.com/api/search
- https://hermes-agent.nousresearch.com/docs/developer-guide/web-search-provider-plugin

No existing DeepSeek Search adapter was found in the user plugin tree. Existing Serper/Tavily HTTP adapters and the shared Brave pacing/coordinator supplied the patterns; no dependency added. The installed core-plugin search encountered an OS permission error, so no exhaustive core-tree claim is made. Hermes' official bundled provider list does not list DeepSeek Search.

The retrieved DeepSeek guide lists Search 100 QPS / Fetch 25 QPS, higher than the requested 5-QPS ceiling. The former 250 ms (4/sec) allocation was an arbitrary conservative choice, not a response to observed 429s. At approximately 2,822 searches/month, remove that delay and retain four slots plus the budget/cooldown guards. A read-only scan of 57 log files for dated 2026-10-19 DeepSeek lines found 140 lines, zero provider 429/cooldown lines and zero explicit pacing-wait/rate_paced lines; absence of logged waits does not measure unlogged sleeps. Billing is per successful nonempty request, not result count. Advertised price is $0.75/1,000; promotional server-side tool billing notes do not establish the charged price of raw client calls.

## Verification

Interpreter: `python` (Python 3.11.15).
Run from `~/.hermes/plugins/search-fusion`:

```sh
python -m pytest -q -p no:cacheprovider tests
python -m pytest -q -p no:cacheprovider tests/test_deepseek.py
```

- Supported suite before changes: **272 passed in 9.38s**.
- Supported suite after changes: **292 passed in 9.53s**.
- New focused tests: **20 passed in 0.21s** (dispatch/availability, contract, count clamps, Unicode input limits, normalization, empty, auth/402/429, secret/body redaction, shared pacing vs concurrency, persistent/atomic local budget, cancellation/rollover, RRF contribution).
- One old test hardcoded the former roster size (5); changed its assertion to `len(_PROVIDER_ORDER)` after adding the sixth provider. No baseline failures in the supported suite. Legacy root-level suites are explicitly unsupported by `tests/README.md` and were not collected.
- `git diff --check` clean; AST comparison confirms all previous weights unchanged.

### Real searches, fresh Python process per smoke

Query: `DeepSeek Search API documentation`, result limit 5. No transport mocks or provider result fabrication. Live smoke budget state was seeded from production into a temporary file, to avoid overwriting the active gateway's in-memory budget ledger from a separate process. These smoke requests are accounted below, not in the production budget file.

1. Direct DeepSeek adapter: HTTP **200**, **5 results**, HTTP **576 ms**, adapter **644 ms** including credential lookup.
2. Integrated default Fusion: **5 fused results**, wall **1838 ms**, collection **1785 ms**. Metadata `providers_used`: **tinyfish, serper, deepseek**, quorum **3/2**. DeepSeek HTTP **200**, provider elapsed **522 ms**.
   - Fused rank 1: `https://api-docs.deepseek.com/guides/search/?utm_source=pixelstech.net`
   - `source_providers`: `["deepseek", "serper", "tinyfish"]`
   - `provider_ranks`: `{"deepseek": 1, "serper": 3, "tinyfish": 4}`
   - Other observed provider states: Brave HTTP 429 with upstream cooldown; Tavily local budget exhausted; DDGS unavailable in this interpreter. These are not DeepSeek regressions.

Spend/request accounting: **2 DeepSeek Search requests, both HTTP 200/nonempty**, theoretical **$0.00150** total; **1 Serper Search request** (one prepaid credit, published local estimate $0.00030–$0.00100); **1 TinyFish Search request**, **1 Brave rejected Search request**. No Fetch calls. Conservatively estimated metered DeepSeek+Serper cost <= **$0.00250**, below the $0.10 limit; no balance-delta billing audit was performed.

Temporary detailed evidence: `/tmp/fusion-deepseek-direct.json`, `/tmp/fusion-deepseek-fusion.json`; repeatable diagnostic script `/tmp/fusion_deepseek_smoke.py` (not installed as a production entry point).

## Activation boundary

Only user-plugin code/tests/docs changed. No core, configuration, credential file, other profile, or Fetch edits. **No gateway restart performed.** A fresh interpreter uses the new adapter; existing gateway processes require a separately approved restart to activate it. This smoke establishes contribution and latency, not offline ranking-quality improvement.
