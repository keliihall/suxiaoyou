# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the 苏小有 backend.

Build with:
    cd backend
    pyinstaller suxiaoyou.spec
"""

import json
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Collect packages that PyInstaller sometimes misses
uvicorn_datas, uvicorn_binaries, uvicorn_hiddenimports = collect_all('uvicorn')
wcmatch_datas, wcmatch_binaries, wcmatch_hiddenimports = collect_all('wcmatch')
croniter_datas, croniter_binaries, croniter_hiddenimports = collect_all('croniter')
anthropic_datas, anthropic_binaries, anthropic_hiddenimports = collect_all('anthropic')
google_genai_datas, google_genai_binaries, google_genai_hiddenimports = collect_all('google.genai')
keyring_datas, keyring_binaries, keyring_hiddenimports = collect_all('keyring')
acp_datas, acp_binaries, acp_hiddenimports = collect_all('acp')


def production_package_only(datas, hiddenimports):
    """Drop SDK test modules and duplicate Python sources from frozen data.

    ``collect_all`` is useful for generated/dynamic SDK modules, but it also
    treats every ``.py`` source file as data in addition to compiling it into
    PYZ. Shipping those duplicates (including google-genai's own test suite)
    bloats the installer and expands its unaudited executable surface.
    """
    filtered_datas = [
        (source, destination)
        for source, destination in datas
        if Path(source).suffix not in {'.py', '.pyc'}
        and not {'tests', 'testing'}.intersection(Path(source).parts)
    ]
    filtered_hiddenimports = [
        module
        for module in hiddenimports
        if not any(
            part in {'tests', 'testing'}
            or part.startswith('test_')
            or part.startswith('_test_')
            for part in module.split('.')
        )
    ]
    return filtered_datas, filtered_hiddenimports


uvicorn_datas, uvicorn_hiddenimports = production_package_only(
    uvicorn_datas, uvicorn_hiddenimports
)
wcmatch_datas, wcmatch_hiddenimports = production_package_only(
    wcmatch_datas, wcmatch_hiddenimports
)
croniter_datas, croniter_hiddenimports = production_package_only(
    croniter_datas, croniter_hiddenimports
)
anthropic_datas, anthropic_hiddenimports = production_package_only(
    anthropic_datas, anthropic_hiddenimports
)
google_genai_datas, google_genai_hiddenimports = production_package_only(
    google_genai_datas, google_genai_hiddenimports
)
keyring_datas, keyring_hiddenimports = production_package_only(
    keyring_datas, keyring_hiddenimports
)
acp_datas, acp_hiddenimports = production_package_only(
    acp_datas, acp_hiddenimports
)

# Resolve paths
backend_dir = os.path.abspath('.')
app_dir = os.path.join(backend_dir, 'app')
repo_root = os.path.abspath(os.path.join(backend_dir, '..'))
frontend_out = os.environ.get(
    'SUXIAOYOU_FRONTEND_OUT',
    os.path.join(repo_root, 'frontend', 'out'),
)
from release_packaging.release_identity import (  # noqa: E402
    ReleaseIdentityPackagingError,
    prepare_frozen_release_identity,
)
from release_packaging.office_renderer_stage import (  # noqa: E402
    OFFICE_RENDERER_PROFILE_ENV,
    SIGNED_AUTHORITATIVE_PROFILE,
    UNSIGNED_DEGRADED_PROFILE,
    OfficeRendererPackagingError,
    bind_office_renderer_analysis_assets,
    prepare_office_renderer_assets,
    verify_office_renderer_analysis_assets,
)

try:
    _release_identity_build = prepare_frozen_release_identity(
        repository_root=repo_root,
        work_root=os.path.join(workpath, 'release-identity'),
    )
except ReleaseIdentityPackagingError as exc:
    sys.stderr.write(
        f'\n[suxiaoyou.spec] FATAL: frozen release identity refused: {exc}\n'
    )
    raise SystemExit(1) from exc

try:
    # ``office_renderer_datas`` remains a compatibility API, but release
    # builds retain the richer snapshot identity so Analysis cannot expand or
    # substitute renderer sources without a post-Analysis verification.
    _office_renderer_build = prepare_office_renderer_assets(
        app_dir=app_dir,
        repo_root=repo_root,
        work_root=os.path.join(workpath, 'office-renderer'),
        release_identity=_release_identity_build.identity,
    )
    _required_office_renderer_assets = list(_office_renderer_build.datas)
except OfficeRendererPackagingError as exc:
    sys.stderr.write(
        f'\n[suxiaoyou.spec] FATAL: Office renderer packaging refused: {exc}\n'
    )
    raise SystemExit(1) from exc

_office_renderer_profile_datas = []
_release_version_parts = tuple(
    int(part) for part in _release_identity_build.identity.app_version.split('.')
)
if _release_version_parts >= (1, 1, 0):
    _office_renderer_profile = os.environ.get(
        OFFICE_RENDERER_PROFILE_ENV,
        SIGNED_AUTHORITATIVE_PROFILE,
    )
    _renderer_bundled = _office_renderer_build.snapshot_root is not None
    if (
        (_office_renderer_profile == SIGNED_AUTHORITATIVE_PROFILE and not _renderer_bundled)
        or (_office_renderer_profile == UNSIGNED_DEGRADED_PROFILE and _renderer_bundled)
    ):
        sys.stderr.write(
            '\n[suxiaoyou.spec] FATAL: Office renderer profile does not match '
            'the admitted bundle inputs\n'
        )
        raise SystemExit(1)
    _office_renderer_profile_marker = os.path.join(
        workpath,
        'office-renderer-profile.json',
    )
    _office_renderer_profile_payload = {
        'app_version': _release_identity_build.identity.app_version,
        'authoritative_authoring_available': False,
        'authoritative_renderer_bundled': _renderer_bundled,
        'contract': 'suxiaoyou-office-renderer-profile-v1',
        'profile': _office_renderer_profile,
        'release_commit': _release_identity_build.identity.release_commit,
        'schema_version': 1,
    }
    with open(_office_renderer_profile_marker, 'w', encoding='ascii', newline='\n') as marker:
        json.dump(
            _office_renderer_profile_payload,
            marker,
            ensure_ascii=True,
            allow_nan=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        marker.write('\n')
    os.chmod(_office_renderer_profile_marker, 0o600)
    _office_renderer_profile_datas.append(
        (_office_renderer_profile_marker, os.path.join('app', 'data'))
    )

_required_pdf_font_files = [
    os.path.join(app_dir, 'data', 'fonts', 'SuxiaoyouCJK-Regular.ttf'),
    os.path.join(app_dir, 'data', 'fonts', 'OFL-1.1.txt'),
    os.path.join(app_dir, 'data', 'fonts', 'PROVENANCE.md'),
]
_required_agent_prompt_files = [
    os.path.join(app_dir, 'agent', 'prompts', 'validator.txt'),
    os.path.join(app_dir, 'agent', 'prompts', 'office_repair.txt'),
]
_required_office_template_assets = [
    (
        os.path.join(app_dir, 'office_templates', 'assets', 'catalog.json'),
        os.path.join('app', 'office_templates', 'assets'),
    ),
    (
        os.path.join(app_dir, 'office_templates', 'assets', 'catalog.sig.json'),
        os.path.join('app', 'office_templates', 'assets'),
    ),
    (
        os.path.join(
            app_dir,
            'office_templates',
            'assets',
            'templates',
            'business-brief.docx',
        ),
        os.path.join('app', 'office_templates', 'assets', 'templates'),
    ),
    (
        os.path.join(
            app_dir,
            'office_templates',
            'assets',
            'templates',
            'project-tracker.xlsx',
        ),
        os.path.join('app', 'office_templates', 'assets', 'templates'),
    ),
    (
        os.path.join(
            app_dir,
            'office_templates',
            'assets',
            'templates',
            'status-update.pptx',
        ),
        os.path.join('app', 'office_templates', 'assets', 'templates'),
    ),
]

# Data files to include.
#
# Every entry here is REQUIRED. If any source path is missing at build time,
# the spec aborts instead of silently shipping a broken bundle — that is how
# we ended up releasing 1.0.7 with no mobile PWA (frontend_out was never
# copied, /m returned 404 over the cloudflare tunnel). Never weaken this
# check; add a new required path if you need a new resource.
_required_datas = [
    # Agent prompt templates
    (os.path.join(app_dir, 'agent', 'prompts'), os.path.join('app', 'agent', 'prompts')),
    # Alembic migrations
    (os.path.join(backend_dir, 'alembic'), 'alembic'),
    (os.path.join(backend_dir, 'alembic.ini'), '.'),
    # Bundled data (skills, plugins, connectors)
    (os.path.join(app_dir, 'data'), os.path.join('app', 'data')),
    # v1.1+ binds the signed renderer to the frozen app version and exact
    # clean Git checkout. This resource is generated inside PyInstaller's
    # private work tree and has no source-tree or environment fallback.
    *_release_identity_build.datas,
    # v1.1+ carries a generated, commit-bound declaration of whether this is a
    # signed authoritative renderer build or the explicitly unsupported
    # unsigned-degraded profile. The marker never grants Office write authority.
    *_office_renderer_profile_datas,
    # The signed-authoritative profile receives exactly one lock-bound native
    # renderer from the external atomic staging chain; unsigned-degraded
    # receives none. ``*_required_office_renderer_assets`` are deliberately
    # NOT Analysis inputs: PyInstaller would reclassify real ELF/PE/Mach-O data
    # as BINARY and rewrite signed bytes. Any admitted bytes are attached to
    # ``a.datas`` only after Analysis has completed all binary processing.
    # Frontend static export — served by FastAPI at /m for the mobile PWA
    # when a phone connects through the cloudflare tunnel. Without this,
    # remote access is effectively broken even though the desktop UI works
    # (Tauri reads the frontend from its own resources).
    (frontend_out, 'frontend_out'),
    # Signed first-party Office catalog and its immutable OOXML assets. Keep
    # this list explicit so unrelated files cannot silently enter a release.
    *_required_office_template_assets,
]

_missing = [src for src, _ in _required_datas if not os.path.exists(src)]
_missing.extend(path for path in _required_pdf_font_files if not os.path.isfile(path))
_missing.extend(path for path in _required_agent_prompt_files if not os.path.isfile(path))
if _missing:
    sys.stderr.write(
        '\n[suxiaoyou.spec] FATAL: required build inputs are missing:\n'
    )
    for p in _missing:
        sys.stderr.write(f'  - {p}\n')
    sys.stderr.write(
        '\nBuild the frontend (DESKTOP_BUILD=true next build) and make sure\n'
        'backend/alembic, both required backend/app/agent/prompts,\n'
        'backend/app/data, and\n'
        'backend/app/office_templates/assets all exist before running\n'
        'pyinstaller. The app/data check includes the bundled PDF CJK font,\n'
        'OFL notice, and provenance record. Aborting\n'
        'so we never ship a half-baked bundle.\n'
    )
    raise SystemExit(1)

# Sanity-check that the frontend export actually contains the mobile entry
# point. A stale `frontend/out` from a non-desktop build would otherwise
# slip past the existence check above.
_mobile_entry = os.path.join(frontend_out, 'm.html')
_next_dir = os.path.join(frontend_out, '_next')
if not os.path.isfile(_mobile_entry) or not os.path.isdir(_next_dir):
    sys.stderr.write(
        f'\n[suxiaoyou.spec] FATAL: frontend export at {frontend_out} is incomplete.\n'
        f'Expected {_mobile_entry} and {_next_dir}/ to exist.\n'
        'Rebuild the frontend with DESKTOP_BUILD=true before packaging.\n'
    )
    raise SystemExit(1)

datas = list(_required_datas)

# Hidden imports — modules that PyInstaller can't detect automatically
hiddenimports = [
    # FastAPI and dependencies
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'fastapi',
    'starlette',
    'pydantic',
    'pydantic_settings',

    # Database
    'sqlalchemy',
    'sqlalchemy.ext.asyncio',
    'aiosqlite',
    'alembic',

    # SSE
    'sse_starlette',

    # LLM
    'openai',
    'anthropic',
    'google.genai',
    'httpx',
    'tiktoken',
    'tiktoken_ext',
    'tiktoken_ext.openai_public',

    # Document processing
    'pypdf',
    'docx',
    'openpyxl',
    'pptx',
    'markdown',

    # PDF generation
    'reportlab',
    'reportlab.graphics.barcode',
    'reportlab.graphics.barcode.code128',
    'reportlab.graphics.barcode.code39',
    'reportlab.graphics.barcode.code93',
    'reportlab.graphics.barcode.common',
    'reportlab.graphics.barcode.eanbc',
    'reportlab.graphics.barcode.ecc200datamatrix',
    'reportlab.graphics.barcode.fourstate',
    'reportlab.graphics.barcode.lto',
    'reportlab.graphics.barcode.qr',
    'reportlab.graphics.barcode.usps',
    'reportlab.graphics.barcode.usps4s',
    'reportlab.graphics.barcode.widgets',

    # Data science
    'pandas',
    'numpy',
    'matplotlib',

    # QR code generation (lazy import in remote.py)
    'qrcode',
    'qrcode.image',
    'qrcode.image.pil',
    'qrcode.main',

    # Utilities
    'ulid',
    'aiofiles',
    'keyring',
    'keyring.backends.macOS',
    'keyring.backends.Windows',
    'keyring.backends.SecretService',
    'keyring.backends.chainer',
    'keyring.backends.fail',
    'yaml',
    'anyio',
    'anyio._backends',
    'anyio._backends._asyncio',
    # ACP SDK and the two frozen entry paths. ``collect_all('acp')`` below
    # covers SDK-internal dynamic imports; these explicit roots make the
    # shipping surface reviewable and keep the application bridge reachable.
    'acp',
    'acp.meta',
    'acp.schema',
    'acp.stdio',
    'wcmatch',
    'wcmatch.glob',
    'wcmatch.fnmatch',
    'wcmatch.pathlib',
    'croniter',

    # Web extraction (lazy imports in web_fetch.py)
    'readabilipy',
    'markdownify',

    # App modules
    'app.main',
    'app.config',
    'app.dependencies',
    'app.api',
    'app.api.router',
    'app.api.chat',
    'app.api.sessions',
    'app.api.messages',
    'app.api.models',
    'app.api.agents',
    'app.api.tools',
    'app.api.artifacts',
    'app.api.pdf',
    'app.api.files',
    'app.api.skills',
    'app.api.health',
    'app.session.processor',
    'app.session.llm',
    'app.session.manager',
    'app.session.compaction',
    'app.session.system_prompt',
    'app.session.retry',
    'app.streaming.events',
    'app.streaming.manager',
    'app.models.base',
    'app.models.session',
    'app.models.message',
    'app.models.project',
    'app.models.todo',
    'app.agent.agent',
    'app.agent.permission',
    'app.provider.base',
    'app.provider.anthropic_provider',
    'app.provider.gemini_provider',
    'app.provider.openrouter',
    'app.provider.openai_compat',
    'app.provider.registry',
    'app.tool.registry',
    'app.tool.context',
    'app.tool.sandbox_self_test',
    'app.tool.sandbox_worker',
    'app.tool.builtin.read',
    'app.tool.builtin.write',
    'app.tool.builtin.edit',
    'app.tool.builtin.bash',
    'app.tool.builtin.code_execute',
    'app.tool.builtin.glob_tool',
    'app.tool.builtin.grep',
    'app.tool.builtin.artifact',
    'app.tool.builtin.question',
    'app.tool.builtin.todo',
    'app.tool.builtin.task',
    'app.tool.builtin.skill',
    'app.tool.builtin.web_fetch',
    'app.tool.builtin.web_search',
    'app.tool.builtin.plan',
    'app.tool.builtin.invalid',
    'app.acp',
    'app.acp.bridge',
    'app.acp.cli',
    'app.acp.self_test',
    'app.acp.server',
    'app.acp.session_bridge',
    'app.acp.stdio',
    # v1.1 gated modules are explicit even when PyInstaller can currently
    # discover them through nested imports. Released bundle smoke exercises
    # the open gate graph, while explicit collection keeps every entrypoint
    # independently auditable inside PYZ.
    'app.api.runtime_control',
    'app.api.office_user_templates',
    'app.api.office_v2',
    'app.hooks',
    'app.hooks.config',
    'app.hooks.dispatcher',
    'app.hooks.models',
    'app.hooks.registry',
    'app.hooks.runner',
    'app.hooks.runtime',
    'app.hooks.trust',
    'app.models.checkpoint_change',
    'app.models.office_user_template',
    'app.models.session_checkpoint',
    'app.models.turn_run',
    'app.models.workspace_instance',
    'app.office_rendering',
    'app.office_rendering.attested',
    'app.office_rendering.cache',
    'app.office_rendering.deployment',
    'app.office_rendering.libreoffice',
    'app.office_rendering.native_bundle',
    'app.office_rendering.native_sandbox',
    'app.office_rendering.native_sandbox_behavior',
    'app.office_rendering.probe',
    'app.office_rendering.process_runner',
    'app.office_rendering.release_identity',
    'app.office_rendering.runtime',
    'app.office_rendering.sandbox',
    'app.office_rendering.service',
    'release_packaging.office_renderer_trust',
    'app.office_templates',
    'app.office_templates.bundled',
    'app.office_templates.instantiation',
    'app.office_templates.policies',
    'app.office_templates.registry',
    'app.office_templates.substitution',
    'app.office_templates.user',
    'app.office_templates.validation',
    'app.office_validation',
    'app.office_validation.draft',
    'app.office_validation.orchestrator',
    'app.office_validation.precommit',
    'app.office_validation.precommit_repair',
    'app.office_validation.repair_agent',
    'app.office_validation.runtime',
    'app.office_validation.startup',
    'app.office_validation.structure',
    'app.office_validation.visual',
    'app.release_readiness',
    'app.runtime.checkpoint_runtime',
    'app.runtime.events',
    'app.runtime.frozen_self_test',
    'app.runtime.rewind',
    'app.runtime.v11_readiness',
    'app.storage.checkpoints',
    'app.validation_agent',
    'app.validation_agent.contracts',
    'app.validation_agent.persistence',
    'app.validation_agent.scheduler',
    'app.validation_agent.service',
    'app.worktree',
    'app.worktree.runtime',
    'app.worktree.service',
    'app.skill.registry',
    'app.storage.database',
    'app.schemas',
]

a = Analysis(
    ['run.py'],
    pathex=(
        [backend_dir, str(_release_identity_build.binding_module_root)]
        if _release_identity_build.binding_module_root is not None
        else [backend_dir]
    ),
    binaries=(
        uvicorn_binaries
        + wcmatch_binaries
        + croniter_binaries
        + anthropic_binaries
        + google_genai_binaries
        + keyring_binaries
        + acp_binaries
    ),
    datas=(
        datas
        + uvicorn_datas
        + wcmatch_datas
        + croniter_datas
        + anthropic_datas
        + google_genai_datas
        + keyring_datas
        + acp_datas
    ),
    hiddenimports=(
        hiddenimports
        + uvicorn_hiddenimports
        + wcmatch_hiddenimports
        + croniter_hiddenimports
        + anthropic_hiddenimports
        + google_genai_hiddenimports
        + keyring_hiddenimports
        + acp_hiddenimports
        + list(_release_identity_build.hiddenimports)
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ── Testing & dev ────────────────────────────────────────────
        'tkinter',
        'test',
        'unittest',
        'pytest',
        'pytest_asyncio',
        '_pytest',
        'IPython',
        'ipykernel',
        'notebook',
        'jupyterlab',

        # ── Deep Learning frameworks (~4.5 GB) ───────────────────────
        'torch',
        'torchvision',
        'torchaudio',
        'torch._C',
        'torch.cuda',
        'paddle',
        'paddlepaddle',

        # ── ML / NLP / CV libraries ──────────────────────────────────
        'transformers',
        'tokenizers',
        'huggingface_hub',
        'hf_xet',
        'safetensors',
        'datasets',
        'accelerate',
        'bitsandbytes',
        'onnxruntime',
        'onnx',
        'sklearn',
        'scikit-learn',
        'scipy',
        'spacy',
        'thinc',
        'blis',
        'cymem',
        'preshed',
        'murmurhash',
        'srsly',
        'wasabi',
        'langcodes',
        'catalogue',
        'confection',
        'weasel',
        'nltk',
        'gensim',
        'lightgbm',
        'xgboost',
        'catboost',
        'sympy',

        # ── Computer Vision ──────────────────────────────────────────
        'cv2',
        'opencv-python',
        'imageio',
        'imageio_ffmpeg',
        'skimage',
        'scikit-image',

        # ── Numba / LLVM ─────────────────────────────────────────────
        'numba',
        'llvmlite',

        # ── Arrow / Parquet (pulled by pandas but not needed at runtime)
        'pyarrow',

        # ── Audio / Video / Game ─────────────────────────────────────
        'pygame',
        'librosa',
        'soundfile',
        'pydub',
        'yt_dlp',

        # ── AWS SDK ──────────────────────────────────────────────────
        'botocore',
        'boto3',
        's3transfer',

        # ── gRPC / Proto ─────────────────────────────────────────────
        'grpc',
        'grpcio',
        'google.protobuf',

        # ── Heavy optional libs ──────────────────────────────────────
        'gradio',
        'altair',
        'plotly',
        'dash',
        'bokeh',
        'seaborn',
        'statsmodels',
        'psycopg2',
        'psycopg',
        'psycopg_binary',
        'redis',
        'celery',
        'dask',
        'distributed',
        'ray',
        'mlflow',
        'wandb',
        'tensorboard',
        'tensorflow',
        'keras',
        'flax',
        'jax',
        'jaxlib',
        'einops',
        'triton',
        'pdfplumber',
        'pdfminer',
        'camelot',
        'tabula',
        'fpdf2',
        'fpdf',

        # ── Crypto / misc pulled by yt-dlp ───────────────────────────
        'Crypto',
        'Cryptodome',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


def _verify_office_renderer_analysis(stage, *, attach=False):
    try:
        if attach:
            bind_office_renderer_analysis_assets(
                _office_renderer_build,
                a.datas,
                a.binaries,
            )
        else:
            verify_office_renderer_analysis_assets(
                _office_renderer_build,
                a.datas,
                a.binaries,
            )
    except OfficeRendererPackagingError as exc:
        sys.stderr.write(
            f'\n[suxiaoyou.spec] FATAL: Office renderer {stage} '
            f'verification refused: {exc}\n'
        )
        raise SystemExit(1) from exc


# Keep native renderer bytes outside Analysis' automatic BINARY
# reclassification and dependency rewriting. After Analysis fully returns,
# reject any ambient collision and inject each admitted source as exact DATA.
_verify_office_renderer_analysis('post-Analysis attach', attach=True)

# PyInstaller prepends the entry-script directory to module search paths.  A
# source-root or installed module with this reserved name must never replace
# the generated digest binding, irrespective of ``pathex`` ordering.
if _release_identity_build.binding_module_path is not None:
    _identity_binding_entries = [
        entry
        for entry in a.pure
        if entry[0] == _release_identity_build.hiddenimports[0]
    ]
    _expected_identity_binding = os.path.realpath(
        _release_identity_build.binding_module_path
    )
    if (
        len(_identity_binding_entries) != 1
        or os.path.realpath(_identity_binding_entries[0][1])
        != _expected_identity_binding
    ):
        sys.stderr.write(
            '\n[suxiaoyou.spec] FATAL: frozen release identity binding '
            'module origin was shadowed or omitted\n'
        )
        raise SystemExit(1)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='suxiaoyou-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Windows must use the console bootloader so redirected stdin/stdout/stderr
    # remain real pipe handles for Tauri. The Rust launcher adds
    # CREATE_NO_WINDOW, so users still never see a console window.
    console=sys.platform == 'win32',
)

# COLLECT performs the actual source-file reads. Recheck both the Analysis TOC
# and every captured inode/digest immediately before final assembly.
_verify_office_renderer_analysis('pre-COLLECT')

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='suxiaoyou-backend',
)
