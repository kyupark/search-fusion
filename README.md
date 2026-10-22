# search-fusion

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that replaces the single-provider `web_search` with a fused, budget-aware search layer, plus a cheaper `web_fetch`.

- **Fusion search**: queries Exa, TinyFish, and paced Brave in parallel, merges and dedupes results, with Serper/Tavily/DeepSeek and Firecrawl as fallbacks.
- **Turn-aware routing**: the first search batch in a user turn gets the full roster; follow-up batches in the same turn go to one cheap provider. See `TURN_ROUTING.md`.
- **Frugal extract / `web_fetch`**: Defuddle first for known URLs, paid extractors only on failure.
- **Shadow mode**: compare routing policies on live traffic without changing results (`shadow.py`, `shadow_report.py`).

Status: personal plugin, published as-is. Built against Hermes Agent's plugin API (`plugin.yaml`, `pre_tool_call` hook).

## Install

```sh
git clone https://github.com/kyupark/search-fusion ~/.hermes/plugins/search-fusion
```

Set whichever provider keys you have in the Hermes env: `EXA_API_KEY`, `TINYFISH_API_KEY`, `BRAVE_SEARCH_API_KEY`, `SERPER_API_KEY`, `TAVILY_API_KEY`, `DEEPSEEK_API_KEY`. Providers without a key are skipped.

## Tests

From the plugin directory, with the Hermes Agent venv active:

```sh
python -m pytest -q -p no:cacheprovider tests
```

## License

MIT
