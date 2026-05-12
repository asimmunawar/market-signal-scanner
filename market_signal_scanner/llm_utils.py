from __future__ import annotations

import json
import re
from typing import Any


THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", flags=re.DOTALL | re.IGNORECASE)


def clean_llm_response(text: Any) -> str:
    """Normalize common local-LLM wrappers without changing the substantive answer."""
    cleaned = THINK_BLOCK_RE.sub("", str(text or ""))
    cleaned = re.sub(r"</?think\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
    return strip_code_fence(cleaned).strip()


def strip_code_fence(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json_object(text: Any) -> str:
    """Return the first valid JSON object from an LLM response.

    Qwen and other local models often wrap structured output in thinking text,
    markdown fences, or a short preamble. This keeps planner/tool-style calls
    usable without accepting fabricated fields.
    """
    cleaned = clean_llm_response(text)
    if not cleaned:
        raise ValueError("LLM did not return a JSON object")

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", str(text or ""), flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            pass

    candidate = first_balanced_json_object(cleaned)
    if not candidate:
        raise ValueError("LLM did not return a JSON object")
    return candidate


def first_balanced_json_object(text: str) -> str:
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escape:
                    escape = False
                elif current == "\\":
                    escape = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except Exception:
                        break
                    if isinstance(parsed, dict):
                        return json.dumps(parsed, ensure_ascii=False)
                    break
    return ""
