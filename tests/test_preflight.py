from __future__ import annotations

from io import StringIO

from akp03 import preflight


CORRECT = {
    "vendor_id": 0x0300,
    "product_id": 0x3002,
    "usage_page": 0xFFA0,
    "path": b"safe-enumeration-path",
}


def test_no_hid_module_is_reported_without_importing_hardware_adapter() -> None:
    def missing_hid(_name: str) -> object:
        raise ImportError("hid is not installed")

    result = preflight.preflight(import_module=missing_hid)

    assert result.status == "hidapi unavailable"
    assert result.candidates == ()


def test_no_devices_has_no_matching_candidate() -> None:
    assert preflight.preflight_from_enumeration([]).status == "no matching AKP03 vendor interface found"


def test_wrong_vid_or_pid_is_rejected() -> None:
    wrong_vid = {**CORRECT, "vendor_id": 0x9999}
    wrong_pid = {**CORRECT, "product_id": 0x9999}

    result = preflight.preflight_from_enumeration([wrong_vid, wrong_pid])

    assert result.status == "no matching AKP03 vendor interface found"


def test_wrong_usage_page_is_rejected() -> None:
    result = preflight.preflight_from_enumeration([{**CORRECT, "usage_page": 0x0001}])

    assert result.status == "no matching AKP03 vendor interface found"


def test_missing_usage_page_is_rejected_not_assumed() -> None:
    candidate = dict(CORRECT)
    candidate.pop("usage_page")

    assert not preflight.is_akp03_vendor_interface(candidate)
    assert preflight.preflight_from_enumeration([candidate]).status == "no matching AKP03 vendor interface found"


def test_single_correct_candidate_is_ready_and_report_is_explicitly_non_destructive() -> None:
    result = preflight.preflight_from_enumeration([CORRECT])
    output = StringIO()
    preflight.write_report(result, output)

    assert result.status == "exactly one matching candidate found"
    report = output.getvalue()
    assert "no HID device was opened, read, written, configured, or sent any data" in report
    assert "unported and unverified" in report
    assert "never uploads, injects keys, or opens a device" in report


def test_same_path_multiple_usage_collections_are_one_physical_candidate() -> None:
    second_collection = {**CORRECT, "usage": 2}

    result = preflight.preflight_from_enumeration([CORRECT, second_collection])

    assert result.status == "exactly one matching candidate found"
    assert len(result.candidates) == 2


def test_multiple_correct_candidates_are_ambiguous() -> None:
    result = preflight.preflight_from_enumeration([CORRECT, {**CORRECT, "path": b"another-path"}])

    assert result.status == "ambiguous candidates"
    assert len(result.candidates) == 2


def test_enumerator_is_called_but_no_candidate_is_opened() -> None:
    calls = 0

    def enumerate_only() -> list[object]:
        nonlocal calls
        calls += 1
        return [CORRECT]

    result = preflight.preflight(enumerate_devices=enumerate_only)

    assert calls == 1
    assert result.status == "exactly one matching candidate found"
