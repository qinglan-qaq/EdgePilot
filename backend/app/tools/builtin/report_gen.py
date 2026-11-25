"""报告生成内置工具（``generate_report``）：只把结构化段落拼成 Markdown，不编内容。

这是仓库内唯一的报告渲染工具实现：模块名刻意用 ``report_gen``，避免与
``app.agents.report``（Agent 契约/证据链构建器）以及旧 ``builtin/report.py``
混淆；工具 ID、权限范围与渲染行为保持不变。
"""

from app.tools.registry import ToolContext, ToolRegistry

REPORT_INPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["title", "sections"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["heading", "content"],
                "properties": {
                    "heading": {"type": "string", "minLength": 1, "maxLength": 200},
                    "content": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

REPORT_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["title", "markdown"],
    "properties": {
        "title": {"type": "string"},
        "markdown": {"type": "string"},
        "section_count": {"type": "integer"},
    },
}


async def generate_report(args: dict, ctx: ToolContext | None) -> dict:
    """把 title + sections 渲染为 Markdown 报告（严格使用入参内容）。"""
    title = args["title"]
    sections = args["sections"]

    lines = [f"# {title}", ""]
    for section in sections:
        lines.append(f"## {section['heading']}")
        lines.append("")
        content = section["content"]
        if content:
            lines.append(content)
            lines.append("")

    markdown = "\n".join(lines).rstrip() + "\n"
    return {"title": title, "markdown": markdown, "section_count": len(sections)}


def register(registry: ToolRegistry) -> None:
    registry.register(
        "generate_report",
        "1.0.0",
        REPORT_INPUT_SCHEMA,
        REPORT_OUTPUT_SCHEMA,
        generate_report,
        permission_scope="read",
        timeout_ms=5000,
        max_retries=1,
        side_effect=False,
        description="将 title + sections 拼装为 Markdown 报告，不生成入参之外的内容",
    )
