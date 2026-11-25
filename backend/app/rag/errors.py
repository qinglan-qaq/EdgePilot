"""RAG 业务错误。

全部继承 ``AppError``，由 FastAPI 统一异常处理器转成 400 信封；错误信息只说明
类型/原因，不包含文档全文或密钥。
"""

from app.core.exceptions import AppError


class RagError(AppError):
    code = http_status = 400


class UnsupportedDocumentType(RagError):
    def __init__(self, suffix: str) -> None:
        super().__init__(
            f"不支持的文件类型 {suffix or '(无扩展名)'}："
            "仅支持 .pdf / .md / .markdown / .txt；.docx 需安装 python-docx（当前环境未安装）"
        )


class DocumentEmptyError(RagError):
    def __init__(self, message: str = "文档内容为空，未写入知识库") -> None:
        super().__init__(message)


class DocumentParseError(RagError):
    def __init__(self, message: str = "文档解析失败") -> None:
        super().__init__(message)
