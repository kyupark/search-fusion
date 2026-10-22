"""Behaviour contract for FrugalExtractProvider: Firecrawl is the LAST rung, never the first.

Run: cd <hermes-agent checkout> && PYTHONPATH=. .venv/bin/python -m pytest -q \
    ~/.hermes/plugins/search-fusion/test_frugal_extract.py -p no:cacheprovider --noconftest
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent
PACKAGE = "kyu_search_fusion_frugal_testpkg"
if f"{PACKAGE}.frugal_extract" not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    for name in ("tools", "frugal_extract"):
        spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{PACKAGE}.{name}"] = module
        spec.loader.exec_module(module)

fe = sys.modules[f"{PACKAGE}.frugal_extract"]

CFG = {"firecrawl_hosts": {"x.com"}, "skip_firecrawl_hosts": {"never.example"}, "timeout_s": 5, "max_chars": 10000, "min_local_chars": 50}


def _provider():
    return fe.FrugalExtractProvider(settings_override=CFG)


def _run(provider, url):
    return asyncio.run(provider.extract([url], format="markdown"))[0]


class _FakeFirecrawl:
    def __init__(self):
        self.calls: list[str] = []

    def supports_extract(self):
        return True

    def is_available(self):
        return True

    async def extract(self, urls, **kwargs):
        self.calls.extend(urls)
        return [{"url": u, "title": "fc", "content": "firecrawl body " * 20, "metadata": {}} for u in urls]


@pytest.fixture
def firecrawl():
    fake = _FakeFirecrawl()
    with mock.patch.object(fe, "_firecrawl_provider", return_value=fake):
        yield fake


def test_non_html_direct_hit_never_reaches_firecrawl(firecrawl):
    with mock.patch.object(fe, "_direct_http", return_value={"ok": True, "method": "direct_http", "content": "{\"a\": 1}" * 20, "content_type": "application/json"}), \
         mock.patch.object(fe, "_run_defuddle", side_effect=AssertionError("defuddle must not run for JSON")):
        entry = _run(_provider(), "https://api.example.com/data")
    assert entry["metadata"]["extract_method"] == "direct_http"
    assert firecrawl.calls == []


def test_html_page_uses_defuddle_before_firecrawl(firecrawl):
    with mock.patch.object(fe, "_direct_http", return_value={"ok": True, "method": "direct_http", "content": "<html>x</html>", "content_type": "text/html"}), \
         mock.patch.object(fe, "_run_defuddle", return_value={"ok": True, "method": "defuddle", "content": "Real article body. " * 20, "title": "T"}):
        entry = _run(_provider(), "https://news.example.com/story")
    assert entry["metadata"]["extract_method"] == "defuddle"
    assert entry["title"] == "T"
    assert firecrawl.calls == []


def test_js_shell_escalates_to_firecrawl(firecrawl):
    shell = "Please enable JavaScript to continue. " * 5
    with mock.patch.object(fe, "_direct_http", return_value={"ok": True, "method": "direct_http", "content": shell, "content_type": "text/html"}), \
         mock.patch.object(fe, "_run_defuddle", return_value={"ok": True, "method": "defuddle", "content": shell}):
        entry = _run(_provider(), "https://spa.example.com/app")
    assert firecrawl.calls == ["https://spa.example.com/app"]
    assert entry["metadata"]["extract_method"] == "firecrawl"
    assert entry["metadata"]["attempts"] == ["direct_http", "defuddle", "firecrawl"]


def test_definitive_404_does_not_escalate(firecrawl):
    with mock.patch.object(fe, "_direct_http", return_value={"ok": False, "method": "direct_http", "error": "HTTP 404: Not Found"}), \
         mock.patch.object(fe, "_run_defuddle", side_effect=AssertionError("no defuddle on 404")):
        entry = _run(_provider(), "https://docs.example.com/missing")
    assert entry["error"].startswith("direct_http: HTTP 404")
    assert firecrawl.calls == []


def test_forced_host_skips_local_ladder(firecrawl):
    with mock.patch.object(fe, "_direct_http", side_effect=AssertionError("forced host must skip local")):
        entry = _run(_provider(), "https://x.com/parkonx/status/1")
    assert firecrawl.calls == ["https://x.com/parkonx/status/1"]
    assert entry["metadata"]["escalation_reason"] == "forced by firecrawl_hosts"


def test_skip_host_fails_instead_of_paying(firecrawl):
    with mock.patch.object(fe, "_direct_http", return_value={"ok": False, "method": "direct_http", "error": "HTTP 403: Forbidden"}), \
         mock.patch.object(fe, "_run_defuddle", return_value={"ok": False, "method": "defuddle", "error": "403"}):
        entry = _run(_provider(), "https://never.example/page")
    assert "Firecrawl is disabled for this host" in entry["error"]
    assert firecrawl.calls == []


def test_firecrawl_unavailable_returns_error_not_exception():
    with mock.patch.object(fe, "_firecrawl_provider", return_value=None), \
         mock.patch.object(fe, "_direct_http", return_value={"ok": False, "method": "direct_http", "error": "HTTP 403: Forbidden"}), \
         mock.patch.object(fe, "_run_defuddle", return_value={"ok": False, "method": "defuddle", "error": "403"}):
        entry = _run(_provider(), "https://walled.example/page")
    assert "Firecrawl is unavailable" in entry["error"]
