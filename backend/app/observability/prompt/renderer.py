"""Prompt 渲染：JSON Schema 子集校验 + Jinja2 StrictUndefined。

- 校验只覆盖 `required` / `properties.type` / `properties.enum`，不引入完整
  jsonschema 依赖。
- 模板变量缺失时 Jinja2 StrictUndefined 直接报错，不会渲染成空字符串。
- prompt_hash 对最终 messages 做规范化 JSON 后取 SHA-256，用于稳定比对。
"""

import hashlib
import json
from dataclasses import dataclass, field

from jinja2 import Environment, StrictUndefined

_env = Environment(undefined=StrictUndefined, autoescape=False)


@dataclass
class RenderResult:
    status: str            # ok | schema_error | render_error
    messages: list[dict] = field(default_factory=list)
    prompt_hash: str = ""


def _validate(variables: dict, schema: dict) -> list[str]:
    errors: list[str] = []
    for name in schema.get("required", []):
        if name not in variables:
            errors.append(f"missing required variable: {name}")
    for name, spec in (schema.get("properties") or {}).items():
        if name in variables:
            expected = spec.get("type")
            if expected and not _type_ok(variables[name], expected):
                errors.append(f"{name} expected {expected}")
        if "enum" in spec and name in variables and variables[name] not in spec["enum"]:
            errors.append(f"{name} not in enum")
    return errors


def _type_ok(value, expected: str) -> bool:
    mapping = {"string": str, "integer": int, "number": (int, float),
               "boolean": bool, "object": dict, "array": list}
    py = mapping.get(expected)
    return True if py is None else isinstance(value, py)


def _hash(messages: list[dict]) -> str:
    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render(template, variables: dict) -> RenderResult:
    schema = template.variables_schema or {"type": "object"}
    if _validate(variables, schema):
        return RenderResult(status="schema_error")
    try:
        messages = [
            {"role": "system", "content": _env.from_string(template.system_prompt or "")
             .render(**variables)},
            {"role": "user", "content": _env.from_string(template.user_prompt_template or "")
             .render(**variables)},
        ]
    except Exception:
        return RenderResult(status="render_error")
    return RenderResult(status="ok", messages=messages, prompt_hash=_hash(messages))
