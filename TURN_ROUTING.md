# Turn-aware search routing

Default ON. The first **search-bearing model response** in a user turn uses the
existing Fusion roster for every query; subsequent search batches use only
Serper. This is routing, not a search-sufficiency classifier.

## Identity and boundaries

The public `pre_tool_call` hook supplies `session_id`, `turn_id`, and
`api_request_id` (verified in host `agent/inline_tool_executors.py:tool_hook_ids`
and `agent/tool_executor.py:_pre_tool_block`). Because the host runs bounded
pre-tool hooks on separate threads, the plugin bridges those IDs in a locked
map keyed by (session, turn, tool call). At provider execution it reads the
host's approval observability ContextVars (a small optional private seam in
`tools.approval_context`) to recover the matching hook payload. Post-tool hooks
remove the bridge entry. Inline-hook/standalone callers also use a plugin
ContextVar. Missing host symbols fall back to session heuristics, not a crash.
The API request ID identifies the model
response/batch, not an individual query. A locked process-shared map keys on
(session, turn) and remembers the first search-bearing API request. Concurrent
siblings and delayed siblings of that first batch all receive Fusion. A new
turn ID resets immediately; a long pause within an identified turn does not.
No host monkeypatch is needed for this policy.

Compatibility fallback: if a caller lacks hook IDs, use session-context
`HERMES_SESSION_ID` / `HERMES_SESSION_KEY`. Without a turn ID, an idle gap of
120 seconds between search starts begins a new inferred turn. Without a batch
ID, starts within a **fixed 1-second window from the first search** share the
first batch (not a sliding window). These heuristics can conflate rapid turns,
split slow batches, or mistake a long research pause for a new turn. Context
lost across subprocess/programmatic tool boundaries cannot be reconstructed;
callers without any session identity always use Fusion rather than share a
global counter. State is process-local, bounded to 4096 LRU turn entries;
eviction/restart safely reverts the next search to first-batch Fusion.

Core query caching may satisfy a search without entering this provider. The
policy applies to actual provider invocations, not cache hits. Separate worker
processes do not share routing state. Normal direct web_search calls carry the
public hook IDs; custom execute_code workers require context propagation or
receive the documented fallback behavior.

## Configuration

Under `plugins.entries.search-fusion.fusion`:

```yaml
turn_routing_enabled: true
turn_idle_gap_s: 120.0
batch_window_s: 1.0
```

`SEARCH_FUSION_TURN_ROUTING=0` (also `false`, `off`, `no`) overrides the config
and restores all-Fusion routing. Settings are read with the existing provider
settings loader. No production config change is required. Metadata includes
`search_route` and `turn_boundary` so inferred boundaries are distinguishable
from exact hook IDs. Provider pacing, admission, budgets, roster, deadlines,
DeepSeek query checks/weight, and result provenance are unchanged.

## One-shot recovery

Any **query-level** error, timeout, HTTP 429, or empty web result retries once
with the full Fusion roster, including when the first batch was already Fusion.
A successful nonempty Fusion result with an individual failed index is not a
query failure and does not trigger another paid round. Missing/unavailable
Serper also falls back. The retry is a second bounded collection round, not a
recursive call; failure of that round is returned to the caller. Worst-case
collection latency can therefore be twice `first_result_timeout_s` (plus local
overhead). Provider HTTP calls already in flight retain their original socket
timeouts and lease accounting. Recovery never clears circuits/cooldowns,
replenishes budgets, or bypasses pacing/concurrency guards. A retry can have
zero admitted transports when every index is blocked.

`auto_retry_enabled: true` in the same config block and
`SEARCH_FUSION_AUTO_RETRY=0` independently control recovery. The master
`turn_routing_enabled: false` / `SEARCH_FUSION_TURN_ROUTING=0` restores the legacy
all-Fusion, single-round policy. Metadata includes `initial_search_route`,
final `search_route`, `retry_count`, `routing_elapsed_ms`, and `search_rounds`
with per-round provider outcomes. Existing top-level provider/collection
metadata describes the final round. Error diagnostics do not include exception
bodies, request URLs, or credentials.

## Verification

Two real searches in one fresh interpreter, driven by the installed host's
`PluginManager.invoke_hook` plus its real approval observability context:

- First batch (`Python official documentation pathlib`): Fusion, 4 rows,
  `providers_used=[serper, deepseek]`, wall **1877 ms**, collection **1876 ms**.
- Later batch (`Python official documentation concurrent futures`): Serper only,
  4 rows, `providers_used=[serper]`, wall **828 ms**, collection **825 ms**.
- Both reported `turn_boundary=hook_ids`, no retry. First top URL was
  `https://docs.python.org/3/library/pathlib.html`, with
  `source_providers=[serper]`, `provider_ranks={serper: 1}`. The contributing
  DeepSeek rows did not enter the requested top four; provider success is not
  the same as final-row contribution.
- Brave returned 429 and entered cooldown; Tavily's local budget was exhausted;
  DDGS unavailable; TinyFish missed the soft collection deadline. These did not
  prevent the successful query and correctly did not trigger retry.
- Actual transport attempts: Serper 2, DeepSeek 1, TinyFish 1, Brave 1; no Tavily
  or Fetch. Local budget was seeded into an isolated scratch ledger, never
  overwriting the gateway's ledger. No vendor billing/balance audit performed.
- Detailed smoke evidence:
  (local evidence file, not included).

## Log-only shadow Fusion (default ON)

A successful, nonempty later Serper-only batch enqueues an independent Fusion
counterfactual. The returned object is never changed: the worker reuses a value
snapshot of the Serper rows and **never calls Serper again**. Failed/retried
queries and first batches do not schedule shadow work.

Configuration under the same `fusion` block:

```yaml
shadow_enabled: true
shadow_daily_cap: 300
shadow_timeout_s: 3.0
```

`SEARCH_FUSION_SHADOW=0` (also false/off/no) always disables shadow, including
already queued jobs. No production configuration edit is required. A single
daemon collector consumes an eight-job queue; full queues drop immediately.
Six separate provider workers have a nonblocking submission bound, so timed-out
transports cannot grow an unbounded executor backlog or use real-search worker
threads. Foreground submission does no disk/network work, admission or waiting
for completion (only bounded snapshot/enqueue overhead).

Shadow admission tries the shared coordinator without waiting, skips occupied
slots and Brave pacing reservations, never polls, and reserves the last
DeepSeek slot for foreground work when the effective limit exceeds one. It
still respects cooldowns, authentication circuits and monthly spending. This
is best-effort priority, not preemption: an already-started shadow HTTP request
can occupy a slot until its transport finishes. The collector stops at the hard
per-run deadline; Python cannot forcibly kill in-flight HTTP threads. Their
socket phase timeouts are bounded by the run timeout and existing provider cap,
and their leases remain held until actual completion. Cancellation prevents
queued provider work from starting after the deadline.

The daily UTC run cap persists in `state/search-fusion/shadow-daily.json` with a
nonblocking local file lock; corrupt accounting fails closed. Shadow DeepSeek
attempts use the ordinary monthly ledger. Entire runs skip when DeepSeek has
less than 20% of its monthly allowance remaining. The inherited monthly ledger
is process-cached, not a distributed account-wide counter. Skipped admissions
still consume the daily run cap; dropped jobs and budget/kill-switch skips do not.

`~/.hermes/state/search-fusion/shadow.jsonl` stores timestamp, session/turn,
observed batch index, query, Serper/Fusion/Fusion-only URLs, successful providers,
per-provider status/latency, and only title/URL/snippet result fields (300 chars).
Known provider credentials and labeled secrets are redacted; URL userinfo,
query strings and fragments are discarded. No response/error bodies or headers
are recorded. A 5 MiB file rotates to one backup (about 10 MiB total bound).
Read-only report:

```sh
python shadow_report.py
python shadow_report.py /path/to/shadow.jsonl --db /path/to/state.db
```

The report counts retained runs/URL observations and matches Fusion-only URL
tokens in subsequent same-session `messages.tool_calls`, not response bodies.
It reports unavailable sessions/schema/database explicitly. This is URL reuse,
not evidence of usefulness; URL query stripping can conflate distinct pages,
rotation removes older observations, and the heuristic batch index is only an
observed sequence without exact hook IDs (old batch IDs are bounded to 256).

### Offline verification

Supported suite: **334 passed** versus the **318-test** baseline. New tests cover
non-waiting/unchanged foreground results, Serper reuse, slot/worker/queue/cap/
budget/switch skips, reserved DeepSeek capacity, hard deadline lease retention,
log schema/redaction/rotation, persisted daily cap, configuration, negative
routing paths, and read-only subsequent tool-call URL reporting.

### Live shadow verification

One paid foreground search, with the preceding first-batch routing identity
primed without a transport: `Python official documentation concurrent futures ThreadPoolExecutor`.
Returned four Serper-only results, wall **1347.02 ms**, collection/routing
**1331 ms**, shadow enqueue **0.0415 ms**. Shadow TinyFish took **2531 ms**,
DeepSeek **584 ms**; neither was awaited by the foreground. A real shadow log
line was read back. This establishes non-waiting submission, not a statistical
before/after latency claim. Actual transports: Serper 1, DeepSeek 1, TinyFish 1,
Brave 1 (429); Tavily budget exhausted and DDGS unavailable. The monthly ledger
was seeded into scratch, never replacing the gateway ledger. DeepSeek theoretical
cost $0.00075 plus one Serper credit; no balance-delta billing audit.
Evidence: (local evidence file, not included).

## Activation

Files and fresh-interpreter tests do not activate code in the running gateway.
An approved gateway restart is needed. No restart is performed by this change.
