# Release runbook

This file is the maintainer's checklist for cutting a new `epublate`
release. The repo's CI matrix (`.github/workflows/ci.yml`) and the
tag-triggered publish workflow (`.github/workflows/release.yml`) do
the heavy lifting; this runbook documents the local steps and the
manual gates around them.

> **Permissions reminder.** Releases require the `PYPI_API_TOKEN`
> repository secret (or a configured PyPI Trusted Publisher) and
> `id-token: write` on the workflow. Without one of those, the
> publish step is a guarded no-op and the artifacts only attach to
> the GitHub Release.

## 0. Prerequisites

- `uv` is on your `PATH` (`uv --version` works).
- You're on `main` with a clean working tree
  (`git status` is empty).
- The remote points at the canonical fork
  (`git remote -v`).

## 1. Local sanity loop

```bash
uv sync --all-extras --dev

uv run pytest                       # full suite + snapshots
uv run ruff check .                 # lint
uv run ruff format --check .        # formatting
uv run mypy src/epublate            # types
```

If any step is red, fix it and start again.

## 2. Update the changelog

- Open `CHANGELOG.md` and replace the `[Unreleased]` heading with the
  new version + date.
- Make sure every PR merged since the previous tag has a bullet
  under the matching section (`Added` / `Changed` / `Fixed` /
  `Removed` / `Deprecated`).
- Add a new `[Unreleased]` section at the top to keep a landing
  zone for the next development cycle.

## 3. Bump the version

The single source of truth is `[project].version` in
`pyproject.toml`. Bump it in line with [SemVer](https://semver.org/);
a typical patch release looks like `0.1.0` → `0.1.1`.

```bash
uv version --bump patch     # or `--bump minor` / `--bump major`
```

Verify:

```bash
grep -E '^version =' pyproject.toml
uv run python -c "import epublate; print(epublate.__version__)"
```

## 4. Build artifacts locally

```bash
rm -rf dist/
uv build
ls -la dist/
```

You should see one wheel (`epublate-<v>-py3-none-any.whl`) and one
sdist (`epublate-<v>.tar.gz`). Optional sanity check:

```bash
uv tool install --reinstall --from dist/epublate-*.whl epublate
epublate --version
uv tool uninstall epublate
```

## 5. Commit, tag, push

```bash
git add CHANGELOG.md pyproject.toml
git commit -m "Release 0.1.0"
git tag -a v0.1.0 -m "v0.1.0"
git push origin main
git push origin v0.1.0
```

The `v` prefix is required — `release.yml` matches `v*`.

## 6. Watch the publish workflow

- `release.yml` reruns the lint/test matrix on the tagged ref.
- It then runs `uv build` and uploads `dist/*` as artifacts on the
  GitHub Release.
- If `PYPI_API_TOKEN` is configured (or Trusted Publishers is wired
  via `id-token: write`), it also runs `uv publish`.

If the publish step is skipped (no secret), download the artifacts
from the GitHub Release and run `uv publish --token $TOKEN
dist/*` from a trusted machine.

## 7. Verify the release

After `uv publish` completes:

```bash
uvx --refresh epublate@<v> --version
uv tool install epublate==<v>
epublate --version
```

Both should report the new version.

## 8. Announce

- Update the README's status banner if the milestone target changed.
- Post the release notes (the `CHANGELOG.md` section you just
  finalized) wherever you announce releases.

## Hotfix path

For urgent fixes off `main`:

1. Branch from the tag: `git checkout -b hotfix/0.1.1 v0.1.0`.
2. Cherry-pick the fix, run §1, §2, §3 on the patch version.
3. Tag `v0.1.1` from the branch and push the tag.
4. Merge the branch back into `main`.

## Rollback

`uv publish` is one-way (PyPI yanks, but doesn't allow deletion of a
published version). If a release is broken, ship a `0.1.x+1` patch.
Yank from PyPI only if the broken release would actively mislead
users.
