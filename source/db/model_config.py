"""Model configuration persistence.

Split out of db.py. Holds model_config rows, per-model argument overrides,
model groups (+ capability constraints), and agent->group bindings, plus the
resolved-kwargs/sync helpers. Re-exported from db for import compatibility.
"""
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from db.models import (
    CAPABILITY_CONSTRAINTS,
    AgentModelBinding,
    ModelConfig,
    ModelConfigOverride,
    ModelGroup,
    ModelGroupMember,
    args_reasoning_on,
    db,
)


def list_model_configs() -> list[ModelConfig]:
    return db.session.query(ModelConfig).order_by(ModelConfig.id.asc()).all()


def get_model_config(model_config_uuid: UUID) -> ModelConfig | None:
    return (
        db.session.query(ModelConfig)
        .filter(ModelConfig.uuid == model_config_uuid)
        .one_or_none()
    )


def create_model_config(model_name: str, arguments: dict[str, Any]) -> ModelConfig:
    row = ModelConfig(model_name=model_name, arguments=arguments)
    db.session.add(row)
    db.session.commit()
    return row


def list_model_config_overrides(
    model_config_uuid: UUID | None = None,
) -> list[ModelConfigOverride]:
    q = db.session.query(ModelConfigOverride)
    if model_config_uuid is not None:
        q = q.filter(ModelConfigOverride.model_config_uuid == model_config_uuid)
    return q.order_by(ModelConfigOverride.id.asc()).all()


def get_model_config_override(
    override_uuid: UUID,
) -> ModelConfigOverride | None:
    return (
        db.session.query(ModelConfigOverride)
        .filter(ModelConfigOverride.uuid == override_uuid)
        .one_or_none()
    )


def create_model_config_override(
    model_config_uuid: UUID,
    overrides: dict[str, Any],
    display_name: str = "",
) -> ModelConfigOverride:
    row = ModelConfigOverride(
        model_config_uuid=model_config_uuid,
        overrides=overrides,
        display_name=display_name,
    )
    db.session.add(row)
    db.session.commit()
    return row


def delete_model_config_override(override_uuid: UUID) -> None:
    row = get_model_config_override(override_uuid)
    if row is None:
        raise LookupError(f"model_config_override {override_uuid} not found")
    db.session.delete(row)
    db.session.commit()


def list_model_configs_with_overrides(
    sort_by: Literal["provider", "model_name"] = "provider",
) -> list[tuple[ModelConfig, list[ModelConfigOverride]]]:
    """One DB pass: every ModelConfig and the ModelConfigOverrides that
    reference it. Available configs always sort before unavailable ones.

    sort_by="provider" (default): provider ASC, model_name ASC. Groups
    the tree by backend (all LM Studio rows, then Jan, then Ollama, …).
    sort_by="model_name": model_name ASC only — mixes providers but
    surfaces same-name twins (e.g. llama3.2 under both LM Studio and
    Ollama) right next to each other.

    Overrides under each config are sorted by display_name."""
    if sort_by == "model_name":
        order = (ModelConfig.available.desc(), ModelConfig.model_name.asc())
    else:
        order = (
            ModelConfig.available.desc(),
            ModelConfig.provider.asc(),
            ModelConfig.model_name.asc(),
        )
    configs = (
        db.session.query(ModelConfig)
        .order_by(*order)
        .all()
    )
    overrides = (
        db.session.query(ModelConfigOverride)
        .order_by(ModelConfigOverride.display_name.asc())
        .all()
    )
    by_config: dict[UUID, list[ModelConfigOverride]] = {c.uuid: [] for c in configs}
    for ov in overrides:
        by_config.setdefault(ov.model_config_uuid, []).append(ov)
    return [(c, by_config.get(c.uuid, [])) for c in configs]


def chat_model_choices() -> list[dict[str, Any]]:
    """Every ModelConfig and ModelConfigOverride flattened to
    {uuid (str), label, available} — the option list for a direct room's
    model picker and the chat.default_model setting. Available configs
    sort first; an override's label is its base config's plus its own name."""
    out: list[dict[str, Any]] = []
    for cfg, overrides in list_model_configs_with_overrides():
        base = f"{cfg.provider} · {cfg.effective_display_name}"
        out.append({
            "uuid": str(cfg.uuid),
            "label": base,
            "available": bool(cfg.available),
        })
        for ov in overrides:
            out.append({
                "uuid": str(ov.uuid),
                "label": f"{base} — {ov.effective_display_name}",
                "available": bool(cfg.available),
            })
    return out


def default_chat_model_uuid() -> UUID | None:
    """The alphabetically earliest ModelConfigOverride, by its picker label
    ('provider · config — override', case-insensitive). This is the built-in
    default model for direct chat rooms while the chat.default_model setting
    is unset. None when no overrides exist."""
    best: tuple[str, UUID] | None = None
    for cfg, overrides in list_model_configs_with_overrides():
        base = f"{cfg.provider} · {cfg.effective_display_name}"
        for ov in overrides:
            label = f"{base} — {ov.effective_display_name}".lower()
            if best is None or label < best[0]:
                best = (label, ov.uuid)
    return best[1] if best else None


def resolved_arguments(override_uuid: UUID) -> dict[str, Any]:
    """Return the effective LlamaIndex-constructor args for an override:
    base ModelConfig.arguments shallow-merged with override.overrides
    (override values win)."""
    override = get_model_config_override(override_uuid)
    if override is None:
        raise LookupError(f"model_config_override {override_uuid} not found")
    base = get_model_config(override.model_config_uuid)
    if base is None:
        raise LookupError(
            f"override {override_uuid} references missing model_config "
            f"{override.model_config_uuid}"
        )
    merged = dict(base.arguments)
    merged.update(override.overrides)
    return merged


def resolved_model_kwargs(target_uuid: UUID) -> tuple[str, str, dict[str, Any]]:
    """Resolve a ModelConfig OR ModelConfigOverride uuid to the data needed to
    build a LlamaIndex client: (provider_id, model_name, kwargs).

    Tries ModelConfig first, then ModelConfigOverride; for an override the
    arguments are the base config ∪ override (override wins). The `model`
    key is stripped from kwargs since callers pass model_name separately.
    The first element is the base config's provider id ('lm_studio' / 'jan'
    / …) — overrides inherit their parent config's provider. Raises
    LookupError if the uuid is in neither table (or an override's base is
    gone)."""
    cfg = get_model_config(target_uuid)
    if cfg is not None:
        args = dict(cfg.arguments)
        args.pop("model", None)
        return cfg.provider, cfg.model_name, args
    override = get_model_config_override(target_uuid)
    if override is None:
        raise LookupError(
            f"no ModelConfig or ModelConfigOverride with uuid {target_uuid}"
        )
    base = get_model_config(override.model_config_uuid)
    if base is None:
        raise LookupError(
            f"override {target_uuid} references missing base config "
            f"{override.model_config_uuid}"
        )
    args = {**base.arguments, **override.overrides}
    args.pop("model", None)
    return base.provider, base.model_name, args


def sync_model_configs(
    provider: str,
    available_model_names: list[str],
    default_arguments: dict[str, Any],
    sizes_by_name: dict[str, int] | None = None,
    function_calling_by_name: dict[str, bool] | None = None,
    force_update_arguments: bool = False,
) -> dict[str, int]:
    """Reconcile model_config rows belonging to `provider` against the set of
    currently-available models for that provider.

    Scope: only rows where ModelConfig.provider == `provider` are inspected or
    mutated. Rows from other providers are left untouched — this is the
    contract that lets one provider be unreachable on startup without
    disabling another's rows.

    Normally the sync only touches the `available` flag and the observational
    `size_bytes` — never `model_name`, never the row's identity, and never the
    `arguments` blob of an *existing* row, which stays a permanent record of
    what was tried for that uuid.

    - For each name in `available_model_names`: ensure a row exists for this
      provider (creating one with `default_arguments`, with
      `is_function_calling_model` taken from `function_calling_by_name` if
      known) and mark `available=True`.
    - For each existing row of this provider whose `model_name` is NOT in
      `available_model_names`: flip `available=False`. Never deletes.

    `function_calling_by_name` maps model name -> whether the provider reports
    the `tool_use` capability. It's always applied to *new* rows. For *existing*
    rows it is only applied when `force_update_arguments=True` (the explicit
    `--force-model-sync` path) — so the immutable-arguments invariant holds by
    default, but the operator can opt in to refresh the capability flag to
    match reality. Pass `function_calling_by_name=None` (e.g. the provider's
    native API was unreachable) to leave the flag untouched rather than clobber
    it to False.

    Returns a counts summary for logging."""
    available_set = set(available_model_names)
    sizes = sizes_by_name or {}
    func_calling = function_calling_by_name or {}
    existing = {
        c.model_name: c
        for c in db.session.query(ModelConfig)
        .filter(ModelConfig.provider == provider)
        .all()
    }

    created = 0
    re_enabled = 0
    disabled = 0
    function_calling_updated = 0

    for name in available_set:
        cfg = existing.get(name)
        size = sizes.get(name)
        if cfg is None:
            arguments = dict(default_arguments)
            if name in func_calling:
                arguments["is_function_calling_model"] = func_calling[name]
            db.session.add(
                ModelConfig(
                    provider=provider,
                    model_name=name,
                    arguments=arguments,
                    available=True,
                    size_bytes=size,
                )
            )
            created += 1
        else:
            if not cfg.available:
                cfg.available = True
                re_enabled += 1
            # Refresh size_bytes for currently-available models so the field
            # tracks the file LM Studio is actually serving. size_bytes is
            # observational metadata (not the immutable `arguments` blob),
            # so updating is safe.
            if size is not None and cfg.size_bytes != size:
                cfg.size_bytes = size
            # Only with the explicit force flag do we mutate an existing row's
            # arguments — refreshing is_function_calling_model to match what LM
            # Studio reports (reassign the dict so SQLAlchemy sees the JSONB
            # change).
            if (
                force_update_arguments
                and name in func_calling
                and cfg.arguments.get("is_function_calling_model")
                != func_calling[name]
            ):
                cfg.arguments = {
                    **cfg.arguments,
                    "is_function_calling_model": func_calling[name],
                }
                function_calling_updated += 1

    for name, cfg in existing.items():
        if name not in available_set and cfg.available:
            cfg.available = False
            disabled += 1

    db.session.commit()
    return {
        "created": created,
        "re_enabled": re_enabled,
        "disabled": disabled,
        "function_calling_updated": function_calling_updated,
    }


def list_model_groups() -> list[ModelGroup]:
    return db.session.query(ModelGroup).order_by(ModelGroup.name.asc()).all()


def get_model_group(group_uuid: UUID) -> ModelGroup | None:
    return (
        db.session.query(ModelGroup)
        .filter(ModelGroup.uuid == group_uuid)
        .one_or_none()
    )


def create_model_group(
    name: str,
    function_calling_constraint: str = "dont_care",
    structured_output_constraint: str = "dont_care",
    reasoning_constraint: str = "dont_care",
) -> ModelGroup:
    if function_calling_constraint not in CAPABILITY_CONSTRAINTS:
        raise ValueError(f"invalid function_calling_constraint: {function_calling_constraint!r}")
    if structured_output_constraint not in CAPABILITY_CONSTRAINTS:
        raise ValueError(f"invalid structured_output_constraint: {structured_output_constraint!r}")
    if reasoning_constraint not in CAPABILITY_CONSTRAINTS:
        raise ValueError(f"invalid reasoning_constraint: {reasoning_constraint!r}")
    row = ModelGroup(
        name=name,
        function_calling_constraint=function_calling_constraint,
        structured_output_constraint=structured_output_constraint,
        reasoning_constraint=reasoning_constraint,
    )
    db.session.add(row)
    db.session.commit()
    return row


def member_is_function_calling(member_uuid: UUID) -> bool:
    """Whether a group member (a ModelConfig or ModelConfigOverride uuid) resolves
    to is_function_calling_model=True. Missing members resolve to False."""
    try:
        _provider_id, _model_name, args = resolved_model_kwargs(member_uuid)
    except LookupError:
        return False
    return bool(args.get("is_function_calling_model"))


def member_uses_structured_output(member_uuid: UUID) -> bool:
    """Whether a ModelConfig/ModelConfigOverride uuid resolves to
    should_use_structured_outputs=True. Missing members resolve to False."""
    try:
        _provider_id, _model_name, args = resolved_model_kwargs(member_uuid)
    except LookupError:
        return False
    return bool(args.get("should_use_structured_outputs"))


def member_supports_reasoning(member_uuid: UUID) -> bool:
    """Whether a ModelConfig/ModelConfigOverride uuid resolves to reasoning on
    (see args_reasoning_on). Missing members resolve to False."""
    try:
        _provider_id, _model_name, args = resolved_model_kwargs(member_uuid)
    except LookupError:
        return False
    return args_reasoning_on(args)


def rename_model_group(group_uuid: UUID, name: str) -> None:
    g = get_model_group(group_uuid)
    if g is None:
        raise LookupError(f"model_group {group_uuid} not found")
    g.name = name
    db.session.commit()


def delete_model_group(group_uuid: UUID) -> None:
    g = get_model_group(group_uuid)
    if g is None:
        raise LookupError(f"model_group {group_uuid} not found")
    db.session.delete(g)
    db.session.commit()


def get_model_group_member_uuids(group_uuid: UUID) -> list[UUID]:
    rows = (
        db.session.query(ModelGroupMember)
        .filter(ModelGroupMember.group_uuid == group_uuid)
        .order_by(ModelGroupMember.position.asc())
        .all()
    )
    return [r.member_uuid for r in rows]


def _enforce_capability_constraint(
    constraint: str,
    member_uuids: list[UUID],
    member_has_capability: Callable[[UUID], bool],
    label: str,
) -> None:
    """Raise ValueError if any member violates a single capability constraint.
    "dont_care" never rejects; "must_have" rejects members lacking the
    capability; "must_not_have" rejects members that have it."""
    if constraint == "must_have":
        rejected = [m for m in member_uuids if not member_has_capability(m)]
        if rejected:
            raise ValueError(
                f"group requires members that support {label}; rejected: "
                + ", ".join(str(m) for m in rejected)
            )
    elif constraint == "must_not_have":
        rejected = [m for m in member_uuids if member_has_capability(m)]
        if rejected:
            raise ValueError(
                f"group requires members that do not support {label}; rejected: "
                + ", ".join(str(m) for m in rejected)
            )


def set_model_group_members(group_uuid: UUID, member_uuids: list[UUID]) -> None:
    """Replace a group's ordered membership with `member_uuids` (in order).

    Enforces the group's per-capability constraints (function calling, structured
    output, reasoning): a "must_have" constraint rejects members lacking the
    capability, "must_not_have" rejects members that have it, and "dont_care"
    imposes no check. Raises ValueError listing any rejected members."""
    group = get_model_group(group_uuid)
    if group is not None:
        _enforce_capability_constraint(
            group.function_calling_constraint,
            member_uuids,
            member_is_function_calling,
            "function calling",
        )
        _enforce_capability_constraint(
            group.structured_output_constraint,
            member_uuids,
            member_uses_structured_output,
            "structured output",
        )
        _enforce_capability_constraint(
            group.reasoning_constraint,
            member_uuids,
            member_supports_reasoning,
            "reasoning",
        )
    db.session.query(ModelGroupMember).filter(
        ModelGroupMember.group_uuid == group_uuid
    ).delete()
    for pos, m in enumerate(member_uuids):
        db.session.add(
            ModelGroupMember(group_uuid=group_uuid, position=pos, member_uuid=m)
        )
    db.session.commit()


def resolve_member(member_uuid: UUID) -> dict[str, Any]:
    """Resolve a group member uuid to a display dict
    {uuid, kind, provider, model_name, display_name}. Tries the override
    table first, then model_config; 'missing' if neither has it.

    For overrides, `display_name` is the override's effective_display_name
    (user-set name or the synthesized "t0.5 c32k struct" summary) so
    unnamed overrides remain informative. For configs `display_name` is
    empty — the UI renders "(base config)" when it sees that."""
    ov = get_model_config_override(member_uuid)
    if ov is not None:
        parent = get_model_config(ov.model_config_uuid)
        return {
            "uuid": str(member_uuid),
            "kind": "override",
            "provider": parent.provider if parent else "",
            "model_name": parent.model_name if parent else "(missing config)",
            # The parent config's friendly label (its display_name or model_name)
            # — what the UI shows as the model line.
            "model_display_name": (
                parent.effective_display_name if parent else "(missing config)"
            ),
            "display_name": ov.effective_display_name,
            # An override is only usable if its parent config is still available
            # (the model still exists in the provider).
            "available": bool(parent.available) if parent else False,
        }
    cfg = get_model_config(member_uuid)
    if cfg is not None:
        return {
            "uuid": str(member_uuid),
            "kind": "config",
            "provider": cfg.provider,
            "model_name": cfg.model_name,
            "model_display_name": cfg.effective_display_name,
            "display_name": "",
            "available": bool(cfg.available),
        }
    return {
        "uuid": str(member_uuid),
        "kind": "missing",
        "provider": "",
        "model_name": "(missing model)",
        "model_display_name": "(missing model)",
        "display_name": "",
        "available": False,
    }


def ensure_agent_model_bindings(agent_uuids: list[UUID]) -> None:
    """Create an unassigned (model_group_uuid=NULL) binding row for any
    agent_uuid that lacks one. Idempotent — existing bindings are untouched."""
    existing = {b.agent_uuid for b in db.session.query(AgentModelBinding).all()}
    created = False
    for agent_uuid in agent_uuids:
        if agent_uuid not in existing:
            db.session.add(AgentModelBinding(agent_uuid=agent_uuid, model_group_uuid=None))
            created = True
    if created:
        db.session.commit()


def get_agent_model_binding(agent_uuid: UUID) -> AgentModelBinding | None:
    return (
        db.session.query(AgentModelBinding)
        .filter(AgentModelBinding.agent_uuid == agent_uuid)
        .one_or_none()
    )


def list_agent_model_bindings() -> dict[UUID, AgentModelBinding]:
    """All bindings keyed by agent_uuid, for one-pass lookup when rendering."""
    return {b.agent_uuid: b for b in db.session.query(AgentModelBinding).all()}


def set_agent_model_binding(
    agent_uuid: UUID, model_group_uuid: UUID | None
) -> AgentModelBinding:
    """Assign (or clear, with None) the model group a given agent uses. Upserts."""
    row = get_agent_model_binding(agent_uuid)
    if row is None:
        row = AgentModelBinding(agent_uuid=agent_uuid, model_group_uuid=model_group_uuid)
        db.session.add(row)
    else:
        row.model_group_uuid = model_group_uuid
    db.session.commit()
    return row
