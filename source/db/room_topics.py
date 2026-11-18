"""Room topic persistence: what a room keeps coming back to, and which parts
of its chunks belong to each topic. The housekeeping role assigns the parts
(agents/housekeeping.py, agents/room_topics.py); the room's page reads the
same rows. Design:
docs/superpowers/specs/2026-11-18-room-topic-registry-design.md.
"""
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from db.models import RoomChunk, RoomTopic, RoomTopicMember, RoomTopicSuggestion, db

__all__ = [
    "add_room_topic",
    "add_room_topic_members",
    "assigned_chunk_parts",
    "dismiss_room_topic_suggestion",
    "edit_room_topic",
    "explain_room_topic",
    "get_room_chunk",
    "get_room_topic",
    "list_chunk_topics",
    "list_room_topic_members",
    "list_room_topic_suggestions",
    "list_room_topics",
    "merge_room_topics",
    "room_topic_exemplars",
    "room_topic_requests",
    "suggest_room_topic_merge",
    "unassigned_room_chunks",
]


def get_room_chunk(chunk_uuid: UUID) -> RoomChunk | None:
    return db.session.get(RoomChunk, chunk_uuid)


def get_room_topic(topic_uuid: UUID) -> RoomTopic | None:
    return db.session.get(RoomTopic, topic_uuid)


def list_room_topics(room_uuid: UUID) -> list[RoomTopic]:
    return (db.session.query(RoomTopic).filter_by(room_uuid=room_uuid)
            .order_by(RoomTopic.created_at.asc()).all())


def _in_order(query):
    return query.order_by(RoomTopicMember.created_at.asc(),
                          RoomTopicMember.part_index.asc())


def room_topic_exemplars(room_uuid: UUID, axis: str,
                         limit: int) -> list[dict[str, Any]]:
    """A room's topics on one axis, oldest first, each with the members a
    new part is compared with: `{topic, exemplars}`, the exemplars being
    the topic's first `limit` members."""
    topics = (db.session.query(RoomTopic)
              .filter_by(room_uuid=room_uuid, axis=axis)
              .order_by(RoomTopic.created_at.asc()).all())
    members: dict[UUID, list[RoomTopicMember]] = {}
    for m in _in_order(db.session.query(RoomTopicMember)
                       .filter_by(room_uuid=room_uuid, axis=axis)):
        mine = members.setdefault(m.topic_uuid, [])
        if len(mine) < limit:
            mine.append(m)
    return [{"topic": t, "exemplars": members.get(t.uuid, [])} for t in topics]


def assigned_chunk_parts(chunk_uuid: UUID) -> set[int]:
    """The places of the chunk's parts that have a topic already."""
    rows = (db.session.query(RoomTopicMember.part_index)
            .filter_by(chunk_uuid=chunk_uuid).distinct().all())
    return {r[0] for r in rows}


def add_room_topic(room_uuid: UUID, axis: str, name: str) -> RoomTopic:
    """A new topic, not yet committed: `add_room_topic_members` commits it
    with its first member, so no topic is ever stored without one."""
    topic = RoomTopic(room_uuid=room_uuid, axis=axis, name=name)
    db.session.add(topic)
    db.session.flush()
    return topic


def add_room_topic_members(rows: list[dict[str, Any]]) -> None:
    """One part's memberships, both axes in one commit: a part has a topic
    on each axis or on neither."""
    db.session.add_all([RoomTopicMember(**row) for row in rows])
    db.session.commit()


def list_chunk_topics(chunk_uuid: UUID) -> list[RoomTopic]:
    """The topics the chunk's parts belong to."""
    uuids = [r[0] for r in db.session.query(RoomTopicMember.topic_uuid)
             .filter_by(chunk_uuid=chunk_uuid).distinct().all()]
    if not uuids:
        return []
    return (db.session.query(RoomTopic).filter(RoomTopic.uuid.in_(uuids))
            .order_by(RoomTopic.created_at.asc()).all())


def list_room_topic_members(room_uuid: UUID) -> list[RoomTopicMember]:
    return _in_order(db.session.query(RoomTopicMember)
                     .filter_by(room_uuid=room_uuid)).all()


def room_topic_requests(topic_uuid: UUID, limit: int) -> tuple[int, list[dict[str, str]]]:
    """How many members a topic has, and its newest `limit` as what the
    model that explains it reads: `{wanted, result, tools}`, oldest first.
    The result and the tools are the part's own, read off its chunk."""
    from agents.room_chunks import part_tools_text

    members = _in_order(db.session.query(RoomTopicMember)
                        .filter_by(topic_uuid=topic_uuid)).all()
    out = []
    for m in members[-limit:]:
        chunk = db.session.get(RoomChunk, m.chunk_uuid)
        parts = (chunk.parts or []) if chunk is not None else []
        part = parts[m.part_index] if m.part_index < len(parts) else {}
        out.append({"wanted": m.phrase, "result": str(part.get("result") or ""),
                    "tools": part_tools_text(part)})
    return len(members), out


def explain_room_topic(topic_uuid: UUID, name: str, explanation: str,
                       members: int) -> bool:
    """The model's name and explanation for a topic. Not written, and False,
    when the operator has edited the topic in the meantime."""
    topic = db.session.get(RoomTopic, topic_uuid)
    if topic is None or topic.operator_edited:
        return False
    topic.name, topic.explanation, topic.explained_members = name, explanation, members
    db.session.commit()
    return True


def edit_room_topic(topic_uuid: UUID, name: str, explanation: str) -> RoomTopic | None:
    """The operator's name and explanation. From here on the topic is theirs:
    the model does not write it again."""
    topic = db.session.get(RoomTopic, topic_uuid)
    if topic is None:
        return None
    topic.name, topic.explanation, topic.operator_edited = name, explanation, True
    db.session.commit()
    return topic


def unassigned_room_chunks(room_uuid: UUID, size: int, overlap: int,
                           version: int, limit: int) -> list[RoomChunk]:
    """The chunks of a series whose parts have no topic yet, oldest first,
    at most `limit`."""
    assigned = db.session.query(RoomTopicMember.chunk_uuid).filter_by(room_uuid=room_uuid)
    return (db.session.query(RoomChunk)
            .filter_by(room_uuid=room_uuid, chunk_size=size, overlap=overlap,
                       prompt_version=version)
            .filter(RoomChunk.uuid.notin_(assigned))
            .order_by(RoomChunk.seq.asc()).limit(limit).all())


def merge_room_topics(keep_uuid: UUID, drop_uuid: UUID) -> RoomTopic | None:
    """Make two topics one: the members of `drop` become members of `keep`,
    and `drop` is gone. `keep` stays what it was called, unless only `drop`
    carried the operator's own words, which are then kept. A topic the
    operator did not write is due for a new explanation, since it now holds
    more than the model explained. None when either is missing, or they are
    not two topics of one room and axis."""
    keep = db.session.get(RoomTopic, keep_uuid)
    drop = db.session.get(RoomTopic, drop_uuid)
    if (keep is None or drop is None or keep.uuid == drop.uuid
            or (keep.room_uuid, keep.axis) != (drop.room_uuid, drop.axis)):
        return None
    if drop.operator_edited and not keep.operator_edited:
        keep.name, keep.explanation, keep.operator_edited = drop.name, drop.explanation, True
    if not keep.operator_edited:
        keep.explained_members = 0
    db.session.execute(sa.update(RoomTopicMember)
                       .where(RoomTopicMember.topic_uuid == drop.uuid)
                       .values(topic_uuid=keep.uuid))
    # What was suggested of the topic that is gone no longer names a topic.
    db.session.execute(sa.delete(RoomTopicSuggestion).where(sa.or_(
        RoomTopicSuggestion.topic_uuid == drop.uuid,
        RoomTopicSuggestion.other_uuid == drop.uuid)))
    db.session.delete(drop)
    db.session.commit()
    return keep


def suggest_room_topic_merge(room_uuid: UUID, topic_uuid: UUID, other_uuid: UUID,
                             score: float, reason: str, *, state: str = "open") -> bool:
    """Record what was found of two topics: `open`, they look like one; or
    `apart`, the model that read their requests said they are two. False,
    and nothing written, when the pair is recorded already, in either
    order: a pair is compared once, and one the operator dismissed stays
    dismissed."""
    known = db.session.query(RoomTopicSuggestion.uuid).filter(sa.or_(
        sa.and_(RoomTopicSuggestion.topic_uuid == topic_uuid,
                RoomTopicSuggestion.other_uuid == other_uuid),
        sa.and_(RoomTopicSuggestion.topic_uuid == other_uuid,
                RoomTopicSuggestion.other_uuid == topic_uuid))).first()
    if known is not None:
        return False
    db.session.add(RoomTopicSuggestion(
        room_uuid=room_uuid, topic_uuid=topic_uuid, other_uuid=other_uuid,
        score=score, reason=reason, state=state))
    db.session.commit()
    return True


def list_room_topic_suggestions(room_uuid: UUID, *,
                                state: str | None = "open") -> list[RoomTopicSuggestion]:
    """A room's suggestions, best first: the open ones, or with `state=None`
    all of them, which is what a new pass checks a pair against."""
    query = db.session.query(RoomTopicSuggestion).filter_by(room_uuid=room_uuid)
    if state is not None:
        query = query.filter_by(state=state)
    return query.order_by(RoomTopicSuggestion.score.desc()).all()


def dismiss_room_topic_suggestion(suggestion_uuid: UUID) -> RoomTopicSuggestion | None:
    """The operator says the two are not one topic. The row stays, so the
    pair is not suggested again."""
    row = db.session.get(RoomTopicSuggestion, suggestion_uuid)
    if row is None:
        return None
    row.state = "dismissed"
    db.session.commit()
    return row
