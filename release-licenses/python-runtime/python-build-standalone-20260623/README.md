# Python 3.12.13 macOS runtime provenance

The macOS installers are built with the relocatable CPython 3.12.13 runtime
published by Astral's `python-build-standalone` release `20260623`. This avoids
accidentally embedding a Homebrew Python compiled for the build machine's much
newer macOS version.

The release workflow uses `uv 0.11.28`, whose managed-Python catalog resolves
CPython 3.12.13 on macOS to the following `install_only` archives:

- Apple Silicon: `cpython-3.12.13+20260623-aarch64-apple-darwin-install_only.tar.gz`
  (`3724aa4dafb5f7b6c2cf98e89914e4248dc6bd2fe40407df4a2d73de99615f16`)
- Intel: `cpython-3.12.13+20260623-x86_64-apple-darwin-install_only.tar.gz`
  (`7c57fdd1fa675190093700eb0d8e7117e1f9eae7c30a46dea5f8d5266bcfc791`)

The corresponding full archives are the source of the checked-in runtime
metadata and license directory:

- Apple Silicon: `cpython-3.12.13+20260623-aarch64-apple-darwin-pgo+lto-full.tar.zst`
  (`ce5a2d552077d869f69dc25d834c2fe4d036f9d78e770fb5d916273db802cabc`)
- Intel: `cpython-3.12.13+20260623-x86_64-apple-darwin-pgo+lto-full.tar.zst`
  (`3b2ee510354f51b6bda71fec7d5cf70dfad29ed672aa5847d1f5216ee22fc5ef`)

All four files are available from:

<https://github.com/astral-sh/python-build-standalone/releases/tag/20260623>

The two full archives contain identical `python/licenses/` directories. The
19 files are retained once under `licenses/`. Architecture-specific
`PYTHON.json` files are retained under `metadata/`; they record deployment
targets of macOS 11.0 for arm64 and macOS 10.15 for x86_64, both below this
project's macOS 13.3 minimum.

`build-system/LICENSE.python-build-standalone.MPL-2.0.txt` covers the upstream
build system itself and is kept separately from the licenses governing the
resulting Python runtime and its incorporated libraries.

