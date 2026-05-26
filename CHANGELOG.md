# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-26
### Changed
- Refactored to the new Lyndrix Core plugin standard (`./app/` sub-package layout).
- `entrypoint.py` is now a pure wiring layer (manifest + lifecycle hooks only).
- All business logic consolidated behind `DiscordNotifierService`.
- Manifest `repo_url` corrected to the canonical `lyndrix-platform` repository.
- Avatar URL in webhook embeds updated to canonical `lyndrix-platform/lyndrix-core` raw URL.
- Replaced deprecated `datetime.utcnow()` with timezone-aware `datetime.now(timezone.utc)`.

### Added
- `app/controller/service.py`, `app/controller/webhook.py`, `app/ui/settings.py`.
- `requirements.txt` declaring `requests`.
- `requirements-dev.txt` with the standard toolchain.
- `tests/` scaffold with a smoke test for `DiscordNotifierService`.
- `CHANGELOG.md`.

### Fixed
- `repo_url` previously pointed to a personal fork.
- Plugin no longer relies on `requests` being globally available — declared as a dependency.

## [0.0.5] - earlier
- Last release on the legacy flat layout.
