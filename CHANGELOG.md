# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-19

First public version.

### Added

- Places table with status (mine / free / reserved / taken), tags, exporter, owner, time held
  and comment; places held longer than `stale_hours` are flagged.
- Filter by tag (`board=imx8*`), name/alias, exporter or free text; saved filters on F1–F9.
- Acquire, release, reserve (with automatic wait and acquire) and cancel reservations;
  serial console for acquired places.
- Exporters tab: status, hostname, resources, places per exporter; SSH into the exporter host.
- Places without match patterns (e.g. places that stand for exporters) are hidden by default.
- Live updates via `labgrid-client monitor`, with polling as fallback.
- Robust connection handling: retries with backoff, state-changing calls are verified instead of
  repeated, reservations survive network blips and coordinator restarts.
- Debug console listing every `labgrid-client` call, optional log file.
- Adjustable columns: max widths, wrapping, tag columns.
- TOML config with profiles (coordinator, proxy, identity, ssh settings).

[Unreleased]: https://github.com/C0DE-X/labgrid-tui/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/C0DE-X/labgrid-tui/releases/tag/v0.1.0
