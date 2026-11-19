# Contributing

Issues and pull requests are welcome.

## Development setup

```bash
git clone https://github.com/C0DE-X/labgrid-tui.git
cd labgrid-tui
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,labgrid]"
pre-commit install   # optional: run ruff before every commit
```

## Checks

CI runs these on every push and pull request; run them locally before pushing:

```bash
ruff check .
ruff format --check .   # or `ruff format .` to fix
pytest
```

The integration tests (`tests/test_integration.py`) start real `labgrid-coordinator` processes
on free local ports and drive the UI headlessly. They are skipped if labgrid isn't installed,
so install the `labgrid` extra to run everything.

## Where things live

| File | What |
|---|---|
| `src/labgrid_tui/app.py` | Textual UI: tables, tabs, keys, workers |
| `src/labgrid_tui/backend.py` | `labgrid-client` wrapper: retries, verification, monitor, debug events |
| `src/labgrid_tui/parser.py` | Parsing `labgrid-client` text output (places, reservations, resources) |
| `src/labgrid_tui/filtering.py` | Filters, sorting, exporter resolution |
| `src/labgrid_tui/config.py` | TOML config and profiles |
| `src/labgrid_tui/ssh.py` | ssh command for exporter hosts |

labgrid-client has no machine-readable output, so `parser.py` depends on its text format. If a
labgrid release changes it, add the new output to the tests and adjust the parser.

## Releasing

1. Update `__version__` in `src/labgrid_tui/__init__.py`.
2. Move the "Unreleased" entries in `CHANGELOG.md` to a new version section.
3. Commit, then tag and push: `git tag v0.2.0 && git push origin main v0.2.0`.

The release workflow checks that the tag matches `__version__`, builds the package and creates a
GitHub release with the wheel and sdist attached.
