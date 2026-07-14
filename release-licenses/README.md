# Release license resource bundle

Every Tauri platform configuration copies this directory into
`licenses/third-party/` in the installed application. The repository-level
`LICENSE`, `NOTICE`, and `THIRD_PARTY_NOTICES.md` files are copied beside it in
`licenses/`.

This directory is the checked-in, mandatory baseline for material upstream
code, bundled language runtimes, PyInstaller output, MPL-covered components,
and the certificate data used by `webpki-roots`. It is deliberately curated;
the baseline itself is not an automatically generated or exhaustive inventory
of every transitive package. The generated language reports supplement it for
the locked production graphs. The lock files remain the exact version
inventory, and license files already present inside runtime payloads must not
be stripped.

Included files:

- `ANTHROPIC-SKILLS-APACHE-2.0.txt` — Apache-2.0 terms and Anthropic's
  explicit 2026 copyright notice for the eight permitted Agent Skills.
- `ANTHROPIC-KNOWLEDGE-WORK-PLUGINS-APACHE-2.0.txt` — upstream Apache-2.0
  terms for the 15 bundled Knowledge Work Plugins.
- `ANTHROPIC-CANVAS-FONTS-OFL-1.1.txt` — 27 font-family notices, Reserved
  Font Names, and the complete SIL Open Font License 1.1.
- `SUXIAOYOU-CJK-FONT-OFL-1.1.txt` — the complete Adobe/Noto Sans CJK
  copyright notice, Modified Version provenance and SHA-256, and SIL Open
  Font License 1.1 for the portable PDF CJK font.
- `SHADCN-UI-MIT.txt` — shadcn/ui MIT terms for the component source archive
  bundled with `web-artifacts-builder`.
- `NANOBOT-MIT.txt` — nanobot MIT terms and copyright.
- `OPENCLAW-MIT.txt` — OpenClaw MIT terms and copyright.
- `TENCENT-WEIXIN-OPENCLAW-1.0.3-MIT.txt` — Tencent's MIT terms and copyright
  for the personal WeChat channel adaptation.
- `NODEJS-22.22.0-LICENSE.txt` — the complete Node.js 22.22.0 distribution
  license and incorporated-library notices.
- `CPYTHON-3.12.13-LICENSE.txt` — the CPython 3.12.13 distribution license.
  Windows release installers bundle CPython 3.12.10; macOS and Linux release
  installers bundle CPython 3.12.13. `SOURCE_AVAILABILITY.md` records the
  exact source tag for each platform runtime.
- `python-runtime/python-build-standalone-20260623/` — exact macOS arm64 and
  x86_64 runtime provenance, checksums, architecture-specific `PYTHON.json`
  metadata, all 19 upstream runtime/incorporated-library license files, and
  the separately identified MPL-2.0 build-system license.
- `PYINSTALLER-6.21.0-COPYING.txt` — PyInstaller terms, GPLv2 text,
  Bootloader Exception, and Apache-2.0 terms for run-time hooks.
- `MOZILLA-PUBLIC-LICENSE-2.0.txt` — full MPL-2.0 text.
- `TQDM-4.68.4-LICENSE.txt` — tqdm's MPL/MIT file-level notice and MIT text.
- `WEBENCODINGS-0.5.1-BSD-3-CLAUSE.txt` — complete BSD-3-Clause terms and
  copyright notice for the PDF renderer's webencodings dependency.
- `CDLA-PERMISSIVE-2.0.txt` — full CDLA-Permissive-2.0 text for root data.
- `SOURCE_AVAILABILITY.md` — exact covered versions and upstream source links.
- `JAVASCRIPT-LICENSES.txt` — generated frontend production package notices.
- `PYTHON-LICENSES.txt` — generated hash-locked Python package notices.
- `RUST-LICENSES.html` — generated Cargo dependency licenses and provenance.
- `COLORAMA-0.4.6-LICENSE.txt` — Windows-only colorama wheel license.
- `PYWIN32-312-LICENSES.txt` — all unique license files supplied by the
  Windows pywin32 wheel.

The versioned texts were taken from the corresponding upstream release files.
When a pinned runtime or covered component changes, update both the applicable
text and `SOURCE_AVAILABILITY.md` before producing another installer.

Regenerate the language reports after installing the locked dependencies:

On macOS, install the pure-Python, platform-conditional wheels into the
temporary notice-generation environment as well. This keeps the checked-in
report complete for Linux Secret Service and Windows keyring helpers; pywin32
remains covered by its separate wheel notice file.

```bash
node scripts/generate-javascript-licenses.mjs
node scripts/generate-anthropic-font-licenses.mjs
python -m pip install --no-deps \
  colorama==0.4.6 jeepney==0.9.0 pywin32-ctypes==0.2.3 secretstorage==3.5.0
python backend/scripts/generate_python_licenses.py
cd desktop-tauri/src-tauri
cargo about generate --locked --fail about.hbs \
  --output-file ../../release-licenses/RUST-LICENSES.html
```
