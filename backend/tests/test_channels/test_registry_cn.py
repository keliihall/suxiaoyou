from app.channels.registry import CHINA_READY_CHANNELS


def test_builtin_channel_registry_only_exposes_china_ready_channels():
    assert CHINA_READY_CHANNELS == frozenset()
