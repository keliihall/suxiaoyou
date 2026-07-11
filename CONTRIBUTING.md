# Contributing to 苏小有

Thank you for helping improve 苏小有.

## Before opening a change

- Search existing issues and discussions first.
- Keep each pull request focused on one problem.
- Do not submit secrets, personal data, generated build output, or material
  whose license does not allow redistribution.
- Preserve upstream attribution and add third-party notices when introducing
  copied or adapted code, assets, skills, fonts, models, or runtimes.

## Local checks

Install the locked JavaScript dependencies with `npm ci` in the repository
root, `frontend/`, and `desktop-tauri/`. Use Python 3.12 with the hash-locked
`backend/requirements.txt`.

Before submitting, run the checks relevant to your change:

```bash
node --test scripts/*.test.mjs
npm --prefix frontend run lint
npm --prefix frontend exec tsc -- --noEmit
npm --prefix frontend exec -- node --test tests/unit/*.test.ts
cd backend && pytest -q
cd ../desktop-tauri/src-tauri && cargo test --locked
```

Packaging changes must also pass the bundle verifiers documented in the
release workflow. A local unsigned installer is test-only and must never be
presented as signed or notarized.

## Pull requests

Describe the user-visible outcome, the cause of any bug, tests performed, and
known limitations. Screenshots are welcome for interface changes. By
submitting a contribution, you agree that it may be distributed under the
repository's Apache License 2.0 and that you have the right to submit it.

All participation is subject to [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
