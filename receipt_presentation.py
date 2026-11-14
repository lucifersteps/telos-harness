"""Remove presentation-only echoes of current saved receipts; no stored state."""
import re


def _line(value):
    value = re.sub(r'^\s*(?:>\s*)?(?:[-+*]\s+|#{1,6}\s+)?', '', value)
    value = value.replace('**', '').replace('__', '').replace('`', '').strip()
    value = re.sub(r'\s*[:：]\s*', '：', value)
    return re.sub(r'\s+', ' ', value)


def _fence(value):
    return bool(re.match(r'^\s*(?:`{3,}|~{3,})', value))


def strip_saved_card_echoes(text, cards, *, protected=(), controls=()):
    """Match the complete saved body, tolerating Markdown wrappers only.

    Do not guess at changed values, partial summaries or other cards. A pending
    approval is kept verbatim even when its fields equal a just-saved object.
    """
    if not cards:
        return text
    protected = [p for p in protected if p]
    if protected:
        pattern = '(' + '|'.join(re.escape(p) for p in protected) + ')'
        return ''.join(part if part in protected else strip_saved_card_echoes(part, cards, controls=controls)
                       for part in re.split(pattern, text))
    for card in cards:
        body = re.search(r'^```text\s*\n(.*?)\n```', card, re.M | re.S)
        if not body:
            continue
        expected = [_line(line) for line in body[1].splitlines() if line.strip()]
        if not expected:
            continue
        title = next((line.split('：', 1)[1] for line in expected if line.startswith('标题：')), '')
        footer = card[body.end():].strip()
        lines = text.splitlines(keepends=True)
        tokens = [(i, _line(line)) for i, line in enumerate(lines)
                  if line.strip() and not _fence(line) and _line(line) != '已保存']
        remove = set()
        for offset in range(len(tokens) - len(expected) + 1):
            group = tokens[offset:offset + len(expected)]
            if [value for _, value in group] != expected:
                continue
            start, end = group[0][0], group[-1][0] + 1

            def before(index):
                index -= 1
                while index >= 0 and not lines[index].strip():
                    index -= 1
                return index

            def after(index):
                while index < len(lines) and not lines[index].strip():
                    index += 1
                return index

            left, right = before(start), after(end)
            # Remove enclosing fences together, never orphan an unrelated block.
            if left >= 0 and right < len(lines) and _fence(lines[left]) and _fence(lines[right]):
                start, end = left, right + 1
            left = before(start)
            if left >= 0 and _line(lines[left]) == '已保存':
                start = left
            right = after(end)
            if right < len(lines) and _line(lines[right]) in {_line(value) for value in (footer, *controls)}:
                end = right + 1
            left = before(start)
            if left >= 0 and title and title in _line(lines[left]) and lines[left].rstrip().endswith(('：', ':')):
                start = left
            remove.update(range(start, end))
        text = ''.join(line for i, line in enumerate(lines) if i not in remove)
    return text
