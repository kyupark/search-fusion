# Search Fusion behavior tests

Run from the plugin directory:

```sh
python -m pytest -q -p no:cacheprovider tests
```

Only the `tests` path is supported. The suite is independent of the legacy
root suites (`test_fusion.py`, `test_frugal_extract.py`) and does not collect
or exclude them; do not run `pytest .` from the plugin root.

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

Mutation evidence is in `../evidence/`: `mutants.json` lists the 120 exact
single-span mutants; `mutation-before.json` (initial suite, i.e. without
`test_*_extra.py`) and `mutation-after.json` (full suite) record each outcome.
Source files were restored after every mutant and checked against
`../SOURCE_SHA256.txt`; the temporary runner has been removed.
