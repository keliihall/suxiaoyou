# Portable PDF CJK font provenance

`SuxiaoyouCJK-Regular.ttf` is the application-owned PDF export face. It is
bundled so exported Chinese text does not depend on fonts installed on
Windows, macOS, or Linux.

Source inputs:

- Family/version: Noto Sans SC / Noto Sans CJK Sans 2.004 (`2.004-H2` in the
  source name table), Copyright 2014-2021 Adobe, with Reserved Font Name
  `Source`.
- Upstream release: <https://github.com/notofonts/noto-cjk/releases/tag/Sans2.004>
- Local source files: 101 unique `.woff2` files captured from an earlier local
  Next.js build while `frontend/src/app/layout.tsx` still declared Noto Sans SC
  through `next/font/google`. They were selected from the generated
  `font-family:Noto Sans SC` / `font-weight:400` rules in
  `frontend/.next/static/media/`; no font was downloaded for this conversion.
  The web-font declaration and build-time network dependency were removed
  before release, so these input shards are historical build inputs rather
  than part of the final frontend. The exact upstream 2.004 release remains
  linked above.

Conversion performed for v0.7.3:

1. Decode each WOFF2 subset with FontTools 4.63.0 and the locally installed
   Brotli decoder.
2. Instantiate the variable `wght` axis at 400.
3. Remove variable, vertical, and layout tables that ReportLab does not use.
4. Merge the disjoint glyph subsets into one TrueType font.
5. Rename the Modified Version to `Suxiaoyou CJK` so it does not use the
   Reserved Font Name.

Result:

- 13,635 mapped Unicode code points.
- SHA-256
  `c3e1564838ecaa70dcfb786e50670fb8cf3ac4e535584f01b0a00fe158931248`.
- Licensed under the SIL Open Font License 1.1; see `OFL-1.1.txt` beside the
  font and `release-licenses/SUXIAOYOU-CJK-FONT-OFL-1.1.txt` in source
  distributions.
