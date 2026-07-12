# Source availability and provenance

This document tells recipients where to obtain the Source Code Form for
MPL-2.0 components included in 苏小有 v0.8.1. The exact-version links below
provide the source without charge. 苏小有 does not intentionally modify the
listed upstream MPL-covered source files.

## MPL-2.0 components

Python:

- `certifi` 2025.8.3:
  <https://files.pythonhosted.org/packages/source/c/certifi/certifi-2025.8.3.tar.gz>
- `tqdm` 4.68.4:
  <https://github.com/tqdm/tqdm/tree/v4.68.4>

Rust:

- `cssparser` 0.29.6:
  <https://crates.io/api/v1/crates/cssparser/0.29.6/download>
- `cssparser` 0.36.0:
  <https://crates.io/api/v1/crates/cssparser/0.36.0/download>
- `cssparser-macros` 0.6.1:
  <https://crates.io/api/v1/crates/cssparser-macros/0.6.1/download>
- `dtoa-short` 0.3.5:
  <https://crates.io/api/v1/crates/dtoa-short/0.3.5/download>
- `option-ext` 0.2.0:
  <https://crates.io/api/v1/crates/option-ext/0.2.0/download>
- `selectors` 0.24.0:
  <https://crates.io/api/v1/crates/selectors/0.24.0/download>
- `selectors` 0.35.0:
  <https://crates.io/api/v1/crates/selectors/0.35.0/download>

Build tooling:

- `python-build-standalone` release `20260623`:
  <https://github.com/astral-sh/python-build-standalone/tree/20260623>

The complete MPL-2.0 terms are in
`MOZILLA-PUBLIC-LICENSE-2.0.txt`. The additional file-level tqdm terms are in
`TQDM-4.68.4-LICENSE.txt`.

## CDLA-Permissive-2.0 data

- `webpki-roots` 1.0.6 source and certificate data:
  <https://crates.io/api/v1/crates/webpki-roots/1.0.6/download>

The complete data-license agreement is in `CDLA-PERMISSIVE-2.0.txt`.

## Other material upstream sources

- Noto Sans CJK Sans 2.004, used by the Noto Sans SC WOFF2 subsets from which
  `backend/app/data/fonts/SuxiaoyouCJK-Regular.ttf` was produced:
  <https://github.com/notofonts/noto-cjk/releases/tag/Sans2.004>. The 101
  local input shards, weight-400 instantiation/merge procedure, Modified
  Version name, and final SHA-256 are recorded in
  `backend/app/data/fonts/PROVENANCE.md` and
  `SUXIAOYOU-CJK-FONT-OFL-1.1.txt`.
- Anthropic Agent Skills source-equivalent snapshot for the eight permitted
  bundled skill directories:
  <https://github.com/anthropics/skills/tree/7029232b9212482c0476da354b83364bd28fab2f>
- Anthropic Knowledge Work Plugins snapshot for the 15 bundled plugin
  directories:
  <https://github.com/anthropics/knowledge-work-plugins/tree/d2ba7f65cec6502f048286b432dfee2ae59bfdff>
- shadcn/ui (MIT-licensed component source embedded in
  `shadcn-components.tar.gz`):
  <https://github.com/shadcn-ui/ui>
- nanobot: <https://github.com/HKUDS/nanobot>
- OpenClaw source snapshot used when the bridge was introduced:
  <https://github.com/openclaw/openclaw/tree/b75ad800a59009fc47eaa3471410f69046150e59>
- `@tencent-weixin/openclaw-weixin` 1.0.3 package archive used by the personal
  WeChat channel adaptation:
  <https://registry.npmjs.org/@tencent-weixin/openclaw-weixin/-/openclaw-weixin-1.0.3.tgz>
  (SHA-256 `b88b4ca58495d01052ea22171f407c58aa4706295a8c90aa3c7298d8104cec30`).
- Node.js 22.22.0: <https://github.com/nodejs/node/tree/v22.22.0>
- CPython 3.12.13: <https://github.com/python/cpython/tree/v3.12.13>
- Relocatable CPython 3.12.13 macOS runtime archives, metadata, checksums, and
  incorporated-library license files:
  <https://github.com/astral-sh/python-build-standalone/releases/tag/20260623>
- PyInstaller 6.21.0:
  <https://github.com/pyinstaller/pyinstaller/tree/v6.21.0>
- webencodings 0.5.1:
  <https://github.com/gsnedders/python-webencodings/tree/v0.5.1>
- colorama 0.4.6:
  <https://github.com/tartley/colorama/tree/0.4.6>
- pywin32 312 (including the separately licensed `adodbapi` sources present
  in the upstream wheel but forbidden from the application bundle):
  <https://github.com/mhammond/pywin32/tree/b312>

If an exact-version link becomes unavailable, request the corresponding source
through the 苏小有 public repository issue tracker:
<https://github.com/keliihall/suxiaoyou/issues>.
