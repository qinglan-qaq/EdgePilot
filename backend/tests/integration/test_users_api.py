"""用户管理 API 集成测试（Task 26）。

覆盖 admin 分页/隔离、viewer/operator 403、创建与更新、密码真实哈希、
审计旁路与跨租户 404。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.core.security import hash_password, verify_password
from app.models.audit import AuditLog
from app.models.user import User
from tests.conftest import (
    ADMIN_EMAIL,
    OTHER_ADMIN_EMAIL,
    OTHER_TENANT,
    PASSWORD,
    TENANT,
    VIEWER_EMAIL,
    auth_headers,
    login_token,
)

USERS_URL = "/api/v1/users"
OPERATOR_EMAIL = "operator@edgepilot.local"


async def _add_user(
    session,
    email: str,
    role: str,
    *,
    tenant_id=TENANT,
    password: str = PASSWORD,
    status: str = "active",
) -> User:
    user = User(
        tenant_id=tenant_id,
        email=email,
        password_hash=hash_password(password),
        display_name=email.split("@")[0],
        role=role,
        status=status,
    )
    session.add(user)
    await session.flush()
    return user


async def test_users_list_admin_only_tenant_scoped_and_paginated(
    client, session, seeded_users, admin_token, viewer_token
):
    operator = await _add_user(session, OPERATOR_EMAIL, "operator")
    operator_token = await login_token(client, OPERATOR_EMAIL)

    for token in (viewer_token, operator_token):
        forbidden = await client.get(USERS_URL, headers=auth_headers(token))
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == 403

    response = await client.get(USERS_URL, headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 3
    emails = {item["email"] for item in data["items"]}
    assert emails == {ADMIN_EMAIL, VIEWER_EMAIL, OPERATOR_EMAIL}
    assert OTHER_ADMIN_EMAIL not in emails
    assert all("password_hash" not in item for item in data["items"])

    page1 = (
        await client.get(f"{USERS_URL}?page=1&size=2", headers=auth_headers(admin_token))
    ).json()["data"]
    page2 = (
        await client.get(f"{USERS_URL}?page=2&size=2", headers=auth_headers(admin_token))
    ).json()["data"]
    assert page1["total"] == 3 and len(page1["items"]) == 2
    assert page2["total"] == 3 and len(page2["items"]) == 1
    assert page1["page"] == 1 and page1["size"] == 2
    assert page2["page"] == 2 and page2["size"] == 2
    assert {item["id"] for item in page1["items"]}.isdisjoint(
        {item["id"] for item in page2["items"]}
    )

    filtered = (
        await client.get(f"{USERS_URL}?role=operator", headers=auth_headers(admin_token))
    ).json()["data"]
    assert filtered["total"] == 1
    assert filtered["items"][0]["id"] == operator.id

    invalid_role = await client.get(
        f"{USERS_URL}?role=root", headers=auth_headers(admin_token)
    )
    assert invalid_role.status_code == 422


async def test_create_user_hashes_password_audits_and_can_login(
    client, session, seeded_users, admin_token
):
    payload = {
        "email": "  New.User@Example.COM  ",
        "password": "NewPassw0rd!",
        "display_name": "New User",
        "role": "operator",
        "status": "active",
    }
    response = await client.post(
        USERS_URL, json=payload, headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["email"] == "new.user@example.com"
    assert data["role"] == "operator"
    assert "password_hash" not in data
    assert "password" not in data

    # 新用户能用明文密码真实登录。
    token = await login_token(client, "new.user@example.com", "NewPassw0rd!")
    assert token

    row = await session.scalar(select(User).where(User.id == data["id"]))
    assert row is not None
    assert row.password_hash != "NewPassw0rd!"
    assert verify_password(row.password_hash, "NewPassw0rd!")

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == TENANT,
            AuditLog.action == "user.create",
            AuditLog.resource_id == row.id,
        )
    )
    assert audit is not None
    assert audit.user_id is not None
    assert audit.detail["email"] == "new.user@example.com"
    assert "password" not in json.dumps(audit.detail)


async def test_create_user_conflicts_and_validation(
    client, session, seeded_users, admin_token
):
    payload = {
        "email": "dup@example.com",
        "password": "DupPassw0rd!",
        "display_name": "Dup",
    }
    first = await client.post(USERS_URL, json=payload, headers=auth_headers(admin_token))
    assert first.status_code == 200

    duplicate = await client.post(
        USERS_URL,
        json={**payload, "email": "  DUP@Example.COM "},
        headers=auth_headers(admin_token),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == 409

    invalid_role = await client.post(
        USERS_URL,
        json={**payload, "email": "role@example.com", "role": "root"},
        headers=auth_headers(admin_token),
    )
    assert invalid_role.status_code == 422

    short_password = await client.post(
        USERS_URL,
        json={**payload, "email": "short@example.com", "password": "short"},
        headers=auth_headers(admin_token),
    )
    assert short_password.status_code == 422

    bad_email = await client.post(
        USERS_URL,
        json={**payload, "email": "not-an-email"},
        headers=auth_headers(admin_token),
    )
    assert bad_email.status_code == 422


async def test_patch_user_updates_fields_audits_and_blocks_cross_tenant(
    client, session, seeded_users, admin_token
):
    created = (
        await client.post(
            USERS_URL,
            json={
                "email": "patch-me@example.com",
                "password": "InitialPass1!",
                "display_name": "Before",
                "role": "viewer",
            },
            headers=auth_headers(admin_token),
        )
    ).json()["data"]
    user_id = created["id"]

    response = await client.patch(
        f"{USERS_URL}/{user_id}",
        json={
            "display_name": "After",
            "role": "operator",
            "email": "  Renamed@Example.com ",
            "password": "ChangedPass1!",
        },
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["display_name"] == "After"
    assert data["role"] == "operator"
    assert data["email"] == "renamed@example.com"
    assert "password_hash" not in data

    # 新密码可登录，旧密码不可。
    assert await login_token(client, "renamed@example.com", "ChangedPass1!")
    old_password = await client.post(
        "/api/v1/auth/login",
        json={"email": "renamed@example.com", "password": "InitialPass1!"},
    )
    assert old_password.status_code == 401

    row = await session.scalar(select(User).where(User.id == user_id))
    assert verify_password(row.password_hash, "ChangedPass1!")
    assert row.password_hash != "ChangedPass1!"

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == TENANT,
            AuditLog.action == "user.update",
            AuditLog.resource_id == user_id,
        )
    )
    assert audit is not None
    assert set(audit.detail["changed"]) == {"display_name", "role", "email"}
    assert "password" not in json.dumps(audit.detail)

    # 显式 null 不能把非空列写成 NULL，也不能触发 500。
    nulls = await client.patch(
        f"{USERS_URL}/{user_id}",
        json={"display_name": None, "email": None, "role": None, "status": None},
        headers=auth_headers(admin_token),
    )
    assert nulls.status_code == 200
    assert nulls.json()["data"]["display_name"] == "After"
    assert nulls.json()["data"]["email"] == "renamed@example.com"

    # 跨租户 id 天然 404，且不会改动对方数据。
    other_user = seeded_users[2]
    cross = await client.patch(
        f"{USERS_URL}/{other_user.id}",
        json={"display_name": "hacked"},
        headers=auth_headers(admin_token),
    )
    assert cross.status_code == 404
    assert cross.json()["code"] == 404
    other_row = await session.scalar(
        select(User).where(User.id == other_user.id)
    )
    assert other_row.display_name == "Other"

    # 禁用后登录失败 401。
    disabled = await client.patch(
        f"{USERS_URL}/{user_id}",
        json={"status": "disabled"},
        headers=auth_headers(admin_token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["status"] == "disabled"
    denied = await client.post(
        "/api/v1/auth/login",
        json={"email": "renamed@example.com", "password": "ChangedPass1!"},
    )
    assert denied.status_code == 401
