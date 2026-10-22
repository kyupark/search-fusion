"""Isolated plugin imports; no production configuration or transports."""
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
# pytest imports the root __init__ during setup. Import Hermes tools before
# restoring cwd so its package wins over the plugin's tools.py.
sys.dont_write_bytecode = True
_HOME = tempfile.TemporaryDirectory(prefix='fusion-tests-')
_SAVED_ENV = {key: os.environ.get(key) for key in ('HOME', 'HERMES_HOME')}
_SAVED_MODULES = {key: sys.modules.get(key) for key in ('hermes_cli.config', '__init__')}
os.environ['HOME'] = _HOME.name
os.environ['HERMES_HOME'] = _HOME.name
_old_path = sys.path[:]
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != ROOT]
import tools.registry
sys.path[:] = _old_path
# Config is deliberately a minimal in-memory boundary, never real user config.
_config = types.ModuleType('hermes_cli.config')
_config.load_config = lambda: {}
_config.get_hermes_home = lambda: Path(os.environ['HERMES_HOME'])
sys.modules['hermes_cli.config'] = _config

# Supply pytest's root-package setup import with a properly loaded package.
_spec = importlib.util.spec_from_file_location('_fusion_collection', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
_collection = importlib.util.module_from_spec(_spec)
sys.modules['_fusion_collection'] = _collection
_spec.loader.exec_module(_collection)
sys.modules['__init__'] = _collection

@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv('SEARCH_FUSION_SHADOW', '0')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(hermes_constants, 'get_hermes_home_override', lambda: str(tmp_path))
    import httpx
    import socket
    import subprocess
    import urllib.request
    # Synthetic DNS is explicit and deterministic; the socket connect guard below remains.
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port=None, *a, **kw:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port or 443))])
    hits = []
    def forbidden(*args, **kwargs):
        # pytest's Failed is a BaseException, so plugin `except Exception`
        # handlers cannot turn an unmocked call into an ordinary error result.
        hits.append(args[:1])
        pytest.fail('Unmocked network or subprocess')
    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(_config, 'load_config', lambda: {})
    name = '_isolated_search_fusion'
    for key in list(sys.modules):
        if key == name or key.startswith(name + '.'):
            monkeypatch.delitem(sys.modules, key)
    spec = importlib.util.spec_from_file_location(name, ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    f = module.fusion
    monkeypatch.setattr(f, '_BUDGET_STATE_PATH', tmp_path / 'budget.json')
    monkeypatch.setattr(f, '_BUDGET_CACHE', {})
    monkeypatch.setattr(f, 'get_provider_env', lambda name: None)
    yield module
    f.SHADOW.close()
    f._SHARED_EXECUTOR.shutdown(wait=True, cancel_futures=True)
    assert not hits, f'unmocked network or subprocess calls: {hits}'

def pytest_sessionfinish(session, exitstatus):
    _HOME.cleanup()

def pytest_unconfigure(config):
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    for key, value in _SAVED_MODULES.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value
