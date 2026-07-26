# Windows ARM64 Python dependency overrides

The CPython 3.12 Windows ARM64 release graph replaces five versions from the
cross-platform `PYTHON-LICENSES.txt` baseline because the baseline versions do
not publish official `win_arm64` wheels. The exact graph is
`backend/requirements-windows-arm64.txt`; the build accepts only the official
wheel hashes below.

| Package | Baseline → Windows ARM64 | SPDX license | Exact source archive (SHA-256) | Official CPython 3.12 `win_arm64` wheel (SHA-256) |
| --- | --- | --- | --- | --- |
| greenlet | 3.1.1 → 3.3.1 | `MIT AND Python-2.0` | [greenlet-3.3.1.tar.gz](https://files.pythonhosted.org/packages/8a/99/1cd3411c56a410994669062bd73dd58270c00cc074cac15f385a1fd91f8a/greenlet-3.3.1.tar.gz) (`41848f3230b58c08bb43dee542e74a2a2e34d3c59dc3076cec9151aeeedcae98`) | [greenlet-3.3.1-cp312-cp312-win_arm64.whl](https://files.pythonhosted.org/packages/c8/ab/717c58343cf02c5265b531384b248787e04d8160b8afe53d9eec053d7b44/greenlet-3.3.1-cp312-cp312-win_arm64.whl) (`bfb2d1763d777de5ee495c85309460f6fd8146e50ec9d0ae0183dbf6f0a829d1`) |
| MarkupSafe | 3.0.2 → 3.0.3 | `BSD-3-Clause` | [markupsafe-3.0.3.tar.gz](https://files.pythonhosted.org/packages/7e/99/7690b6d4034fffd95959cbe0c02de8deb3098cc577c67bb6a24fe5d7caa7/markupsafe-3.0.3.tar.gz) (`722695808f4b6457b320fdc131280796bdceb04ab50fe1795cd540799ebe1698`) | [markupsafe-3.0.3-cp312-cp312-win_arm64.whl](https://files.pythonhosted.org/packages/e5/f1/216fc1bbfd74011693a4fd837e7026152e89c4bcf3e77b6692fba9923123/markupsafe-3.0.3-cp312-cp312-win_arm64.whl) (`35add3b638a5d900e807944a078b51922212fb3dedb01633a8defc4b01a3c85f`) |
| NumPy | 1.26.4 → 2.3.0 | `BSD-3-Clause` (plus the bundled-component terms enumerated in the wheel license) | [numpy-2.3.0.tar.gz](https://files.pythonhosted.org/packages/f3/db/8e12381333aea300890829a0a36bfa738cac95475d88982d538725143fd9/numpy-2.3.0.tar.gz) (`581f87f9e9e9db2cba2141400e160e9dd644ee248788d6f90636eeb8fd9260a6`) | [numpy-2.3.0-cp312-cp312-win_arm64.whl](https://files.pythonhosted.org/packages/c2/1c/6d343e030815c7c97a1f9fbad00211b47717c7fe446834c224bd5311e6f1/numpy-2.3.0-cp312-cp312-win_arm64.whl) (`bd8df082b6c4695753ad6193018c05aac465d634834dca47a3ae06d4bb22d9ea`) |
| pandas | 2.3.3 → 3.0.0 | `BSD-3-Clause` | [pandas-3.0.0.tar.gz](https://files.pythonhosted.org/packages/de/da/b1dc0481ab8d55d0f46e343cfe67d4551a0e14fcee52bd38ca1bd73258d8/pandas-3.0.0.tar.gz) (`0facf7e87d38f721f0af46fe70d97373a37701b1c09f7ed7aeeb292ade5c050f`) | [pandas-3.0.0-cp312-cp312-win_arm64.whl](https://files.pythonhosted.org/packages/d4/64/ff571be435cf1e643ca98d0945d76732c0b4e9c37191a89c8550b105eed1/pandas-3.0.0-cp312-cp312-win_arm64.whl) (`da768007b5a33057f6d9053563d6b74dd6d029c337d93c6d0d22a763a5c2ecc0`) |
| PyYAML | 6.0.2 → 6.0.3 | `MIT` | [pyyaml-6.0.3.tar.gz](https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz) (`d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f`) | [pyyaml-6.0.3-cp312-cp312-win_arm64.whl](https://files.pythonhosted.org/packages/1a/08/67bd04656199bbb51dbed1439b7f27601dfb576fb864099c7ef0c3e55531/pyyaml-6.0.3-cp312-cp312-win_arm64.whl) (`64386e5e707d03a7e172c0701abfb7e10f0fb753ee1d773128192742712a98fd`) |

## Complete license-text locations

The complete family license texts are reproduced in
`release-licenses/PYTHON-LICENSES.txt` under the baseline sections `greenlet
3.1.1`, `MarkupSafe 3.0.2`, `numpy 1.26.4`, `pandas 2.3.3`, and `PyYAML
6.0.2`. The replacement wheels retain the same license families. Their exact
version-specific license payloads are the following wheel members; the hashes
make that correspondence independently auditable:

- `greenlet-3.3.1.dist-info/licenses/LICENSE`
  (`ab977b654605670319509481ece49198f405a129372ac38c96f51cd96c2f452e`)
  and `LICENSE.PSF`
  (`69efa689cc7aec7736236d3039c1d665e4b0c734a5bfea5abf633023abb2fff4`)
- `markupsafe-3.0.3.dist-info/licenses/LICENSE.txt`
  (`4631ec0db5fd90a547e336817264c6798214338146f8ac94b4a57f96ee8c9ec4`)
- `numpy-2.3.0.dist-info/LICENSE.txt`
  (`233d7ab5c32bc4aed6848f647d397007965acdb691c86fe27d17cc3eb60359d1`);
  this is authoritative for bundled NumPy/OpenBLAS component notices
- `pandas-3.0.0.dist-info/LICENSE`
  (`9850f6b5bdef7346503065807b878deb8058b976c5c329f4c4d23bf4796ce9ae`)
- `pyyaml-6.0.3.dist-info/licenses/LICENSE`
  (`8d3928f9dc4490fd635707cb88eb26bd764102a7282954307d3e5167a577e8a4`)

## Statically linked OpenSSL

The locally built `cryptography 48.0.1` wheel statically links OpenSSL 4.0.1;
no vcpkg artifact is used. OpenSSL 4.0.1 is Apache-2.0. Its exact source is
[openssl-4.0.1.tar.gz](https://github.com/openssl/openssl/releases/download/openssl-4.0.1/openssl-4.0.1.tar.gz),
SHA-256
`2db3f3a0d6ea4b59e1f094ace2c8cd536dffb87cdc39084c5afa1e6f7f37dd09`.
The complete Apache-2.0 terms are reproduced in the repository root
`LICENSE`; OpenSSL's version-specific `LICENSE.txt` remains in that locked
source archive. Build flags, source identity, static library hashes, and the
runtime `OpenSSL_version()` result are recorded in the Windows ARM64
wheelhouse manifest.
