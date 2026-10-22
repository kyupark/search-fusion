"""State follows the selected Hermes home even after modules are imported."""
import importlib
import json
import os
import sys

import hermes_constants

REAL_HOME = hermes_constants.get_hermes_home
REAL_OVERRIDE = hermes_constants.get_hermes_home_override


def use_real_home(monkeypatch):
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', REAL_HOME)
    monkeypatch.setattr(hermes_constants, 'get_hermes_home_override', REAL_OVERRIDE)


def test_hermes_home_only_override_at_use_time(plugin, monkeypatch, tmp_path):
    use_real_home(monkeypatch)
    unchanged_home = os.environ['HOME']
    f = plugin.fusion
    monkeypatch.setattr(f, '_BUDGET_STATE_PATH', None)
    monkeypatch.setattr(f, '_MONTHLY_BUDGET', {'tavily': 2})
    s = importlib.import_module(f.__package__ + '.shadow')
    runner = s.ShadowRunner(autostart=False)
    first, second = tmp_path / 'profile-one', tmp_path / 'profile-two'
    try:
        for selected in (first, second):
            monkeypatch.setenv('HERMES_HOME', str(selected))
            assert f._budget_allows('tavily')
            assert runner._daily_claim(1)
            runner._append({'query': 'synthetic'})
            assert (selected / 'state/search-fusion/shadow.jsonl').exists()
            assert json.loads((selected / 'state/fusion-provider-budget.json').read_text())['counts'] == {'tavily': 1}
        monkeypatch.setenv('HERMES_HOME', str(first))
        assert f._budget_allows('tavily')
        assert not f._budget_allows('tavily')
        assert not runner._daily_claim(1)
        assert os.environ['HOME'] == unchanged_home
        assert not (tmp_path / '.hermes/state').exists()
    finally:
        runner.close()


def test_home_fallback_without_host(plugin, monkeypatch, tmp_path):
    paths = importlib.import_module(plugin.fusion.__package__ + '.paths')
    monkeypatch.setitem(sys.modules, 'hermes_constants', None)
    assert paths.hermes_home() == tmp_path / '.hermes'


def test_profile_context_survives_provider_and_shadow_workers(plugin, monkeypatch, tmp_path):
    use_real_home(monkeypatch)
    f = plugin.fusion
    monkeypatch.setattr(f, '_BUDGET_STATE_PATH', None)
    monkeypatch.setenv('SEARCH_FUSION_SHADOW', '1')
    profile = tmp_path / 'context-profile'
    settings = f.FusionSettings(shadow_enabled=True, turn_routing_enabled=False)
    p = f.SearchFusionWebSearchProvider(settings=settings, searchers={
        'serper': lambda *a: [{'url': 'https://result.example'}],
        'deepseek': lambda *a: [{'url': 'https://aux.example'}]})
    s = importlib.import_module(f.__package__ + '.shadow')
    runner = s.ShadowRunner()
    token = hermes_constants.set_hermes_home_override(profile)
    try:
        assert p.search('synthetic')['success']
        assert runner.submit(p, 'synthetic', 5, settings, {'data': {'web': []}}, {})
    finally:
        hermes_constants.reset_hermes_home_override(token)
    try:
        runner.queue.join()
        assert (profile / 'state/search-fusion/shadow.jsonl').exists()
        counts = json.loads((profile / 'state/fusion-provider-budget.json').read_text())['counts']
        assert counts == {'serper': 1, 'deepseek': 2}
        assert not (tmp_path / 'state/search-fusion/shadow.jsonl').exists()
    finally:
        runner.close()


def test_shadow_requires_opt_in(plugin, monkeypatch):
    f = plugin.fusion
    s = importlib.import_module(f.__package__ + '.shadow')
    monkeypatch.delenv('SEARCH_FUSION_SHADOW')
    assert not f.FusionSettings().shadow_enabled
    assert not f.load_fusion_settings().shadow_enabled
    assert not s.enabled(f.load_fusion_settings())
    monkeypatch.setenv('SEARCH_FUSION_SHADOW', '1')
    assert s.enabled(f.load_fusion_settings())
    monkeypatch.setenv('SEARCH_FUSION_SHADOW', '0')
    assert not s.enabled(f.FusionSettings(shadow_enabled=True))
