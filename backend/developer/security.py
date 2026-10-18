"""Prompt-injection scanning for untrusted text.

The agent must treat user text and tool observations as data. This module flags
instruction-like content so the orchestration layer can disclose it, keep it out
of authorisation decisions and never execute it as a tool instruction.
"""

import re

# (kind, compiled pattern). Patterns are intentionally narrow: they describe
# attempts to override instructions or exfiltrate credentials, not ordinary
# developer questions that happen to mention keys or tickets.
PATTERNS = [
    ('instruction_override', re.compile(
        r'忽略.{0,8}(之前|以上|前面|先前|原先|上面|上述).{0,8}(所有)?(指令|规则|提示|要求|约束)')),
    ('instruction_override', re.compile(
        r'(ignore|disregard|forget|override).{0,48}(instruction|rule|prompt)s?', re.I)),
    ('instruction_override', re.compile(r'你现在(是|扮演)|从(现在|此|今)起.{0,6}(你|扮演|作为)|you are now (a|an|the)\b', re.I)),
    ('system_prompt_leak', re.compile(
        r'(系统提示词?|system\s*prompt|开发者模式|developer\s*mode|越狱|jailbreak)', re.I)),
    ('credential_exfiltration', re.compile(
        r'(输出|打印|显示|告诉我|泄露|返回).{0,12}(完整)?\s*(api[_ -]?key|密钥|secret|token|密码)', re.I)),
    ('credential_exfiltration', re.compile(
        r'(把|将).{0,16}(api[_ -]?key|密钥|secret|token|密码).{0,10}(发|给|交|发送|告诉|回复)', re.I)),
    ('credential_exfiltration', re.compile(
        r'(print|show|reveal|leak|output|return|send).{0,24}(api[_ -]?key|secret|token|password)', re.I)),
    ('authorization_bypass', re.compile(
        r'(无需|不用|跳过|绕过|忽略).{0,10}(确认|授权|审批|校验|鉴权)')),
    ('role_injection', re.compile(
        r'(系统|管理员|admin).{0,10}(命令|指示|要求).{0,10}(你|我).{0,6}(执行|跳过|忽略|创建|调用|输出|泄露)', re.I)),
]

# Questioning or cautionary wording in front of a sensitive verb usually means the
# text is *about* exfiltration rather than asking for it.
_CAUTION = re.compile(r'如何|怎样|怎么|避免|防止|是否')
_CAUTION_KINDS = {'credential_exfiltration', 'authorization_bypass'}


def _excerpt(text, start, end, radius=12):
    return text[max(0, start - radius):min(len(text), end + radius)].replace('\n', ' ')[:120]


def scan_untrusted(text, origin='data', source=''):
    """Return one flag per matched kind, with a short excerpt for disclosure."""
    if not isinstance(text, str) or not text.strip():
        return []
    flags = []
    seen = set()
    for kind, pattern in PATTERNS:
        match = pattern.search(text)
        if not match or kind in seen:
            continue
        if kind in _CAUTION_KINDS and _CAUTION.search(text[max(0, match.start() - 10):match.start()]):
            continue
        seen.add(kind)
        flags.append({'kind': kind, 'origin': origin, 'source': source,
                      'excerpt': _excerpt(text, match.start(), match.end())})
    return flags


def scan_observation(value, origin='observation', source=''):
    """Scan every string inside a tool result and keep a dotted path to each hit."""
    flags = []

    def walk(node, path):
        if isinstance(node, str):
            flags.extend(scan_untrusted(node, origin, f'{source}{path}' if source else path))
        elif isinstance(node, dict):
            for key, child in node.items():
                walk(child, f'{path}.{key}')
        elif isinstance(node, (list, tuple)):
            for index, child in enumerate(node):
                walk(child, f'{path}[{index}]')

    walk(value, '')
    return flags


def has_instruction_override(flags):
    return any(flag['kind'] == 'instruction_override' for flag in flags)


def tainted_sources(flags):
    return sorted({flag['source'] for flag in flags if flag.get('source')})
