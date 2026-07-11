"""Tests for app.memory.config — workspace memory configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.memory.config import MemoryConfig, get_memory_config


class TestMemoryConfig:
    def test_defaults(self):
        cfg = MemoryConfig()
        assert cfg.enabled is True
        assert cfg.max_lines == 200
        assert cfg.debounce_seconds == 10

    def test_custom_values_within_bounds(self):
        cfg = MemoryConfig(enabled=False, max_lines=50, debounce_seconds=30)
        assert cfg.enabled is False
        assert cfg.max_lines == 50
        assert cfg.debounce_seconds == 30

    def test_max_lines_lower_bound(self):
        assert MemoryConfig(max_lines=10).max_lines == 10
        with pytest.raises(ValidationError):
            MemoryConfig(max_lines=9)

    def test_max_lines_upper_bound(self):
        assert MemoryConfig(max_lines=500).max_lines == 500
        with pytest.raises(ValidationError):
            MemoryConfig(max_lines=501)

    def test_debounce_seconds_lower_bound(self):
        assert MemoryConfig(debounce_seconds=2).debounce_seconds == 2
        with pytest.raises(ValidationError):
            MemoryConfig(debounce_seconds=1)

    def test_debounce_seconds_upper_bound(self):
        assert MemoryConfig(debounce_seconds=120).debounce_seconds == 120
        with pytest.raises(ValidationError):
            MemoryConfig(debounce_seconds=121)


class TestGetMemoryConfig:
    def test_returns_memory_config_instance(self):
        cfg = get_memory_config()
        assert isinstance(cfg, MemoryConfig)

    def test_returns_module_singleton(self):
        assert get_memory_config() is get_memory_config()
