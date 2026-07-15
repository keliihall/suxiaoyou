"""Release evidence contract for native restricted Office support."""

from __future__ import annotations

import platform

import pytest

from app.tool import office_contract as office_contract_module
from app.tool.office_contract import (
    OFFICE_CONTRACT_VERSION,
    OfficeContractError,
    native_platform_id,
    run_office_contract,
)


@pytest.mark.parametrize(
    ("system_name", "machine_name", "expected"),
    [
        ("Windows", "AMD64", "windows-x64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "x86_64", "linux-x64"),
    ],
)
def test_native_platform_ids_match_release_scorecard(
    system_name: str,
    machine_name: str,
    expected: str,
):
    assert (
        native_platform_id(system_name=system_name, machine_name=machine_name)
        == expected
    )


def test_unknown_native_platform_fails_closed():
    with pytest.raises(OfficeContractError, match="Unsupported native"):
        native_platform_id(system_name="Haiku", machine_name="riscv64")


def test_release_commit_override_wins_over_annotated_tag_object(
    monkeypatch: pytest.MonkeyPatch,
):
    commit = "a" * 40
    monkeypatch.setenv("SUXIAOYOU_RELEASE_COMMIT", commit)
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)

    assert office_contract_module._source_commit() == commit


@pytest.mark.asyncio
async def test_contract_creates_edits_reopens_and_versions_all_formats():
    report = await run_office_contract(
        source_commit="a" * 40,
        release_ref="v1.0.0-rc.7",
    )

    assert report["schema_version"] == 1
    assert report["contract_version"] == OFFICE_CONTRACT_VERSION
    assert report["platform"] == native_platform_id(
        system_name=platform.system(), machine_name=platform.machine()
    )
    assert report["source_commit"] == "a" * 40
    assert report["release_ref"] == "v1.0.0-rc.7"
    assert report["status"] == "ok"
    assert report["all_passed"] is True
    assert report["runner"]["frozen_backend"] is False
    assert set(report["formats"]) == {"docx", "xlsx", "pptx"}
    for result in report["formats"].values():
        assert result["created"] is True
        assert result["edited"] is True
        assert result["reopened_and_validated"] is True
        assert result["independent_reopen_validated"] is True
        assert result["atomic_install"] is True
        assert result["version_snapshot_verified"] is True
        assert result["initial_sha256"] != result["final_sha256"]
        assert result["final_size"] > 0
        assert result["previous_version_id"]


@pytest.mark.asyncio
async def test_expected_platform_mismatch_fails_before_writing():
    actual = native_platform_id()
    other = "windows-x64" if actual != "windows-x64" else "linux-x64"
    with pytest.raises(OfficeContractError, match="expected"):
        await run_office_contract(
            expected_platform=other,
            source_commit="a" * 40,
            release_ref="v1.0.0-rc.7",
        )
