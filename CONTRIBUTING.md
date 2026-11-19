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

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/): the changelog is generated
from them with [git-cliff](https://git-cliff.org) (`cliff.toml`).

| Prefix | Changelog section |
|---|---|
| `feat:` | Added |
| `fix:` | Fixed |
| `perf:` | Performance |
| `refactor:` | Changed |
| `docs:` | Documentation |
| `chore:`, `ci:`, `test:`, `style:`, `build:` | not listed |

A `!` after the type (`feat!:`) or a `BREAKING CHANGE:` footer marks a breaking change. Only the
first line of the message ends up in the changelog.

## Releasing

Install git-cliff once: `pipx install git-cliff` (or `cargo install git-cliff`).

1. Set `__version__` in `src/labgrid_tui/__init__.py` (e.g. `0.2.0`) and commit it.
2. Optional: write highlights for the release to `.github/release-notes/v0.2.0.md`. They appear
   above the generated list in the changelog and the GitHub release.
3. Generate the changelog and commit it:

   ```bash
   git cliff --tag v0.2.0 --with-tag-message "$(cat .github/release-notes/v0.2.0.md)" -o CHANGELOG.md
   # without highlights: git cliff --tag v0.2.0 -o CHANGELOG.md
   git commit -am "chore(release): 0.2.0"
   git push
   ```

4. When CI is green, create an **annotated** tag with the same highlights and push it:

   ```bash
   git tag -a v0.2.0 -F .github/release-notes/v0.2.0.md   # or: git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```

The highlights are stored in the tag, so git-cliff keeps them whenever the changelog is
regenerated. The release workflow checks that the tag matches `__version__` and that
`CHANGELOG.md` has a section for it, builds the package, and creates the GitHub release with
the git-cliff notes and the wheel and sdist attached.

Preview what the next release would contain: `git cliff --unreleased`.
