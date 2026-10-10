# Licensing Hypothesise — step by step

Owner: **Siddhant Shah** (ADR-039). Current state: `license = { text = "UNLICENSED" }`
in `pyproject.toml`, no `LICENSE` file. **Set this before the first PyPI upload** —
a published version can never be changed.

> Not legal advice. For a commercial product, a short review by a lawyer is worth it.

---

## Step 1 — every option: contributor sign-off (do first)

Each person who wrote code owns it until they say otherwise. Collect a short email
from **every contributor** (Pratham, Sairaj, anyone else) to Siddhant:

```
I, <name>, assign all copyright in my contributions to the Hypothesise
codebase (formerly AI_Layer / Weaver / Guardent) to Siddhant Shah, and agree
that he may license it under any terms he chooses.
<name>, <date>
```

Save the replies. Without them the whole codebase cannot be licensed.

## Step 2 — pick one and follow its block

### A. Apache-2.0 (recommended — open core)

1. Copy the full text from https://www.apache.org/licenses/LICENSE-2.0.txt into `LICENSE`.
2. Create `NOTICE`:
   ```
   Hypothesise
   Copyright 2026 Siddhant Shah
   ```
3. `pyproject.toml`:
   ```toml
   license = "Apache-2.0"
   license-files = ["LICENSE", "NOTICE"]
   ```
   classifier: `"License :: OSI Approved :: Apache Software License"`
4. README footer: `Licensed under the Apache License 2.0.`

### B. MIT (simplest open)

1. `LICENSE` = MIT text from https://opensource.org/license/mit with
   `Copyright (c) 2026 Siddhant Shah`.
2. `pyproject.toml`: `license = "MIT"`, `license-files = ["LICENSE"]`
   classifier: `"License :: OSI Approved :: MIT License"`
3. README footer: `Licensed under the MIT License.`

### C. Elastic License 2.0 (source-available — blocks resale as a hosted service)

1. `LICENSE` = text from https://www.elastic.co/licensing/elastic-license, with
   `Licensor: Siddhant Shah` and `Software: Hypothesise`.
2. `pyproject.toml`: `license = "Elastic-2.0"`, `license-files = ["LICENSE"]`
   (no "OSI Approved" classifier — it is not open source).
3. README: state plainly *"source-available under the Elastic License 2.0 — free
   to use and modify; you may not offer it as a hosted/managed service."*

### D. Proprietary (all rights reserved)

1. `LICENSE`:
   ```
   Copyright (c) 2026 Siddhant Shah. All rights reserved.
   Permission is granted to install and use this software. Copying,
   modifying, redistributing or reselling it requires written permission.
   ```
   (Have a lawyer turn this into a proper EULA before selling.)
2. `pyproject.toml`: `license = "LicenseRef-Proprietary"`, `license-files = ["LICENSE"]`
   classifier: `"License :: Other/Proprietary License"`
3. Know the trade-off: PyPI is public and Python packages ship readable source.
   If the code must stay private, use a private package index instead of PyPI.

## Step 3 — every option: apply and verify

1. Remove the old line `license = { text = "UNLICENSED" }`.
2. In `[build-system]`, require a hatchling that understands the new license
   fields: `requires = ["hatchling>=1.27"]`.
3. Build and check:
   ```bash
   uv build && uvx twine check dist/*
   unzip -l dist/*.whl | grep -i -E "LICENSE|NOTICE"     # files are inside
   unzip -p dist/*.whl '*/METADATA' | grep -i license    # metadata is right
   ```
4. Commit `LICENSE` (+ `NOTICE`), `pyproject.toml`, README; push to
   `siddhant-shahhh/Hypothesise`.
5. After the TestPyPI upload (`DEPLOY-PYPI.md` step 5), confirm the project page
   shows the right license.

## Dependencies — already compatible

SQLAlchemy (MIT), FastAPI (MIT), Uvicorn (BSD-3), PyYAML (MIT), Cytoscape.js in the
dashboard (MIT). All permit A–D; keep their notices if you ever bundle them.

## Checklist

- [ ] Sign-off email from every contributor
- [ ] License chosen by Siddhant
- [ ] `LICENSE` (+ `NOTICE` for Apache) added
- [ ] `pyproject.toml` license, license-files, classifier, `hatchling>=1.27`
- [ ] README states the license
- [ ] Wheel contains the license; metadata correct; TestPyPI page correct
