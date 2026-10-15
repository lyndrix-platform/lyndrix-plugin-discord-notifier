# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Security
- Interaction endpoint now fails closed (HTTP 503) when `discord_public_key` is
  not configured, instead of processing unsigned requests.
- Added replay protection: interactions with a stale signed timestamp are rejected.

### Changed
- Moved to the canonical plugin layout: `app/controller/` → `app/logic/`, router
  into `app/api/`, NiceGUI page into `app/ui/nicegui/`.
- Imports now go through the stable `core.api` surface (no `core.components.*` /
  `ui.theme` reach-ins).
- Interaction routing now uses the adapter's real `provider_id` and the endpoint
  is registered once instead of per instance.
- Vault reads in `health()`/`send()` and the interaction route are offloaded with
  `asyncio.to_thread` so they no longer block the event loop.
- NiceGUI settings page persists through `DiscordGatewayAdapter.save_config()`
  (webhook URLs are validated) instead of writing Vault directly.

### Fixed
- Settings page no longer exposes a non-functional "enabled" switch; bot name is
  now persisted and its default matches the adapter (`Lyndrix`).
- Rewrote the broken test suite against the current adapter/service API.

## [1.2.0] - 2026-10-15
### Changed
- Cluster readiness: the notification → Discord message id map moved from process
  memory into the shared DB table `discord_messages` (PK `notification_id` +
  `provider_id`, with `channel_id`, `discord_message_id`, `created_at`,
  `updated_at`). An update handled by another core instance — or after a restart —
  now edits the original message instead of posting a new one. A 60 s per-process
  cache sits in front; without a DB the plugin falls back to the old in-memory map.
- Table creation is idempotent and serialized by the core's `lyndrix_bootstrap`
  advisory lock; it runs at setup when the DB is already connected and again on
  every `db:connected`. Writes use READ COMMITTED sessions and retry MariaDB
  1020/1205/1213; a concurrent insert of the same key is tolerated.
- `get_config_fields()` (called synchronously by the core) serves Vault values
  cached by the off-thread `health()`/`send()` reads instead of hitting Vault on
  the event loop every time.

### Added
- Setting `message_retention_days` (default 30): mappings not updated for that long
  are pruned by the leader instance (at most hourly, piggy-backed on writes).
- `health()` reports `tracked_messages` (row count of `discord_messages`).

### Removed
- The per-process `notifications_sent` counter (never incremented); `health()` no
  longer reports it.

### Notes
- Every instance needs identical `LYNDRIX_GATEWAY_*` env vars; 429 back-off is
  per instance (not coordinated across the cluster).

## [0.1.0] - 2026-05-26
### Changed
- Refactored to the new Lyndrix Core plugin standard (`./app/` sub-package layout).
- `entrypoint.py` is now a pure wiring layer (manifest + lifecycle hooks only).
- All business logic consolidated behind `DiscordNotifierService`.
- Manifest `repo_url` corrected to the canonical `lyndrix-platform` repository.
- Avatar URL in webhook embeds updated to canonical `lyndrix-platform/lyndrix-core` raw URL.
- Replaced deprecated `datetime.utcnow()` with timezone-aware `datetime.now(timezone.utc)`.

### Added
- `app/controller/service.py`, `app/controller/adapter.py`, `app/ui/settings.py`.
- `requirements.txt` declaring `requests`.
- `requirements-dev.txt` with the standard toolchain.
- `tests/` scaffold with a smoke test for `DiscordNotifierService`.
- `CHANGELOG.md`.

### Fixed
- `repo_url` previously pointed to a personal fork.
- Plugin no longer relies on `requests` being globally available — declared as a dependency.

## [0.0.5] - earlier
- Last release on the legacy flat layout.
