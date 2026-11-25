"""API Key 等敏感字段的对称加密（Fernet）。

密钥来源优先级：
1. ``SECRET_ENCRYPTION_KEY``（必须是 Fernet 可用的 32 字节 urlsafe-base64 密钥）；
2. 未配置时由 ``jwt_secret`` 派生开发用密钥。

警告：第 2 种只是开发默认，生产必须用环境变量提供独立密钥
（``SECRET_ENCRYPTION_KEY``），否则 JWT secret 泄露会同时暴露已存密钥。
任何时候都不记录明文或密文。
"""

import base64
import hashlib

from cryptography.fernet import Fernet

from app.core.config import get_settings


def get_fernet() -> Fernet:
    settings = get_settings()
    if settings.secret_encryption_key:
        return Fernet(settings.secret_encryption_key.encode())

    # 仅开发默认：由 jwt_secret 派生稳定密钥，避免明文落库。
    # 生产必须用环境变量提供独立密钥。
    derived = base64.urlsafe_b64encode(
        hashlib.sha256(("edgepilot-dev:" + settings.jwt_secret).encode()).digest()
    )
    return Fernet(derived)


def encrypt_key(plain: str) -> str:
    """加密明文密钥，返回可直接落库的字符串。"""
    return get_fernet().encrypt(plain.encode()).decode()


def decrypt_key(enc: str) -> str:
    """还原 ``encrypt_key`` 生成的密文。"""
    return get_fernet().decrypt(enc.encode()).decode()
