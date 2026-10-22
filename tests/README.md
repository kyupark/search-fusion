# Search Fusion behavior tests

Run from the plugin directory:

```sh
python -m pytest -q -p no:cacheprovider tests
```

`pytest.ini` restricts default discovery to `tests/`, so bare `pytest` works.
Obsolete root-level suites were removed after failing when relocated; the
supported suite covers current providers and extraction behavior.

`conftest.py` imports the installed Hermes `tools` package with the plugin root
removed from `sys.path` (the plugin's own `tools.py` would otherwise shadow it),
then loads the plugin under unique package names. HOME and HERMES_HOME are
redirected to a system temp directory before Hermes/plugin imports.
Configuration is in-memory, provider budget state and Hermes home helpers use
each test's `tmp_path`, and real HTTP/socket/subprocess boundaries are blocked.
Tests replace transports, provider calls and dispatchers with local doubles. No
test inspects plugin source text.

Each test gets fresh plugin module state. Thread pools are joined at fixture
teardown. Real deadline cases use short delayed provider doubles; race-sensitive
collection cases use scripted futures and clocks.
