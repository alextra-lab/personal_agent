"""FRE-1615: seed the three memory facts of tasks mr1-mr3 into ``neo4j-eval``.

Runs INSIDE an eval gateway, after the runner's graph wipe and before an ``mr`` turn:

    docker exec -i cloud-sim-seshat-gateway-treatment /app/.venv/bin/python - < scripts/eval/fre1615/memory_seed.py

It writes through the production memory path (``MemoryService.create_entity`` and
``create_conversation``), so the facts carry the embeddings and the owner ``user_id`` that a real turn
would give them. The owner is the eval gateway's ``agent_owner_email`` user, the identity of an
unauthenticated eval turn (``service/auth.py``). It refuses to run outside an eval deployment whose
Neo4j is ``neo4j-eval`` (FRE-375). It prints one JSON line: the entities and turns written.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

from personal_agent.config import settings
from personal_agent.memory.models import Entity, TurnNode
from personal_agent.memory.service import MemoryService
from personal_agent.service.auth import get_or_create_user_by_email
from personal_agent.service.database import AsyncSessionLocal

#: (days ago, user message, assistant reply, entities as (name, type, description)).
FACTS: tuple[tuple[int, str, str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        6,
        "For the record: I brew my coffee with an AeroPress, inverted, 17 g of beans to 250 ml of "
        "water at 85 C, with a two-minute steep.",
        "Noted: AeroPress, inverted method, 17 g to 250 ml, water at 85 C, two-minute steep.",
        (
            ("AeroPress", "Product", "The owner's coffee brewer, used inverted"),
            ("Coffee brewing", "Topic", "The owner brews 17 g to 250 ml at 85 C, two-minute steep"),
        ),
    ),
    (
        4,
        "I'm working on two projects right now: a greenhouse irrigation controller on an ESP32, and "
        "the kitchen renovation budget.",
        "Two active projects: the ESP32 greenhouse irrigation controller and the kitchen renovation "
        "budget.",
        (
            ("Greenhouse irrigation controller", "Project", "The owner's ESP32 irrigation project"),
            ("Kitchen renovation", "Project", "The owner's kitchen renovation and its budget"),
        ),
    ),
    (
        2,
        "Let's settle the backup schedule: nightly incremental backups at 02:00 to the NAS, a full "
        "backup every Sunday, and an offsite copy to Backblaze B2 once a week.",
        "Decided: incremental backups nightly at 02:00 to the NAS, a full backup each Sunday, and a "
        "weekly offsite copy to Backblaze B2.",
        (
            (
                "Backup schedule",
                "Decision",
                "Nightly 02:00 incremental to the NAS, full on Sunday, "
                "weekly offsite copy to Backblaze B2",
            ),
            ("NAS", "Device", "The owner's network storage, the backup target"),
        ),
    ),
)


async def seed() -> dict[str, object]:
    """Write every fact; return the counts."""
    if settings.deployment_profile != "eval" or "neo4j-eval" not in settings.neo4j_uri:
        raise SystemExit("memory_seed refuses: not an eval deployment on neo4j-eval")
    owner = settings.agent_owner_email
    if not owner:
        raise SystemExit("memory_seed refuses: no agent_owner_email")
    async with AsyncSessionLocal() as db:
        user_id = await get_or_create_user_by_email(db, owner)
        await db.commit()
    service = MemoryService()  # fre-375-allow: runs inside the eval gateway, guarded above
    if not await service.connect():
        raise SystemExit("memory_seed: neo4j-eval connect failed")
    entities = turns = 0
    now = datetime.now(timezone.utc)
    try:
        for days, user, assistant, ents in FACTS:
            session_id = str(uuid.uuid4())
            trace_id = str(uuid.uuid4())
            names = []
            for name, kind, description in ents:
                created = await service.create_entity(
                    Entity(name=name, entity_type=kind, description=description),
                    originating_trace_id=trace_id,
                    originating_session_id=session_id,
                )
                entities += bool(created)
                names.append(created or name)
            turn = TurnNode(
                turn_id=trace_id,
                trace_id=trace_id,
                session_id=session_id,
                sequence_number=1,
                timestamp=now - timedelta(days=days),
                user_message=user,
                assistant_response=assistant,
                key_entities=names,
            )
            turns += bool(await service.create_conversation(turn, user_id=user_id))
    finally:
        await service.disconnect()
    return {"entities": entities, "turns": turns}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(seed())))
    sys.exit(0)
