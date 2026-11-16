"""旗标未知不能阻断日历，也不能产生不完整的旗标筛选结果。"""
import pytest

from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.direct import inverse_fields
from business_tests.test_apple_service import service


@pytest.mark.parametrize("flag", [True, False])
def test_unknown_flags_reject_filtered_results(tmp_path, flag):
    apple, native = service(tmp_path)
    native.items[0].update(flagged=None, flagged_supported=False)
    with pytest.raises(AppleNativeError, match="无法读取旗标"):
        apple.query({"kind": "reminder", "flagged": flag})
    row = apple.query({"kind": "reminder"})["items"][0]
    assert row["flagged"] is None and row["flagged_supported"] is False
    assert inverse_fields("update", "reminder", {"flagged": True}, row) is None


def test_unknown_flag_does_not_block_calendar_query_or_write(tmp_path):
    apple, native = service(tmp_path)
    native.items[0].update(flagged=None, flagged_supported=False)
    event = {"id": "event-001", "kind": "event", "calendar_id": "calendar-001",
             "title": "会议", "start": "2026-11-16T10:00:00+08:00",
             "end": "2026-11-16T11:00:00+08:00"}
    native.items.append(event)
    row = apple.query({"kind": "event"})["items"][0]
    result = apple.mutate("update", {"kind": "event", "id": row["id"],
                         "expected_revision": row["revision"],
                         "fields": {"title": "更新会议"}}, "calendar-with-unknown-flag")
    assert result["saved"] and result["item"]["title"] == "更新会议"
    assert native.items[0]["flagged"] is None
    assert len(native.writes) == 1


def test_known_false_and_out_of_scope_unknown_flags(tmp_path):
    apple, native = service(tmp_path)
    native.items[0].update(flagged=False)
    native.items.append({**native.items[0], "id": "unknown", "calendar_id": "other-list",
                         "flagged": None, "completed": True})
    assert apple.query({"flagged": False})["total"] == 1
    assert apple.query({"flagged": True})["total"] == 0
    assert apple.query({"flagged": False, "include_completed": True,
                        "calendar_ids": ["list-001"]})["total"] == 1
    with pytest.raises(AppleNativeError, match="完整筛选结果"):
        apple.query({"flagged": False, "include_completed": True})


def test_ordinary_reminder_edit_preserves_unknown_flag(tmp_path):
    apple, native = service(tmp_path)
    native.items[0].update(flagged=None, flagged_supported=False)
    row = apple.query({})["items"][0]
    result = apple.mutate("update", {"kind": "reminder", "id": row["id"],
                         "expected_revision": row["revision"],
                         "fields": {"title": "买水果"}}, "ordinary-edit")
    assert result["item"]["flagged"] is None
    assert native.writes[0][1]["fields"] == {"title": "买水果"}
