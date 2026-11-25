"""JSON Schema 子集的递归校验器。

不引入 ``jsonschema`` 依赖，与 ``app.observability.prompt.renderer`` 的口径保持一致：
只覆盖工具入参/出参实际需要的关键词。

支持：``type``（含字符串数组与 ``null``）、``required``、``properties``、
``additionalProperties``（bool 或 schema）、``items``、``enum``、
``minLength`` / ``maxLength`` / ``minimum`` / ``maximum`` / ``minItems`` / ``maxItems``。

不支持的关键词（``$ref`` / ``anyOf`` / ``pattern`` 等）会被忽略——工具 schema
由我们自己定义，保持子集即可；遇到无法识别的结构宁可放行也不误杀。
"""

from typing import Any

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "null": (type(None),),
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        # bool 是 int 子类，但 JSON Schema 里 true 不是 integer
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py_types = _TYPE_MAP.get(expected)
    if py_types is None:  # 未知类型名：放行
        return True
    if expected == "boolean":
        return isinstance(value, bool)
    return isinstance(value, py_types)


def validate(value: Any, schema: dict | None, *, path: str = "$") -> list[str]:
    """返回错误信息列表；空列表表示通过。"""
    if not schema:
        return []
    errors: list[str] = []

    expected = schema.get("type")
    if expected is not None:
        allowed = [expected] if isinstance(expected, str) else list(expected)
        if not any(_type_ok(value, t) for t in allowed):
            errors.append(f"{path}: expected type {expected}")
            return errors  # 类型不符时后续约束无意义

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value not in enum")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: less than minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: greater than maximum")

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name}: required")
        properties = schema.get("properties") or {}
        for name, subschema in properties.items():
            if name in value:
                errors.extend(validate(value[name], subschema, path=f"{path}.{name}"))
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name}: additional property not allowed")
        elif isinstance(additional, dict):
            for name, item in value.items():
                if name not in properties:
                    errors.extend(validate(item, additional, path=f"{path}.{name}"))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(validate(item, items, path=f"{path}[{index}]"))

    return errors
