"""Seed the six built-in Agent Prompt templates (idempotent).

Usage (from the repository root):

    python scripts/seed_prompts.py            # create + publish v1 (repeat-safe)
    python scripts/seed_prompts.py --check    # verify tables and prompt files only

The script writes ``backend/agents/prompts/<role>.md`` into ``prompt_templates``
and ``prompt_versions`` (version 1) and publishes it via the existing version
manager. A second run creates nothing and reports ``created=0``.

``--check`` exits non-zero when ``prompt_templates`` / ``prompt_versions`` are
missing or any prompt markdown file is absent; it never skips silently.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
PROMPTS_DIR = BACKEND_DIR / "agents" / "prompts"

# ``app.*`` imports need the backend directory on sys.path when run from repo root.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import inspect, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.tenancy import reset_tenant_id, set_tenant_id  # noqa: E402
from app.db import get_engine, get_session_factory  # noqa: E402
from app.observability.models import PromptTemplate, PromptVersion  # noqa: E402
from app.observability.prompt import version_manager  # noqa: E402

USER_PROMPT_TEMPLATE = "Task: {{task}}"
VARIABLES_SCHEMA: dict = {
    "type": "object",
    "required": ["task"],
    "properties": {"task": {"type": "string", "description": "Task text for the agent"}},
}

# (role, template_id, template_name, description)
TEMPLATES: list[tuple[str, str, str, str]] = [
    ("supervisor", "agent.supervisor", "Agent Supervisor",
     "Orchestrates sub-agents; no direct data tools."),
    ("device", "agent.device", "Agent Device",
     "Device information, online status and telemetry history."),
    ("vision", "agent.vision", "Agent Vision",
     "Vision events, event types, camera and model links."),
    ("diagnosis", "agent.diagnosis", "Agent Diagnosis",
     "Correlates device state, events and knowledge for root causes."),
    ("operations", "agent.operations", "Agent Operations",
     "Edge node resources, service status and failed tasks."),
    ("report", "agent.report", "Agent Report",
     "Aggregates agent results into a Markdown report."),
]


def _read_prompt(role: str) -> str | None:
    path = PROMPTS_DIR / f"{role}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


async def _tables_exist() -> dict[str, bool]:
    engine = get_engine()
    async with engine.connect() as conn:
        def _inspect(sync_conn) -> dict[str, bool]:
            inspector = inspect(sync_conn)
            return {
                name: inspector.has_table(name)
                for name in ("prompt_templates", "prompt_versions")
            }

        return await conn.run_sync(_inspect)


async def check() -> int:
    tables = await _tables_exist()
    ok = True
    for name, exists in tables.items():
        print(f"[check] table {name}: {'OK' if exists else 'MISSING'}")
        if not exists:
            ok = False

    for role, *_ in TEMPLATES:
        path = PROMPTS_DIR / f"{role}.md"
        exists = path.is_file()
        print(f"[check] prompt {role}.md: {'OK' if exists else 'MISSING'}")
        if not exists:
            ok = False

    if not ok:
        print("[check] FAILED: required tables or prompt files are missing")
        return 1
    print("[check] result: OK")
    return 0


async def seed(tenant_id: uuid.UUID) -> int:
    created = 0
    skipped = 0
    repaired = 0
    missing_files: list[str] = []

    async with get_session_factory()() as session:
        token = set_tenant_id(tenant_id)
        try:
            for role, template_id, template_name, description in TEMPLATES:
                system_prompt = _read_prompt(role)
                if system_prompt is None:
                    missing_files.append(f"{role}.md")
                    print(f"[seed] ERROR missing prompt file: {role}.md")
                    continue

                existing = await session.scalar(
                    select(PromptTemplate).where(
                        PromptTemplate.tenant_id == tenant_id,
                        PromptTemplate.template_id == template_id,
                    )
                )
                if existing is None:
                    template = await version_manager.create_template(
                        session,
                        template_id=template_id,
                        template_name=template_name,
                        description=description,
                        category="agent",
                    )
                    version = await version_manager.add_version(
                        session,
                        template.id,
                        system_prompt=system_prompt,
                        user_prompt_template=USER_PROMPT_TEMPLATE,
                        variables_schema=VARIABLES_SCHEMA,
                        changelog="seed from backend/agents/prompts",
                    )
                    await version_manager.publish(session, template.id, version.version)
                    created += 1
                    print(
                        f"[seed] created {template_id} (pk={template.id}, "
                        f"version={version.version})"
                    )
                    continue

                if existing.active_version is None:
                    # 修复“模板存在但未发布”的半成品状态，仍保持幂等。
                    version = await session.scalar(
                        select(PromptVersion).where(
                            PromptVersion.tenant_id == tenant_id,
                            PromptVersion.template_id == existing.id,
                            PromptVersion.version == 1,
                        )
                    )
                    if version is None:
                        version = await version_manager.add_version(
                            session,
                            existing.id,
                            system_prompt=system_prompt,
                            user_prompt_template=USER_PROMPT_TEMPLATE,
                            variables_schema=VARIABLES_SCHEMA,
                            changelog="seed from backend/agents/prompts",
                        )
                    await version_manager.publish(session, existing.id, version.version)
                    repaired += 1
                    print(
                        f"[seed] repaired {template_id} -> active_version={version.version}"
                    )
                    continue

                skipped += 1
                print(f"[seed] skipped {template_id} (active_version={existing.active_version})")
        finally:
            reset_tenant_id(token)

    print(
        f"[seed] summary: tenant={tenant_id} created={created} skipped={skipped} "
        f"repaired={repaired} missing_files={len(missing_files)}"
    )
    if missing_files:
        print("[seed] FAILED: missing prompt files: " + ", ".join(missing_files))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed built-in agent prompt templates")
    parser.add_argument(
        "--tenant",
        default=get_settings().tenant_default_id,
        help="Tenant UUID (default: TENANT_DEFAULT_ID)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only verify prompt tables and markdown files; do not write data",
    )
    args = parser.parse_args(argv)

    try:
        tenant_id = uuid.UUID(str(args.tenant))
        return asyncio.run(check() if args.check else seed(tenant_id))
    except Exception as exc:  # noqa: BLE001 - CLI must exit with a clear message
        print(f"[prompts] ERROR: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
