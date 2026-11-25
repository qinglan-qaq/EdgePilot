"""工具执行外壳的错误类型。

工具执行的任何失败都以结构化错误返回，不向调用方抛裸异常：
``code`` 供上层（Agent 循环 / API）判定，``message`` 给人看，
``retryable`` 说明同一参数重试是否有意义。
"""


class ToolExecutionError(Exception):
    """工具执行失败（权限 / 校验 / 超时 / 重试耗尽 / 输出不合法）。

    Attributes:
        code: 稳定错误码，见 ``DEFAULT_MESSAGES``。
        message: 人类可读错误信息。
        retryable: 是否值得原样重试。
        detail: 可选的补充信息（如校验错误列表），不含敏感数据。
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        retryable: bool = False,
        detail: dict | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, code)
        self.retryable = retryable
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """供埋点/审计使用的结构化错误。"""
        payload: dict = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


# 与 Task 9 约定的失败码一一对应
DEFAULT_MESSAGES: dict[str, str] = {
    "tool_not_found": "工具不存在",
    "tool_disabled": "工具已禁用",
    "permission_denied": "无权调用该工具",
    "approval_required": "该工具需要人工审批后才能调用",
    "input_schema_invalid": "工具入参不符合 input_schema",
    "output_schema_invalid": "工具输出不符合 output_schema",
    "timeout": "工具执行超时",
    "execution_failed": "工具执行失败（重试已耗尽）",
}
