# search-fusion

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for bounded,
budget-aware search fusion and guarded, local-first URL extraction.

- The active search roster is **tinyfish, serper, brave-free, tavily, deepseek,
  ddgs**. Available providers run in parallel in the first search batch of a
  turn; later batches use Serper. A failed/empty query gets one bounded Fusion
  retry. Missing keys/dependencies skip individual providers.
- Exa and MWMBL clients are retained as inactive experiments; they are absent
  from the roster, including availability checks. Setting an Exa key does not
  enable Fusion. Firecrawl is not used for search or extraction.
- `web_fetch` and optional `frugal-extract` validate public HTTP(S) targets,
  resolve hostnames, pin validated addresses and check every redirect hop.
  Environment HTTP proxies are bypassed so DNS cannot be delegated after validation.
  Defuddle parses a locally fetched HTML file with secondary networking blocked.
  Remote TinyFish Fetch/Firecrawl extraction are disabled because this plugin
  cannot validate their redirect hops. `mode: rendered` returns browser guidance;
  JavaScript rendering and authenticated pages require a separate browser.
- **Shadow diagnostics are OFF by default**. Opting in can issue additional
  paid provider requests and retain query text locally; see below.

## Install

Requires Hermes Agent's Python environment and plugin/provider APIs. Configuration
keys and provider registration were checked against Hermes revision
`8a66c7e867be4a9ba859eceef1a1e393fb7aba8f`; offline behavior tests use that host.
Other host revisions have not been verified.

```sh
git clone https://github.com/kyupark/search-fusion "${HERMES_HOME:-$HOME/.hermes}/plugins/search-fusion"
hermes plugins enable search-fusion
npm install -g defuddle
```

Defuddle requires Node.js/npm; its executable must be on the Hermes process's
`PATH`. See the [Defuddle CLI documentation](https://github.com/kepano/defuddle#cli).
Raw text/JSON and ATS posting extraction work without it; article extraction
falls back to plain HTML-to-text if Defuddle is unavailable. No remote extraction
service is required or used.

In the active Hermes home's `config.yaml` select the contributed provider:

```yaml
web:
  search_backend: search-fusion
  extract_backend: frugal-extract  # optional: route web_extract through this plugin
```

The plugin also registers its own `web_fetch` tool independently of
`web.extract_backend`. Start a new Hermes session, or restart the relevant
long-running Hermes process, after enabling/configuring it.

Set the keys you have in the active Hermes environment or `.env`:

| Provider | Key/dependency |
| --- | --- |
| tinyfish | `TINYFISH_API_KEY` (direct search API; no MCP server needed) |
| serper | `SERPER_API_KEY` |
| brave-free | `BRAVE_SEARCH_API_KEY` |
| tavily | `TAVILY_API_KEY` |
| deepseek | `DEEPSEEK_API_KEY` |
| ddgs | Optional `ddgs` Python package; no API key |

Install optional `ddgs` and development `pytest` into the same Python environment
that runs Hermes (for example `python -m pip install ddgs pytest` with that
environment active). Hermes supplies `httpx` and the plugin APIs. TinyFish MCP
Fetch is not a dependency; direct TinyFish MCP Search/Fetch tools are hidden to
keep routing on the plugin surface.

## Budgets, diagnostics and privacy

Local UTC monthly attempted-request caps in `fusion.py` are Tavily **950**,
Serper **6,000**, and DeepSeek **10,000**. Failed/empty attempts count too;
foreground and shadow work share these caps. Other providers have no local
monthly cap. These are request limits, not currency limits or vendor account
balances. Provider prices/quotas and spending by other clients remain outside
this ledger. The ledger is process-cached, not atomic across independent
processes; unreadable accounting starts fresh and unwritable state falls back
to in-process counting, so it is not a guaranteed billing ceiling.

Paths resolve through `hermes_constants.get_hermes_home()` at use time, respecting
`HERMES_HOME` and host profile overrides (fallback `~/.hermes` without the host):

- `state/fusion-provider-budget.json`: monthly counts and exhaustion flags.
- `state/search-fusion/shadow.jsonl`: opt-in query/result diagnostics, rotating
  at 5 MiB to one `.1` backup (about 10 MiB retained in total).
- `state/search-fusion/shadow-daily.json` and lock files: persisted UTC daily cap.

To opt in, set `SEARCH_FUSION_SHADOW=1` or add:

```yaml
plugins:
  entries:
    search-fusion:
      fusion:
        shadow_enabled: true
        shadow_daily_cap: 300
        shadow_timeout_s: 3.0
```

`SEARCH_FUSION_SHADOW=0` disables diagnostics, including queued work. Shadow logs
contain timestamps, session/turn IDs, batch index, query text, URLs, titles,
snippets and provider status/latency. Known credentials/labeled secrets are
redacted, and URL userinfo/query/fragment are dropped; **ordinary queries and
result text are not anonymized**. Treat these files as sensitive. Disabling
shadow does not delete previous logs. Provider diagnostics go to the host's
configured logger; extraction messages may include the requested URL. Search
requests necessarily send query text to the selected providers.

See [TURN_ROUTING.md](TURN_ROUTING.md) for routing and shadow bounds and
[DEEPSEEK_SEARCH.md](DEEPSEEK_SEARCH.md) for the DeepSeek adapter contract.

## Tests

From the plugin directory with the Hermes environment active:

```sh
python -m pytest -q -p no:cacheprovider tests
# Bare pytest also discovers only tests/.
pytest
```

The supported suite uses offline transport doubles; no provider credentials or
live requests are required. See [tests/README.md](tests/README.md).

## License

MIT
