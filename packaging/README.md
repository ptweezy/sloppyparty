# sloppyparty CI/CD

Full build-and-release automation, modeled on the yacron2 pipeline but adapted to
copyparty's build system (which hardcodes its version in `copyparty/__version__.py`
and ships the SFX + feature-variant Docker images rather than setuptools_scm +
PyInstaller-only).

## Workflows

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `ci.yml` | every push / PR | Runs copyparty's unittest suite (Linux gates; macOS/Windows informational) + coverage → Codecov. |
| `build.yml` | every push | Builds the wheel + sdist + SFX + PyInstaller binaries (all arches) **without** publishing — a fast "does it still build" gate. |
| `release.yml` | **every push to the release branch** | Auto-bumps the patch version and cuts a full release (see below). |
| `docker.yml` | called by release / dispatch / push gate | Builds copyparty's 5 image variants (min/ac/iv/dj/im) → GHCR. |

## Release model — every commit ships

Per project preference, **every push to the release branch cuts a new release**,
auto-bumping the **patch** version. No commit marker is required.

- **Skip one commit:** put `[skip release]` (or `[no release]` / `[skip ci]`) in the
  commit message.
- **Minor/major bump or an exact version:** run `release.yml` via *workflow_dispatch*
  and pick `bump: minor|major`, or set an exact `tag: X.Y.Z`.
- **Versioning:** independent of upstream copyparty. The first release seeds at
  `1.0.0`, then patch-climbs (`1.0.1`, `1.0.2`, …). CI patches `__version__.py`
  transiently at build time and **never commits it back** (so there is no release
  loop; a re-run retries cleanly).
- **Tags:** `sloppyparty-vX.Y.Z` — deliberately prefixed so they never collide with
  the 380+ upstream `vX.Y.Z` tags the fork inherits on every merge.
- **Release notes:** GitHub's auto-generated "What's Changed" (commits/PRs since the
  previous tag). See suggestions in the repo discussion; swap in a changelog file or
  commit-message body later if you want richer notes.

### Release branch

`release.yml` triggers on pushes to **`main`**. `hovudstraum` must stay a pristine
upstream mirror, so it can't be the release branch. If you develop on `develop`,
change the `branches:` list at the top of `release.yml`.

## What a release produces

- **GitHub Release** `sloppyparty-vX.Y.Z` with: wheel, sdist, `sloppyparty-sfx.py`,
  native binaries for Linux/macOS/Windows, and `SHA256SUMS`.
- **GHCR images** `ghcr.io/ptweezy/sloppyparty` — `ac` owns `latest`/`X.Y.Z`; the
  others are `latest-min`, `latest-iv`, `latest-dj`, `latest-im` (+ versioned).
- **Homebrew** `ptweezy/homebrew-tap` formula (if the token is set).
- **PyPI** — off by default (see below).

## Secrets & variables to configure

All are **optional** — each dependent feature skips gracefully if unset.

| Name | Kind | Enables |
|------|------|---------|
| `CODECOV_TOKEN` | secret | Coverage upload (private-repo Codecov). |
| `HOMEBREW_TAP_TOKEN` | secret | Pushing the formula to `ptweezy/homebrew-tap`. Fine-grained PAT, Contents: read/write on that repo. |
| `MACOS_CERT_P12_BASE64`, `MACOS_CERT_PASSWORD`, `MACOS_SIGN_IDENTITY`, `MACOS_NOTARY_KEY_BASE64`, `MACOS_NOTARY_KEY_ID`, `MACOS_NOTARY_ISSUER_ID` | secrets | Developer ID signing + notarization of the macOS binaries. |
| `FULL_BINARY_MATRIX` | **variable** = `true` | Build the exotic Linux arches (i686/armv7/ppc64le/s390x/riscv64 + full musl set, via QEMU) on **every** release. Off by default so per-commit releases stay fast; the core amd64/arm64 Linux/macOS/Windows binaries always build. `workflow_dispatch` always builds the full matrix. |
| `PUBLISH_PYPI` | **variable** = `true` | Publish wheel+sdist to PyPI on release. Also requires claiming the `sloppyparty` name on PyPI and configuring a Trusted Publisher (OIDC) for `ptweezy/sloppyparty`. |

Set variables under *Settings → Secrets and variables → Actions → Variables*.

## First release

1. Push these workflows to your release branch (`main`).
2. Either push a normal commit (auto-releases `1.0.0`) or run `release.yml` via
   *workflow_dispatch* with an exact `tag` if you want a specific starting version.
3. Watch the run: it gates on tests, builds everything, then tags + releases.

## Local helpers (also used by CI)

- `packaging/pyinstaller/sloppyparty.spec` — the portable-binary spec.
- `packaging/ci/set-version.sh X.Y.Z` — patch `__version__.py` (no commit).
- `packaging/ci/fetch-webdeps.sh` — populate `copyparty/web/deps` from a reference SFX.
- `packaging/ci/run-tests.sh` — the CI test runner (deselects 3 tests that fail on
  pristine upstream copyparty too; `SLOPPY_ALL_TESTS=1` to include them).
- `packaging/homebrew/render-formula.sh` — render the tap formula from `SHA256SUMS`.
