"""Channels API — manage in-process messaging platform channels.

Replaces the old OpenClaw-based system with nanobot's native channel
architecture running directly inside 苏小有 (no external Node.js process).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.channels.registry import CHINA_READY_CHANNELS
from app.channels.config import load_channels_config_dict, save_channels_config_dict
from app.auth.local import require_local_session
from app.release_features import MESSAGING_CHANNELS_RELEASED
from app.i18n import localize, request_language

logger = logging.getLogger(__name__)

def _require_channels_release() -> None:
    if not MESSAGING_CHANNELS_RELEASED:
        raise HTTPException(status_code=404, detail="Messaging channels are not available in this release")


router = APIRouter(
    dependencies=[Depends(_require_channels_release), Depends(require_local_session)]
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ChannelSystemStatus(BaseModel):
    """Status of the in-process channel system."""
    running: bool
    channels: dict[str, Any]


class ChannelAddRequest(BaseModel):
    channel: str  # feishu, weixin, wecom, dingtalk, qq
    # Common fields
    allow_from: list[str] | None = None  # ["*"] for allow all
    # Token-based fields (varies by channel)
    token: str | None = None       # discord, telegram
    bot_token: str | None = None   # slack (xoxb-...)
    app_token: str | None = None   # slack (xapp-...)
    app_id: str | None = None      # feishu
    app_secret: str | None = None  # feishu
    # WeChat fields
    api_url: str | None = None     # weixin HTTP API URL
    # General
    streaming: bool = False
    extra: dict[str, Any] | None = None  # Pass-through for any channel-specific config


class ChannelRemoveRequest(BaseModel):
    channel: str


class ChannelLoginRequest(BaseModel):
    channel: str = "weixin"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_channel_manager(request: Request):
    """Get the ChannelManager from app state."""
    return getattr(request.app.state, "channel_manager", None)


def _get_channels_config_path() -> Path:
    return Path("data/channels.json")


def _load_config_dict() -> dict:
    """Load raw channels.json config."""
    return load_channels_config_dict(_get_channels_config_path())


def _save_config_dict(data: dict) -> None:
    """Save raw channels.json config."""
    save_channels_config_dict(data, _get_channels_config_path())


# ---------------------------------------------------------------------------
# Channel System Status
# ---------------------------------------------------------------------------

@router.get("/channels/status")
async def channels_status(request: Request) -> ChannelSystemStatus:
    """Get status of the in-process channel system."""
    mgr = _get_channel_manager(request)
    if mgr is None:
        return ChannelSystemStatus(running=False, channels={})

    return ChannelSystemStatus(
        running=bool(mgr.enabled_channels),
        channels=mgr.get_status(),
    )


# Backward-compat: old frontend polls /channels/openclaw/status
@router.get("/channels/openclaw/status")
async def openclaw_status_compat(request: Request) -> dict:
    """Backward-compatible status endpoint.

    Reports the new channel system as 'installed' and 'running'
    so the old frontend can work during the transition.
    """
    mgr = _get_channel_manager(request)
    running = mgr is not None and bool(mgr.enabled_channels)
    return {
        "installed": True,
        "running": running,
        "port": None,
        "ws_url": None,
    }


# Backward-compat stubs so old frontend doesn't error
@router.post("/channels/openclaw/setup")
async def openclaw_setup_compat(request: Request) -> dict:
    return {"status": "ready", "message": "Channel system is built-in (no setup needed)"}


@router.post("/channels/openclaw/start")
async def openclaw_start_compat(request: Request) -> dict:
    return {"status": "running", "message": "Channel system is always running"}


@router.post("/channels/openclaw/stop")
async def openclaw_stop_compat(request: Request) -> dict:
    return {"status": "stopped"}


@router.delete("/channels/openclaw/uninstall")
async def openclaw_uninstall_compat(request: Request) -> dict:
    return {"status": "not_applicable", "message": "Channel system is built-in"}


# ---------------------------------------------------------------------------
# Channel CRUD
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(request: Request) -> dict:
    """List all configured channels and their status."""
    mgr = _get_channel_manager(request)
    running_channels = mgr.get_status() if mgr else {}

    # Also include configured-but-not-running channels from config
    config = _load_config_dict()
    all_channels: dict[str, Any] = {}

    for name, ch_config in config.get("channels", {}).items():
        if name not in CHINA_READY_CHANNELS:
            continue
        enabled = ch_config.get("enabled", False)
        is_running = name in running_channels and running_channels[name].get("running", False)
        all_channels[name] = {
            "id": name,
            "name": name.capitalize(),
            "status": "running" if is_running else ("configured" if enabled else "disabled"),
            "type": name,
        }

    return {
        "channels": all_channels,
        "gateway_running": bool(running_channels),
    }


@router.post("/channels/add")
async def add_channel(request: Request, body: ChannelAddRequest) -> dict:
    """Add and enable a messaging channel.

    Saves config and starts the channel immediately if possible.
    """
    config = _load_config_dict()
    channels = config.setdefault("channels", {})

    if body.channel not in CHINA_READY_CHANNELS:
        raise HTTPException(400, localize(request_language(request), "暂不支持该消息渠道", "This messaging channel is not supported"))

    # Build channel config
    ch_config: dict[str, Any] = {
        "enabled": True,
        "allow_from": body.allow_from or ["*"],
    }

    if body.channel == "feishu":
        if not body.app_id or not body.app_secret:
            raise HTTPException(400, localize(request_language(request), "飞书需要应用 ID 和应用密钥", "Feishu requires an App ID and App Secret"))
        ch_config["app_id"] = body.app_id
        ch_config["app_secret"] = body.app_secret

    elif body.channel == "weixin":
        ch_config["api_url"] = body.api_url or "http://localhost:9503"

    elif body.channel in ("wecom", "dingtalk", "qq"):
        # Accept generic extra config
        if body.extra:
            ch_config.update(body.extra)
    else:
        raise HTTPException(400, f"Unknown channel: {body.channel}")

    if body.streaming:
        ch_config["streaming"] = True

    # Merge with existing config (don't overwrite fields not provided)
    existing = channels.get(body.channel, {})
    existing.update(ch_config)
    channels[body.channel] = existing

    _save_config_dict(config)

    # Try to start the channel immediately
    mgr = _get_channel_manager(request)
    if mgr:
        try:
            from app.channels.registry import load_channel_class
            cls = load_channel_class(body.channel)
            channel_instance = cls(existing, mgr.bus)
            mgr.add_channel(body.channel, channel_instance)

            import asyncio
            asyncio.create_task(channel_instance.start())

            logger.info("Channel %s added and started", body.channel)
        except Exception as e:
            logger.warning("Channel %s configured but failed to start: %s", body.channel, e)
            return {"ok": True, "message": f"{body.channel} configured (will start on restart): {e}"}

    return {"ok": True, "message": f"{body.channel} channel added and started"}


@router.post("/channels/login")
async def login_channel(request: Request, body: ChannelLoginRequest):
    """Start interactive login for a released channel.

    Unreleased channel names are rejected before any channel manager or
    channel-specific login code is consulted. In particular, the unfinished
    WhatsApp bridge must remain unreachable in v0.8.0.
    """
    if body.channel not in CHINA_READY_CHANNELS:
        raise HTTPException(400, localize(request_language(request), "暂不支持该消息渠道", "This messaging channel is not supported"))

    mgr = _get_channel_manager(request)
    if mgr is None:
        raise HTTPException(503, "Channel manager not initialized")

    # Released channels use their in-process login implementation.
    channel = mgr.get_channel(body.channel)
    if channel is None:
        raise HTTPException(404, f"Channel {body.channel} not configured")

    try:
        result = await channel.login(force=True)
        return {"ok": result, "message": "Login completed" if result else "Login failed"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@router.post("/channels/remove")
async def remove_channel(request: Request, body: ChannelRemoveRequest) -> dict:
    """Remove a channel — stops it and removes from config."""
    # Stop the running channel
    mgr = _get_channel_manager(request)
    if mgr:
        channel = mgr.get_channel(body.channel)
        if channel:
            try:
                await channel.stop()
            except Exception as e:
                logger.warning("Error stopping %s: %s", body.channel, e)
        mgr.remove_channel(body.channel)

    # Remove from config
    config = _load_config_dict()
    channels = config.get("channels", {})
    if body.channel in channels:
        del channels[body.channel]
        _save_config_dict(config)

    return {"ok": True, "message": f"{body.channel} removed"}
