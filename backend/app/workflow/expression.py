"""受限表达式：只允许字段访问、比较与布尔运算。

语法示例::

    $input.question
    $input.a.b
    $plan.output.high_risk_count > 0
    ($input.score >= 0.8 and not $input.flagged) or $input.level == "high"

实现要点：

- 不使用 ``eval``；先做 ``$ident`` 预处理，再用 ``ast.parse(mode="eval")``
  解析，只放行白名单节点；
- ``$input`` 与 ``$<node_id>.output`` 是唯一合法的上下文根；
- 缺失路径、类型不匹配、非布尔条件都抛 ``ExpressionError``，不会静默返回 None。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.workflow.dsl import NODE_ID_MAX_LENGTH, NODE_ID_RE

MAX_SOURCE_LENGTH = 2000
MAX_AST_NODES = 200

_CTX_ROOT = "__ctx__"
_CTX_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_LITERAL_NAMES = frozenset({"true", "false", "null"})
_SCALAR_TYPES = (str, int, float, bool, type(None))


class ExpressionError(ValueError):
    """表达式的语法、白名单或求值错误；``message`` 可直接返回给 API。"""

    def __init__(self, message: str, *, source: str | None = None) -> None:
        self.message = message
        self.source = source
        super().__init__(message)


@dataclass(frozen=True)
class CompiledExpression:
    """已通过白名单校验的表达式，可重复对多个上下文求值。

    ``referenced_nodes`` 记录表达式读取的节点 id（``$input`` 不计入），
    供校验器检查引用是否存在、是否位于当前节点上游。
    """

    source: str
    _tree: ast.AST
    referenced_nodes: tuple[str, ...] = ()

    def evaluate(self, context: dict[str, Any]) -> Any:
        if not isinstance(context, dict):
            raise ExpressionError("表达式上下文必须是 dict", source=self.source)
        return _evaluate_node(self._tree, context)


@lru_cache(maxsize=512)
def compile_expression(source: str) -> CompiledExpression:
    """编译并缓存一个受限表达式；非法语法或越界结构立即报错。"""
    if not isinstance(source, str):
        raise ExpressionError("表达式必须是字符串")
    if not source.strip():
        raise ExpressionError("表达式不能为空")
    if len(source) > MAX_SOURCE_LENGTH:
        raise ExpressionError(
            f"表达式长度不能超过 {MAX_SOURCE_LENGTH} 个字符", source=source
        )

    preprocessed = _preprocess(source)
    try:
        parsed = ast.parse(preprocessed, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(
            f"表达式语法错误: {exc.msg}", source=source
        ) from None

    body = parsed.body
    if sum(1 for _ in ast.walk(body)) > MAX_AST_NODES:
        raise ExpressionError(
            f"表达式 AST 节点数不能超过 {MAX_AST_NODES}", source=source
        )

    _validate_ast(body, source)
    return CompiledExpression(
        source=source,
        _tree=body,
        referenced_nodes=_collect_referenced_nodes(body),
    )


def evaluate_expression(source: str, context: dict[str, Any]) -> Any:
    """编译（命中缓存）并求值；缺失路径或类型错误抛 ``ExpressionError``。"""
    return compile_expression(source).evaluate(context)


def evaluate_condition(source: str, context: dict[str, Any]) -> bool:
    """条件表达式求值，结果必须严格为 ``bool``。"""
    result = evaluate_expression(source, context)
    if type(result) is not bool:
        raise ExpressionError(
            f"条件表达式必须返回布尔值，实际得到 {type(result).__name__}",
            source=source,
        )
    return result


# --------------------------------------------------------------------- 预处理


def _preprocess(source: str) -> str:
    """把字符串字面量之外的 ``$ident`` 改写为 ``__ctx__.ident``。

    字符串内的 ``$`` 属于普通数据；字符串之外的任何 ``$`` 都必须紧跟合法
    标识符，否则视为残留/非法引用直接报错。
    """
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(source):
        char = source[index]
        if quote is not None:
            out.append(char)
            if char == "\\" and index + 1 < len(source):
                out.append(source[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue

        if char in ("'", '"'):
            quote = char
            out.append(char)
            index += 1
            continue

        if char == "$":
            match = _CTX_REF_RE.match(source, index)
            if match is None:
                raise ExpressionError(
                    "非法的 '$' 引用；请使用 $input 或 $节点id.output",
                    source=source,
                )
            out.append(f"{_CTX_ROOT}.{match.group(1)}")
            index = match.end()
            continue

        out.append(char)
        index += 1

    return "".join(out)


# --------------------------------------------------------------- AST 白名单


def _validate_ast(node: ast.AST, source: str) -> None:
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ExpressionError("只允许 and / or 布尔运算", source=source)
        for value in node.values:
            _validate_ast(value, source)
        return

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            raise ExpressionError(
                "只允许 not、负号或正号一元运算", source=source
            )
        _validate_ast(node.operand, source)
        return

    if isinstance(node, ast.Compare):
        for operator in node.ops:
            if not isinstance(
                operator, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
            ):
                raise ExpressionError(
                    "只允许 == != > >= < <= 比较", source=source
                )
        _validate_ast(node.left, source)
        for comparator in node.comparators:
            _validate_ast(comparator, source)
        return

    if isinstance(node, ast.Attribute):
        _attribute_parts(node, source)
        return

    if isinstance(node, ast.Name):
        lowered = node.id.lower()
        if lowered in _LITERAL_NAMES:
            return
        if node.id == _CTX_ROOT:
            raise ExpressionError(
                "上下文引用必须写成 $input 或 $节点id.output", source=source
            )
        if node.id.startswith("__") and node.id.endswith("__"):
            raise ExpressionError(f"禁止使用 dunder 名称: {node.id}", source=source)
        raise ExpressionError(
            f"未知根变量或裸标识符: {node.id}；请使用 $ 引用上下文", source=source
        )

    if isinstance(node, ast.Constant):
        if type(node.value) not in _SCALAR_TYPES:
            raise ExpressionError(
                f"不支持的常量类型: {type(node.value).__name__}", source=source
            )
        return

    raise ExpressionError(
        f"不支持的表达式语法: {type(node).__name__}", source=source
    )


def _attribute_parts(node: ast.Attribute, source: str) -> list[str]:
    """展开 ``a.b.c`` 并校验根与路径安全，返回属性段列表。"""
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    parts.reverse()

    if not isinstance(current, ast.Name):
        raise ExpressionError(
            "只允许访问 $input 或 $节点id.output 的字段", source=source
        )
    if current.id != _CTX_ROOT:
        raise ExpressionError(f"未知根变量: {current.id}", source=source)
    if not parts:
        raise ExpressionError("表达式不能只引用上下文根", source=source)

    for part in parts:
        if part.startswith("_"):
            raise ExpressionError(
                f"禁止访问以下划线开头的字段: {part}", source=source
            )
        if "__" in part:
            raise ExpressionError(f"禁止访问 dunder 字段: {part}", source=source)

    first = parts[0]
    if first == "input":
        return parts
    if (
        len(first) <= NODE_ID_MAX_LENGTH
        and NODE_ID_RE.fullmatch(first)
        and len(parts) >= 2
        and parts[1] == "output"
    ):
        return parts

    raise ExpressionError(
        f"非法字段路径: ${'.'.join(parts)}；只允许 $input 或 $节点id.output",
        source=source,
    )


def _collect_referenced_nodes(tree: ast.AST) -> tuple[str, ...]:
    """按源码顺序收集表达式读取的节点 id；整条属性链只记根节点一次。"""
    names: set[str] = set()

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Attribute):
            parts = _attribute_parts(node, source="")
            if parts[0] != "input":
                names.add(parts[0])
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return tuple(sorted(names))


# ------------------------------------------------------------------- 求值


def _evaluate_node(node: ast.AST, context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        lowered = node.id.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered == "null":
            return None
        if node.id == _CTX_ROOT:
            return context
        raise ExpressionError(f"未知根变量或裸标识符: {node.id}")

    if isinstance(node, ast.Attribute):
        return _resolve_path(context, _attribute_parts(node, source=""))

    if isinstance(node, ast.BoolOp):
        return _evaluate_bool_op(node, context)

    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_node(node.operand, context)
        if isinstance(node.op, ast.Not):
            _require_bool(operand, operator="not")
            return not operand
        if isinstance(operand, bool) or not isinstance(operand, (int, float)):
            raise ExpressionError(
                f"一元 {type(node.op).__name__} 只允许用于数字"
            )
        return -operand if isinstance(node.op, ast.USub) else +operand

    if isinstance(node, ast.Compare):
        return _evaluate_compare(node, context)

    raise ExpressionError(f"不支持的表达式语法: {type(node).__name__}")


def _evaluate_bool_op(node: ast.BoolOp, context: dict[str, Any]) -> bool:
    is_and = isinstance(node.op, ast.And)
    for value_node in node.values:
        value = _evaluate_node(value_node, context)
        _require_bool(value, operator="and" if is_and else "or")
        if is_and and not value:
            return False
        if not is_and and value:
            return True
    return is_and


def _evaluate_compare(node: ast.Compare, context: dict[str, Any]) -> bool:
    left = _evaluate_node(node.left, context)
    for operator, comparator in zip(node.ops, node.comparators):
        right = _evaluate_node(comparator, context)
        if not _compare(operator, left, right):
            return False
        left = right
    return True


def _compare(operator: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(operator, (ast.Eq, ast.NotEq)):
        if not _is_scalar(left) or not _is_scalar(right):
            raise ExpressionError("==/!= 只允许比较标量，不支持 dict/list 比较")
        return left == right if isinstance(operator, ast.Eq) else left != right

    left_kind = _ordered_kind(left)
    right_kind = _ordered_kind(right)
    if left_kind is None or right_kind is None or left_kind != right_kind:
        raise ExpressionError(
            ">/>=/</<= 只允许数字与数字或字符串与字符串比较"
        )

    if isinstance(operator, ast.Lt):
        return left < right
    if isinstance(operator, ast.LtE):
        return left <= right
    if isinstance(operator, ast.Gt):
        return left > right
    return left >= right


def _resolve_path(context: dict[str, Any], parts: list[str]) -> Any:
    current: Any = context
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise ExpressionError(f"缺失路径: ${'.'.join(parts)}")
        current = current[part]
    return current


def _require_bool(value: Any, *, operator: str) -> None:
    if type(value) is not bool:
        raise ExpressionError(
            f"{operator} 的操作数必须是布尔表达式，实际得到 "
            f"{type(value).__name__}"
        )


def _is_scalar(value: Any) -> bool:
    return type(value) in _SCALAR_TYPES


def _ordered_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


__all__ = [
    "CompiledExpression",
    "ExpressionError",
    "MAX_AST_NODES",
    "MAX_SOURCE_LENGTH",
    "compile_expression",
    "evaluate_condition",
    "evaluate_expression",
]
