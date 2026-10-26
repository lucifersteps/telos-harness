import pytest

from telos.adapters.delivery import split_message_bubbles, split_reply_bubbles


@pytest.mark.parametrize("marker", ["-", "*", "+", "1.", "1)", "1、"])
def test_reply_keeps_list_heading_items_and_indented_details_together(marker):
    listing = f"**答辩笔记**\n{marker} 研究演讲\n  说明：包含演讲结构。\n  - 子项目\n\n{marker} 金字塔原理"
    assert split_reply_bubbles("已找到。\n\n" + listing + "\n\n这是全部相关笔记。") == [
        "已找到。", listing, "这是全部相关笔记。"
    ]


def test_reply_keeps_separate_lists_and_plain_chat():
    notes = "**笔记（2 篇）**\n- 如何做一次良好的研究演讲.md\n- 在PPT演示文稿中呈现金字塔.md"
    references = "参考资料：\n\n- giving-a-talk-slides.pdf\n- 金字塔原理.pdf"
    assert split_reply_bubbles("这次读到了。\n\n" + notes + "\n\n" + references + "\n其他补充。\n下一句话。") == [
        "这次读到了。", notes, references, "其他补充。", "下一句话。"
    ]


def test_reply_preserves_list_indentation_and_explicit_bubble_boundaries():
    assert split_reply_bubbles("- 第一项\n  1. 子项\n---\n- 第二项") == [
        "- 第一项\n  1. 子项", "- 第二项"
    ]


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_reply_keeps_fenced_content_including_blank_lines_and_separators(fence):
    block = f"{fence}markdown\n- 第一项\n\n---\n- 第二项\n{fence}"
    assert split_reply_bubbles("示例\n" + block + "\n结束。") == ["示例", block, "结束。"]


def test_reply_preserves_preview_between_lists():
    listing = "待办：\n- 写论文\n- 准备答辩"
    preview = "操作预览\n\n- 原文\n---\n回复 1 确认"
    assert split_reply_bubbles(listing + "\n" + preview + "\n" + listing, protected_text=preview) == [
        listing, preview, listing
    ]


def test_delivery_splits_on_standalone_separator() -> None:
    text = "行，那赛后集锦是你摸鱼的罪证了是吧。\n---\n这几场打得怎么样，有没有值得回味的回合。"

    assert split_message_bubbles(text) == [
        "行，那赛后集锦是你摸鱼的罪证了是吧。",
        "这几场打得怎么样，有没有值得回味的回合。",
    ]


def test_delivery_tolerates_blank_lines_and_longer_rules() -> None:
    assert split_message_bubbles("a段\n\n---\n\nb段") == ["a段", "b段"]
    assert split_message_bubbles("x\n----\ny") == ["x", "y"]


def test_delivery_keeps_single_message_without_separator() -> None:
    assert split_message_bubbles("就一句话，没有横线。") == ["就一句话，没有横线。"]


def test_delivery_splits_three_bubbles() -> None:
    assert split_message_bubbles("一\n---\n二\n---\n三") == ["一", "二", "三"]


def test_delivery_does_not_split_inline_dashes() -> None:
    assert split_message_bubbles("范围 a---b 之间") == ["范围 a---b 之间"]


def test_delivery_pure_separator_falls_back_to_original_text() -> None:
    assert split_message_bubbles("---") == ["---"]


def test_delivery_empty_text_yields_no_message() -> None:
    assert split_message_bubbles("") == []
    assert split_message_bubbles("   \n  ") == []


def test_delivery_caps_bubble_count() -> None:
    text = "\n---\n".join(str(i) for i in range(20))

    assert len(split_message_bubbles(text, max_bubbles=6)) == 6


def test_delivery_keeps_blank_lines_in_one_message() -> None:
    text = (
        "啊，这首歌……真是王炸级别的催泪弹。\n\n"
        "那种“现在的你，有没有让十五岁的自己失望”的叩问，太狠了。\n\n"
        "是哪一句戳到你了？"
    )

    assert split_message_bubbles(text) == [text]


def test_delivery_single_newline_stays_in_one_bubble() -> None:
    assert split_message_bubbles("待办：\n1. 写论文\n2. 跑步") == [
        "待办：\n1. 写论文\n2. 跑步"
    ]


def test_delivery_blank_lines_and_separator_mix() -> None:
    assert split_message_bubbles("a\n\nb\n---\nc") == ["a\n\nb", "c"]


def test_delivery_code_block_is_not_fragmented_by_blank_lines() -> None:
    text = "看这段：\n\n```python\nx = 1\n\ny = 2\n```"

    assert split_message_bubbles(text) == [text]


def test_delivery_code_block_is_not_fragmented_by_separator_lines() -> None:
    text = "看这段：\n\n```markdown\n---\ntitle: demo\n---\n```"

    assert split_message_bubbles(text) == [text]


def test_delivery_still_splits_separator_outside_code_block() -> None:
    text = "先看代码：\n\n```python\nx = 1\n```\n---\n再看结论。"

    assert split_message_bubbles(text) == [
        "先看代码：\n\n```python\nx = 1\n```",
        "再看结论。",
    ]
