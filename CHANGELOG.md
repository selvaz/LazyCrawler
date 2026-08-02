# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

## [0.18.0] — 2026-08-02

### Added
- `make_news_report.py` registers its per-region full reports and its
  digest into LazyTools' shared artifact catalog (`CRAWLER_ARTIFACTS_DB`,
  optional/best-effort) — searchable by `kind` (`report`/`digest`), a
  `daily` cadence tag, and `region:<name>` tags.
- `setup_first_run.ps1` prompts for `BRAVE_API_KEY`/`TAVILY_API_KEY` (the
  search tool's backends) and `CRAWLER_ARTIFACTS_DB` (this repo's artifact
  catalog), previously invisible to this installer.

### Fixed
- `run_news_crawl_with_telegram.ps1` (what the 3 scheduled tasks actually
  run) never reloaded `CRAWLER_ARTIFACTS_DB` from the persisted
  environment — on a Task Scheduler launch, `make_news_report.py`'s
  artifact registration would silently no-op with it looking unset.
- `setup_first_run.ps1` persisted a typed `CRAWLER_ARTIFACTS_DB` path
  verbatim; a relative path now resolves to an absolute one (matching
  `LAZYCRAWLER_NEWS_DB`'s existing handling), so it means the same file
  regardless of which repo's working directory reads it later.

## [0.17.0] — 2026-07-29

### Added
- `resolve_news_db_path()` in `lazycrawler.config`: the one canonical
  resolution chain (`explicit` -> `LAZYCRAWLER_NEWS_DB` env -> `None`) for
  the news cache db path, so callers (and `CrawlerTools`) don't each grow
  their own copy. `CrawlerTools` now warns instead of silently falling back
  to an always-empty `:memory:` db when nothing resolves.
- News-monitor pipeline: financial + geopolitical crawl, digest, Telegram
  delivery, with a `--max-age-hours` option to skip stale feed items.

### Fixed
- Certificate verification falls back to the certifi CA bundle when the
  Windows cert store is corrupt or the `ddgs` search path's own store is
  unavailable (#39, #40).

## [0.16.0] — 2026-07-12

### Added
- `CrawlerDB.get_artifacts` gains an optional `content_hash` filter to fetch
  the artifact(s) matching a specific content hash — the stable join key
  behind `[[artifact:<hash>]]` anchors and downstream `crawler:<hash>`
  artifact refs (LazyTools report figures). Additive parameter; existing
  call sites are unchanged. `content_hash` is unique only per page
  (`UNIQUE(url_hash, content_hash)`), so combine it with `url_hash`/
  `session_id` when you need exactly one row.

## [0.15.0] — 2026-07-12

**Breaking (security): private-network access is now blocked by default**
(ecosystem stabilization plan, Train B / ECO-008, PR B1). This is the
default flip promised in 0.14.1.

### Changed
- `HTTPConfig.block_private_addresses` default flips from `False` to
  `True`. A plain `HTTPConfig()` now refuses loopback / link-local /
  private (RFC-1918) / reserved / multicast / unspecified addresses, plus
  `localhost` / `*.local` / cloud metadata endpoints (e.g.
  `169.254.169.254`), the same set `CrawlerTools` already enforced.
- `HTTPConfig()` and `HTTPConfig(block_private_addresses=True)` (matching
  the new default) no longer warn — the deprecation warning now fires only
  on the path that still matters: explicitly requesting
  `block_private_addresses=False` (disable the guard) via the old field.
  Use `HTTPConfig(allow_private_networks=True)` instead.

### Migration
- **If you crawl localhost, an intranet, or other internal services**, opt
  in explicitly: `HTTPConfig(allow_private_networks=True)`.
- `CrawlerTools` (the agent wrapper) is unaffected — it already forced the
  guard on and sets it explicitly regardless of this default.
- No action needed if you already migrated to `allow_private_networks` in
  0.14.1, or if you never touched private/internal targets.

### Added
- Expanded SSRF test coverage (`tests/test_ssrf_bypass_vectors.py`):
  explicit IPv6 loopback, RFC-1918 `172.16.0.0/12` and `192.168.0.0/16`,
  IPv4-mapped IPv6 (`::ffff:127.0.0.1`), legacy numeric host forms (decimal
  and hex IPv4), URL credentials/userinfo host confusion, and a multi-hop
  redirect chain re-validated at every hop.

## [0.14.1] — 2026-07-12

Migration release ahead of the 0.15.0 SSRF default flip (ecosystem
stabilization plan, Train B / ECO-008). No behavior changes yet — this
release only adds the new API and starts warning.

### Added
- `HTTPConfig.allow_private_networks: bool | None` — new, inverse-polarity
  replacement for `block_private_addresses`. `True` = guard OFF (private/
  loopback/link-local/cloud-metadata targets reachable, today's default),
  `False` = guard ON. Takes precedence over `block_private_addresses` when
  both are set.
- Distribution: this is the first tagged LazyCrawler release. Published via
  GitHub Release (wheel + sdist + `SHA256SUMS.txt`) — LazyCrawler is not on
  PyPI and is not planned to be; only LazyBridge is distributed there.

### Deprecated
- `HTTPConfig.block_private_addresses` is deprecated. Constructing
  `HTTPConfig()` without specifying `allow_private_networks` now emits a
  `DeprecationWarning` regardless of which value `block_private_addresses`
  ends up with — because **LazyCrawler 0.15.0 will block private networks
  by default** (`allow_private_networks=False`). Pass
  `allow_private_networks` explicitly to silence the warning and pin your
  intended behavior across the 0.15.0 upgrade:
  - `HTTPConfig(allow_private_networks=True)` — keep today's behavior.
  - `HTTPConfig(allow_private_networks=False)` — opt into the 0.15.0
    default now.
- `CrawlerTools` (the agent-facing wrapper) is unaffected: it already forces
  the guard on by default and has been migrated internally to the new API,
  so it emits no deprecation warnings.

### Migration notes
- No action required before 0.15.0 ships, but every unmigrated
  `HTTPConfig()` / `WebCrawler()` call without an explicit
  `allow_private_networks` will emit a `DeprecationWarning` in the
  meantime — expected, and safe to ignore short-term (Python does not
  surface `DeprecationWarning` by default outside test runners).
