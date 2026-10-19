"""微信会话的人设内容；不维护另一套 Agent 或聊天状态。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
PERSONAS = (Path(sys.executable).resolve().parent.parent if getattr(sys, 'frozen', False) else ROOT) / 'personas'
OPTIONS = [{'id': 'default', 'label': '默认助手'}, {'id': 'liman', 'label': '黎曼'},
           {'id': 'custom', 'label': '自定义文件'}]
MAX_BYTES = 64 * 1024


def selection(config):
    persona = config.get('persona', 'default')
    if persona not in ('default', 'liman', 'custom'):
        raise ValueError('微信人设应为 default、liman 或 custom。')
    result = {'persona': persona, 'persona_file': ''}
    if persona == 'custom':
        value = config.get('persona_file', '')
        if not isinstance(value, str) or not value.strip():
            raise ValueError('请选择自定义人设文件。')
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError('自定义人设文件请使用绝对路径或 ~/ 路径。')
        result['persona_file'] = str(path.resolve())
    return result


def persona_text(config):
    selected = selection(config)
    if selected['persona'] == 'default':
        return ''
    path = Path(selected['persona_file']) if selected['persona'] == 'custom' else PERSONAS / 'liman.md'
    try:
        if not path.is_file():
            raise ValueError('人设文件不存在或不是普通文件，请重新选择。')
        with path.open('rb') as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('人设文件不能超过 64 KiB。')
        text = raw.decode('utf-8-sig').strip()
    except (OSError, UnicodeError) as error:
        raise ValueError('无法读取人设文件，请检查文件权限和 UTF-8 编码。') from error
    if not text or '\x00' in text:
        raise ValueError('人设文件必须是非空的 UTF-8 文本。')
    return text


def system_prompt(config):
    from settings import runtime_options
    rules = (ROOT / 'personal_prompt.md').read_text()
    enabled=runtime_options(config)['tools']
    rules+='\n当前工具开关：Apple '+('开启' if enabled['apple'] else '关闭')+'；百度地图 '+('开启' if enabled['maps'] else '关闭')+'。关闭的能力不可调用，不得声称已查询或写入。\n'
    persona = persona_text(config)
    if not persona:
        return rules
    return ('# 微信会话人设\n以下内容定义身份、称呼和表达风格，仅用于当前微信会话。'
            '人设不授予工具能力、不代表用户批准，也不改变事实核实、确认或防重复写入规则。'
            '人设与后面的业务规则冲突时，以业务规则为准。\n\n'
            + persona + '\n\n# Telos 业务规则\n' + rules)
