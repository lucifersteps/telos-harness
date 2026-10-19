"""微信图片与后续文字在持久化入站中的分组、重启与隔离。"""

from datetime import datetime, timedelta

import pytest

from telos.adapters.base import AttachmentRef, ConversationRef, InboundTurn
from telos.adapters.inbox import AdapterInboxLedger
from telos.adapters.wechat.adapter import WeChatAdapter
from telos.adapters.wechat.config import WeChatConfig


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 10, 11, 12, 0)}
    monkeypatch.setattr("telos.adapters.inbox.user_now", lambda: clock["now"])
    monkeypatch.setattr("telos.adapters.inbox.user_now_iso", lambda: clock["now"].isoformat())
    path = tmp_path / "wire.jsonl"
    ledger = AdapterInboxLedger(path, agent_id="personal")
    adapter = WeChatAdapter(config=WeChatConfig(image_followup_wait_seconds=30),
                            wall_clock_fn=lambda: clock["now"])

    def record(event_id, *, text="", image=False, user="user-a", group=False):
        turn = InboundTurn(ref=ConversationRef(platform="wechat", chat_id="room" if group else user,
            user_id=user, source_id="group:room" if group else f"dm:{user}"),
            text=text, raw_event_id=event_id, is_group=group,
            attachments=[AttachmentRef(kind="image", uri=f"{event_id}.png")] if image else [])
        return ledger.record_received_turn(adapter="wechat", account_id="bot", turn=turn)

    def claim(current_ledger=None):
        return (current_ledger or ledger).claim_available(adapter="wechat", owner="daemon",
                                                          group_available=adapter.group_inbox_items)

    return clock, ledger, record, claim, path


def test_images_wait_across_restart_then_merge_and_complete_all_sources(inbox):
    clock, ledger, record, claim, path = inbox
    first = record("image-1", image=True)
    assert claim() == []
    assert ledger.pending_count() == 1
    clock["now"] += timedelta(seconds=5)
    second = record("image-2", image=True)
    assert claim() == []
    clock["now"] += timedelta(seconds=5)
    last = record("instruction", text="把图片里的约定加入日历")
    restarted = AdapterInboxLedger(path, agent_id="personal")
    [batch] = claim(restarted)
    turn = batch.claimed_turn()
    assert turn.text == "把图片里的约定加入日历"
    assert [a.uri for a in turn.attachments] == ["image-1.png", "image-2.png"]
    assert turn.metadata["adapter_inbox_keys"] == [first.dedupe_key, second.dedupe_key, last.dedupe_key]
    assert claim(restarted) == []
    for key, claim_id in turn.metadata["adapter_inbox_claims"].items():
        restarted.complete(dedupe_key=key, claim_id=claim_id, turn_id="turn_image")
    assert restarted.pending_count() == 0
    record("image-1", image=True)
    record("instruction", text="把图片里的约定加入日历")
    assert claim(restarted) == []


def test_unfinished_batch_is_reconstructed_after_claim_release(inbox):
    _, ledger, record, claim, _ = inbox
    record("image", image=True)
    record("text", text="加入日历")
    [original] = claim()
    assert ledger.release_incomplete_claims(reason="restart") == 2
    [recovered] = claim()
    assert recovered.turn.text == original.turn.text
    assert recovered.turn.attachments == original.turn.attachments
    assert recovered.claim_id != original.claim_id


def test_image_only_flushes_after_window_and_does_not_absorb_late_text(inbox):
    clock, _, record, claim, _ = inbox
    record("image", image=True)
    clock["now"] += timedelta(seconds=31)
    record("text", text="另外一个任务")
    image, text = claim()
    assert image.turn.text == ""
    assert len(image.turn.attachments) == 1
    assert text.turn.text == "另外一个任务"
    assert not text.turn.attachments


@pytest.mark.parametrize("group", [False, True])
def test_waiting_image_does_not_absorb_another_senders_text(inbox, group):
    _, _, record, claim, _ = inbox
    record("image", image=True, group=group)
    record("other", text="其他用户的指令", user="user-b", group=group)
    [other] = claim()
    assert other.turn.ref.user_id == "user-b"
    assert not other.turn.attachments
    record("own", text="我自己的图片说明", group=group)
    [own] = claim()
    assert own.turn.ref.user_id == "user-a"
    assert len(own.turn.attachments) == 1


def test_commands_and_plain_text_remain_separate(inbox):
    _, _, record, claim, _ = inbox
    record("image", image=True)
    record("stop", text="/stop")
    record("text-1", text="第一条")
    record("text-2", text="第二条")
    batches = claim()
    assert [b.turn.text for b in batches] == ["", "/stop", "第一条", "第二条"]
    assert all(not b.turn.attachments for b in batches[1:])


def test_image_only_flushes_without_followup(inbox):
    clock, _, record, claim, _ = inbox
    record("image", image=True)
    assert claim() == []
    clock["now"] += timedelta(seconds=30)
    [image] = claim()
    assert image.turn.text == ""
    assert len(image.turn.attachments) == 1
