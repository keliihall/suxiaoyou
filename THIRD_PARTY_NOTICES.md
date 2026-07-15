# Third-party notices

This file records the mandatory static notices shipped with every 苏小有
desktop installer. The accompanying license texts are under
`release-licenses/` in source distributions and `licenses/third-party/` in
installed applications.

## Copyright and adapted code

Portions of the codebase are Copyright 2026 W Axis Inc. and are licensed
under the Apache License 2.0. The complete Apache License is in `LICENSE`,
and the retained copyright attribution is in `NOTICE`.

Portions of the messaging-channel implementation are adapted from
[nanobot](https://github.com/HKUDS/nanobot), Copyright (c) 2025-present Xubin
Ren and the nanobot contributors, under the MIT License. See
`release-licenses/NANOBOT-MIT.txt`.

Portions of the messaging bridge implementation are adapted from
[OpenClaw](https://github.com/openclaw/openclaw), Copyright (c) 2025 Peter
Steinberger, under the MIT License. See
`release-licenses/OPENCLAW-MIT.txt`.

Portions of the personal WeChat channel implementation are adapted from
[`@tencent-weixin/openclaw-weixin`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)
1.0.3, Copyright (c) 2026 Tencent Inc., under the MIT License. See
`release-licenses/TENCENT-WEIXIN-OPENCLAW-1.0.3-MIT.txt`.

## Anthropic Agent Skills

Portions of the following bundled skill directories are derived from the
[Anthropic Agent Skills](https://github.com/anthropics/skills) repository at
source-equivalent revision
`7029232b9212482c0476da354b83364bd28fab2f`:

- `algorithmic-art`
- `canvas-design`
- `frontend-design`
- `mcp-builder`
- `skill-creator`
- `theme-factory`
- `web-artifacts-builder`
- `webapp-testing`

Copyright 2026 Anthropic, PBC. Licensed under the Apache License, Version 2.0.
Local changes include 苏小有 compatibility guidance, removal of
Claude-specific wording, and normalization of executable file modes. See
`release-licenses/ANTHROPIC-SKILLS-APACHE-2.0.txt`.

The `canvas-design/canvas-fonts` directory contains 54 font files from 27
families under the SIL Open Font License 1.1. The complete family-specific
copyright notices, Reserved Font Names, and OFL text are in
`release-licenses/ANTHROPIC-CANVAS-FONTS-OFL-1.1.txt`; the individual
`*-OFL.txt` files are also retained beside the fonts.

## Portable PDF CJK font

PDF export embeds `backend/app/data/fonts/SuxiaoyouCJK-Regular.ttf`, a
Modified Version derived from the Noto Sans SC / Noto Sans CJK Sans 2.004
webfont subsets. Copyright 2014-2021 Adobe
(<http://www.adobe.com/>), with Reserved Font Name "Source". The 101 existing
Next.js WOFF2 subsets were instantiated at weight 400, merged, and renamed
`Suxiaoyou CJK`; no system font is copied at runtime. The font remains under
the SIL Open Font License 1.1. Exact input selection, conversion steps, and
the final SHA-256 are recorded beside the font in `PROVENANCE.md`. See
`release-licenses/SUXIAOYOU-CJK-FONT-OFL-1.1.txt` for the complete notice and
license.

The `web-artifacts-builder/scripts/shadcn-components.tar.gz` archive contains
MIT-licensed components from [shadcn/ui](https://github.com/shadcn-ui/ui),
Copyright (c) 2023 shadcn. See `release-licenses/SHADCN-UI-MIT.txt`.

## Anthropic Knowledge Work Plugins

The 15 bundled plugin directories under `backend/app/data/plugins/` are
derived from the
[Anthropic Knowledge Work Plugins](https://github.com/anthropics/knowledge-work-plugins)
repository at revision `d2ba7f65cec6502f048286b432dfee2ae59bfdff`:

- `bio-research`, `cowork-plugin-management`, `customer-support`, `data`
- `design`, `engineering`, `enterprise-search`, `finance`
- `human-resources`, `legal`, `marketing`, `operations`
- `product-management`, `productivity`, `sales`

Published by Anthropic and contributed to by its contributors. Licensed under
the Apache License, Version 2.0. 苏小有 modifies selected `.mcp.json`
files to use compatible MCP endpoints and locally disabled connectors. See
`release-licenses/ANTHROPIC-KNOWLEDGE-WORK-PLUGINS-APACHE-2.0.txt`.

## Bundled runtimes and build output

Desktop distributions include or may include the following material runtime
components:

- Node.js 22.22.0. See `release-licenses/NODEJS-22.22.0-LICENSE.txt`, which
  also contains the upstream notices for libraries incorporated into Node.js.
- CPython 3.12. See `release-licenses/CPYTHON-3.12.13-LICENSE.txt` for the
  CPython license. macOS builds use the relocatable CPython 3.12.13 runtime
  from Astral's `python-build-standalone` release `20260623`; its exact
  archives, checksums, metadata, and all 19 runtime/incorporated-library
  notices are retained under
  `release-licenses/python-runtime/python-build-standalone-20260623/`.
- A PyInstaller 6.21.0 bootloader and run-time hooks. PyInstaller is
  GPL-2.0-or-later with the upstream Bootloader Exception for embedded
  bootloader files; its run-time hooks are Apache-2.0. See
  `release-licenses/PYINSTALLER-6.21.0-COPYING.txt` for the complete terms and
  exception.

The bundled Node.js directory also retains npm's own `LICENSE` and the license
files supplied by npm's included dependencies.

The `python-build-standalone` build system is MPL-2.0. Its build-system
license is identified separately from the licenses governing the resulting
runtime; see the runtime provenance directory and
`release-licenses/SOURCE_AVAILABILITY.md`.

## MPL-2.0 and data notices

The release graph contains Mozilla Public License 2.0 components, including
the Rust crates `cssparser`, `cssparser-macros`, `dtoa-short`, `option-ext`,
and `selectors`, plus the Python packages `certifi` and `tqdm`. See
`release-licenses/MOZILLA-PUBLIC-LICENSE-2.0.txt`,
`release-licenses/TQDM-4.68.4-LICENSE.txt`, and
`release-licenses/SOURCE_AVAILABILITY.md` for exact versions and source links.

The `webpki-roots` 1.0.6 crate contains Mozilla root-certificate data under
the Community Data License Agreement - Permissive - Version 2.0. See
`release-licenses/CDLA-PERMISSIVE-2.0.txt` and the source-availability file.

The Windows dependency graph also contains `colorama` 0.4.6 and `pywin32`
312. Their wheel-supplied notices are preserved in
`release-licenses/COLORAMA-0.4.6-LICENSE.txt` and
`release-licenses/PYWIN32-312-LICENSES.txt`. The release verifier rejects the
unused `adodbapi` LGPL component if it appears in the PyInstaller payload.

PDF export includes `webencodings` 0.5.1 under the BSD-3-Clause license,
Copyright (c) 2012 by Simon Sapin. Its complete terms are preserved in
`release-licenses/WEBENCODINGS-0.5.1-BSD-3-CLAUSE.txt`.

## Dependency inventory scope

The exact package inventories are maintained by the release lock files:

- JavaScript: `package-lock.json`, `frontend/package-lock.json`, and
  `desktop-tauri/package-lock.json`
- Python: `backend/requirements.txt`
- Rust: `desktop-tauri/src-tauri/Cargo.lock`

The corresponding bundled license reports are
`release-licenses/JAVASCRIPT-LICENSES.txt`,
`release-licenses/PYTHON-LICENSES.txt`, and
`release-licenses/RUST-LICENSES.html`.

The checked-in `release-licenses/` directory is a curated, mandatory baseline;
it also contains generated JavaScript, Python, and Rust reports for the locked
v1.0.0 production graphs. Platform-specific build-only JavaScript packages can
vary by build host and are not part of the installed static web application.
Each dependency remains subject to its own license, and package-specific
license files retained inside runtime payloads remain in force. Release
maintainers must regenerate and review these reports whenever a lock changes.

The project intentionally excludes Anthropic's separately licensed proprietary
`docx`, `pdf`, `pptx`, and `xlsx` document skills, along with other
dependencies that do not grant redistribution rights. The Anthropic material
listed above is separately published under Apache-2.0.
