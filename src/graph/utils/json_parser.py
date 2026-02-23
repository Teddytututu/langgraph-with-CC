"""src/graph/utils/json_parser.py — 健壮的 JSON 提取工具

修复正则贪婪匹配问题：使用括号计数法代替 r'{.*}' 贪婪匹配，
确保多 JSON 对象场景下只提取第一个有效对象。
"""
from __future__ import annotations

import json
import re


def extract_first_json_object(text: str) -> dict | None:
    """
    从任意文本中提取第一个有效的 JSON 对象（花括号包裹）。

    使用括号计数法而非正则贪婪匹配，正确处理：
    - LLM 返回多个 JSON 对象
    - 嵌套 JSON 结构
    - 代码块包裹（```json ... ```）

    Returns:
        解析成功的 dict，或 None（未找到有效 JSON）
    """
    if not text:
        return None

    # 先尝试去除 markdown 代码块
    cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r'```\s*$', '', cleaned.strip(), flags=re.MULTILINE)

    # 括号计数法：找到第一个完整的 {} 对象
    brace_count = 0
    start: int | None = None
    for i, ch in enumerate(cleaned):
        if ch == '{':
            if start is None:
                start = i
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0 and start is not None:
                candidate = cleaned[start: i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # 该段无效，继续查找下一个
                    start = None
                    continue
    return None


def extract_first_json_array(text: str) -> list | None:
    """
    从任意文本中提取第一个有效的 JSON 数组（方括号包裹）。

    Returns:
        解析成功的 list，或 None
    """
    if not text:
        return None

    cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r'```\s*$', '', cleaned.strip(), flags=re.MULTILINE)

    bracket_count = 0
    start: int | None = None
    for i, ch in enumerate(cleaned):
        if ch == '[':
            if start is None:
                start = i
            bracket_count += 1
        elif ch == ']':
            bracket_count -= 1
            if bracket_count == 0 and start is not None:
                candidate = cleaned[start: i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    start = None
                    continue
    return None


def safe_parse_json(text: str) -> dict | list | None:
    """
    通用 JSON 解析：先尝试直接 json.loads，再尝试对象/数组提取。

    Returns:
        解析结果（dict / list），或 None
    """
    if not text:
        return None

    # 去除 markdown 包装后尝试整体解析
    cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r'```\s*$', '', cleaned.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 尝试提取对象
    obj = extract_first_json_object(text)
    if obj is not None:
        return obj

    # 尝试提取数组
    return extract_first_json_array(text)
