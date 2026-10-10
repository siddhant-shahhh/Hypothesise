# Publishing Hypothesise to PyPI — step by step

Goal: anyone can run `uv tool install hypothesise` (or `pipx install` / `pip install`)
on macOS, Windows or Linux. Publishing is **one-way** — a version number, once
uploaded, can never be reused or replaced. Do every step in order.

Repo: **`github.com/siddhant-shahhh/Hypothesise`** · Package: `hypothesise` · CLI: `hypothesise`
Owner of the code **and** of the PyPI project: **Siddhant** (ADR-039). Every
PyPI/TestPyPI step below is done **logged in as Siddhant**, and the trusted
publisher points at his repo.

---

## 0. Decide before anything is public

| Decision | Where | Current |
|---|---|---|
| Who owns the code (ADR-007) | — | **Siddhant** — decided, ADR-039 |
| PyPI account that owns `hypothesise` | pypi.org | **Siddhant's** account |
| License | `pyproject.toml` → `license` | `UNLICENSED` — set it now (e.g. `{ text = "Proprietary" }`, MIT, Apache-2.0) |
| Author + links | `pyproject.toml` | missing — add below |
| First version | `pyproject.toml` | `0.1.0` → publish as **`0.1.0a1`** (alpha) |

Add to the root `pyproject.toml` under `[project]`:

```toml
authors = [{ name = "Siddhant Shah", email = "<siddhant's email>" }]
```

(`[project.urls]` already points at `siddhant-shahhh/Hypothesise`.) Add other
maintainers' names there too if they should appear on PyPI.

## 1. Accounts (one time)

1. **Siddhant** creates accounts on **https://pypi.org** and **https://test.pypi.org**
   (they are separate) and turns on **2FA** on both — PyPI requires it.
2. Later, Siddhant can add others as *Maintainer* under the project's
   **Collaborators** page; only he stays *Owner*.
3. **Reserve the name now** so nobody takes it: done automatically by your first
   TestPyPI/PyPI upload (step 5/7). Until then `hypothesise` is only free, not yours.

## 2. Clean the repo before pushing to GitHub

```bash
cd /Volumes/Seagate/AI_Layer
git status
```

Must **not** be committed (already in `.gitignore` — confirm with `git check-ignore -v <file>`):
`hypothesise.config.json` (your local paths), `testbed/` (fake secrets), `.venv/`, `*.db`, `._*`.

Decide yourself: `Claude outputs/` (landing page — still says `hypothesize-agent`, fix
before publishing it) and `sagar_requirements.txt`.

Quick secret scan:
```bash
git grep -nE "(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}" -- . ':!tests' ':!redteam' ':!scenarios'
```
Anything real → remove it, rotate it.

## 3. Prove it works locally

```bash
source ~/sagar_env/bin/activate
bash scripts/dev-setup.sh
pytest -q
python scripts/replay.py --all --quiet
python redteam/run.py
python scripts/ops/drills.py
ruff check packages scripts tests redteam
mypy packages/core/hypothesise_core
```

All must pass. Then push to Siddhant's repo (your current `origin` is the old
`SairajNanda/AI-Layer`; it stays as a second remote):

```bash
git remote add hypothesise https://github.com/siddhant-shahhh/Hypothesise.git
git checkout -b release/0.1.0a1
git add -A
git commit -m "Rename to Hypothesise; init/uninstall/status; agent hooks; local-first spool"
git push -u hypothesise release/0.1.0a1
```

You need **write access** to the repo — Siddhant adds you under Settings →
Collaborators. If the repo is empty, push `main` first:
`git push hypothesise HEAD:main`. Open a PR there and merge to `main`. **Tell Claude at this point** — the next steps add
the release workflow and bump the version.

## 4. Set the version

The version string lives in several files. Bump them together:

```bash
OLD=0.1.0; NEW=0.1.0a1
grep -rl "$OLD" pyproject.toml packages --include=*.toml --include=*.py \
  | xargs sed -i '' "s/$OLD/$NEW/g"         # macOS sed; on Linux drop the ''
sed -i '' "s/hypothesise $OLD/hypothesise $NEW/" tests/test_cli.py
grep -rn "$NEW" pyproject.toml packages/cli/hypothesise/__init__.py
pytest -q tests/test_cli.py
```

## 5. Build and test-upload to TestPyPI (by hand, once)

```bash
rm -rf dist
uv build                         # -> dist/hypothesise-0.1.0a1-py3-none-any.whl + .tar.gz
uvx twine check dist/*           # metadata + README render must PASS
```

**Siddhant**, on TestPyPI → Account settings → **API tokens** → creates one
(scope: entire account for the first upload) and runs, or shares it with you
for this one upload and deletes it afterwards:

```bash
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-XXXXXXXX
```

Install it exactly as a stranger would, on **a Mac and a Windows machine**:

```bash
uv tool install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ hypothesise==0.1.0a1
hypothesise version
hypothesise init --dry-run
hypothesise api --demo           # http://127.0.0.1:8080 shows sample attacks
uv tool uninstall hypothesise
```

Windows (PowerShell): same commands; install uv first with
`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`.

Anything wrong → fix, bump to `0.1.0a2`, repeat step 5. TestPyPI versions burn too.

## 6. Set up automatic, token-free publishing (Trusted Publishing)

No API token ever lives in GitHub — PyPI trusts the workflow directly.

1. **Siddhant**, on **pypi.org** → Your account → **Publishing** → *Add a new pending publisher*:
   - PyPI project name: `hypothesise`
   - Owner: `siddhant-shahhh` · Repository: `Hypothesise`
   - Workflow name: `release.yml` · Environment: `pypi`
2. **GitHub** → `siddhant-shahhh/Hypothesise` → Settings → **Environments** → New → `pypi`.
   Add **Siddhant** (and you, if he wants) as **required reviewers** — every
   release then waits for an owner's click.
3. Add `.github/workflows/release.yml`:

```yaml
name: release
on:
  push:
    tags: ["v*"]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv build
      - run: uvx twine check dist/*
      - uses: actions/upload-artifact@v4
        with: { name: dist, path: dist/ }

  smoke:                      # the wheel must install and run on every OS
    needs: build
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.10", "3.13"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/download-artifact@v4
        with: { name: dist, path: dist/ }
      - uses: astral-sh/setup-uv@v6
        with: { python-version: "${{ matrix.python }}" }
      - shell: bash
        run: |
          uv tool install dist/*.whl
          hypothesise version
          hypothesise init --dry-run || true
          hypothesise migrate --db "sqlite:///$RUNNER_TEMP/x.db"

  publish:
    needs: smoke
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write         # trusted publishing
    steps:
      - uses: actions/download-artifact@v4
        with: { name: dist, path: dist/ }
      - uses: pypa/gh-action-pypi-publish@release/v1
```

Commit it to `main`.

## 7. Release

```bash
git checkout main && git pull hypothesise main
git tag v0.1.0a1
git push hypothesise v0.1.0a1
```

GitHub → **Actions** → `release` runs build → smoke (6 OS/Python combos) →
waits for your approval → publishes. Approve only if smoke is green.

## 8. Verify the real thing

On a clean machine:

```bash
uv tool install hypothesise        # or: pipx install hypothesise
hypothesise version                # hypothesise 0.1.0a1
hypothesise init --dry-run
```

Check **https://pypi.org/project/hypothesise/** — README renders, links work,
license is what you chose.

## 9. Every later release

1. Change code → all checks in step 3 pass → merge to `main`.
2. Bump version (step 4): `0.1.0a2`, … then `0.1.0` when stable, `0.1.1` for fixes.
3. `git tag vX.Y.Z && git push hypothesise vX.Y.Z` → Siddhant approves in Actions.

## If a release is broken

- **Never** delete and re-upload the same version — PyPI forbids it.
- pypi.org → project → **Manage** → release → **Yank** (hides it from new
  installs, keeps it for anyone pinned). Then fix and ship the next version.

## Checklist

- [x] Ownership decided: Siddhant (code + PyPI)
- [ ] Siddhant's PyPI + TestPyPI accounts with 2FA; you have write access to the repo
- [ ] License, authors, URLs in `pyproject.toml`
- [ ] Repo clean, no secrets, pushed to `siddhant-shahhh/Hypothesise`, merged to `main`
- [ ] Version bumped to `0.1.0a1`, tests pass
- [ ] TestPyPI upload installs and runs on Mac **and** Windows
- [ ] Trusted publisher + `pypi` environment + `release.yml`
- [ ] Tag pushed, smoke green, approved, live on pypi.org
