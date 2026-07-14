"""Channel configuration schema for 苏小有.

Replaces the nanobot Config dependency with a lightweight schema
that reads from 苏小有's data/channels.json.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.auth.credential_store import (
    CredentialStoreError,
    StagedSecretTree,
    prepare_stale_secret_cleanup,
    resolve_secret_tree,
    stage_protected_secret_tree,
)
from app.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

# Default path for channels configuration
_DEFAULT_CONFIG_PATH = Path("data/channels.json")
_CREDENTIAL_NAMESPACE = "channels"


def _discard_failed_config_stage(path: Path, staged: StagedSecretTree) -> None:
    if not path.is_file():
        staged.discard_unreferenced()
        return
    try:
        installed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        installed = staged.value
    staged.discard_unreferenced((installed,))


class ChannelsConfig(BaseModel):
    """Top-level channels configuration."""

    # Per-channel configs stored as dicts (flexible schema per channel)
    # e.g. {"telegram": {"enabled": true, "token": "...", "allow_from": ["*"]}, ...}
    channels: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Global settings
    send_progress: bool = True
    send_tool_hints: bool = True
    send_max_retries: int = 3


def load_channels_config(config_path: Path | None = None) -> ChannelsConfig:
    """Load channels configuration from JSON file."""
    path = config_path or _DEFAULT_CONFIG_PATH
    if not path.exists():
        logger.info("No channels config at %s — using defaults", path)
        return ChannelsConfig()

    try:
        data = load_channels_config_dict(path)
        return ChannelsConfig.model_validate(data)
    except CredentialStoreError:
        raise
    except Exception as e:
        logger.warning("Failed to load channels config from %s: %s", path, e)
        return ChannelsConfig()


def save_channels_config(config: ChannelsConfig, config_path: Path | None = None) -> None:
    """Save channels configuration to JSON file."""
    path = config_path or _DEFAULT_CONFIG_PATH
    save_channels_config_dict(config.model_dump(), path)
    logger.info("Saved channels config to %s", path)


def load_channels_config_dict(config_path: Path | None = None) -> dict[str, Any]:
    """Load, migrate, and resolve the channel configuration."""

    path = config_path or _DEFAULT_CONFIG_PATH
    if not path.exists():
        return {"channels": {}}
    try:
        previous_content = path.read_bytes()
        data = json.loads(previous_content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse channels config from %s: %s", path, exc)
        return {"channels": {}}
    if not isinstance(data, dict):
        logger.warning("Channels config at %s is not an object", path)
        return {"channels": {}}

    staged = stage_protected_secret_tree(
        _CREDENTIAL_NAMESPACE,
        data,
        previous_value=data,
    )
    protected = staged.value
    if protected != data:
        cleanup_transaction = None
        try:
            next_text = json.dumps(protected, indent=2, ensure_ascii=False) + "\n"
            cleanup_transaction = prepare_stale_secret_cleanup(
                data,
                protected,
                evidence_path=path,
                previous_exists=True,
                previous_content=previous_content,
                next_exists=True,
                next_content=next_text,
            )
            _write_protected_config(path, next_text)
        except Exception as exc:
            if cleanup_transaction is not None:
                cleanup_transaction.cancel()
            _discard_failed_config_stage(path, staged)
            raise CredentialStoreError(
                f"Cannot erase plaintext channel credentials in {path}: {exc}"
            ) from exc
        if cleanup_transaction is not None:
            cleanup_transaction.commit()
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    resolved = resolve_secret_tree(protected)
    if not isinstance(resolved, dict):
        raise ValueError("Resolved channels config is not an object")
    return resolved


def save_channels_config_dict(
    data: dict[str, Any],
    config_path: Path | None = None,
) -> None:
    """Persist only opaque credential references in ``channels.json``."""

    path = config_path or _DEFAULT_CONFIG_PATH
    previous: dict[str, Any] = {}
    previous_exists = path.is_file()
    previous_content = b""
    if previous_exists:
        try:
            previous_content = path.read_bytes()
            loaded = json.loads(previous_content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError(
                f"Cannot safely replace unreadable channel credentials in {path}: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise CredentialStoreError(
                f"Cannot safely replace invalid channel credentials in {path}"
            )
        previous = loaded
    staged = stage_protected_secret_tree(
        _CREDENTIAL_NAMESPACE,
        data,
        previous_value=previous,
    )
    cleanup_transaction = None
    try:
        next_text = json.dumps(staged.value, indent=2, ensure_ascii=False) + "\n"
        cleanup_transaction = prepare_stale_secret_cleanup(
            previous,
            staged.value,
            evidence_path=path,
            previous_exists=previous_exists,
            previous_content=previous_content,
            next_exists=True,
            next_content=next_text,
        )
        _write_protected_config(path, next_text)
    except Exception:
        if cleanup_transaction is not None:
            cleanup_transaction.cancel()
        _discard_failed_config_stage(path, staged)
        raise
    if cleanup_transaction is not None:
        cleanup_transaction.commit()


def _write_protected_config(path: Path, next_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        next_text,
        mode=0o600,
    )
