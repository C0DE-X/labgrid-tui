# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). It is generated from the commit
history with [git-cliff](https://git-cliff.org).

## [0.1.0] - 2026-11-19

First public version: a terminal UI for labgrid to see, filter, reserve, acquire and
release places, with an overview of the exporters.

- Places table with status (mine / free / reserved / taken), tags, exporter, owner, time
  held and comment; places held longer than `stale_hours` are flagged.
- Filter by tag (`board=imx8*`), name/alias, exporter or free text; saved filters on F1–F9.
- Acquire, release, reserve (waits and acquires automatically) and cancel reservations;
  serial console for acquired places.
- Exporters tab with status, hostname, resources and places per exporter; SSH into the
  exporter host.
- Places without match patterns (e.g. places that stand for exporters) are hidden by default.
- Live updates via `labgrid-client monitor`, with polling as fallback.
- Robust connection handling: retries with backoff, state-changing calls are verified instead
  of repeated, reservations survive network blips and coordinator restarts.
- Debug console listing every `labgrid-client` call, optional log file.
- Adjustable columns (max widths, wrapping, tag columns) and a TOML config with profiles.

### Fixed

- Make live updates arrive immediately ([71a0a66](https://github.com/C0DE-X/labgrid-tui/commit/71a0a66061fda5b26a2c0247fd9fea2c6da40e97))

[0.1.0]: https://github.com/C0DE-X/labgrid-tui/releases/tag/v0.1.0

