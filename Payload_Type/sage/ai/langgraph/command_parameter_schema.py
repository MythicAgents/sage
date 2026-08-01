"""One resolver for every Mythic command-parameter-schema fetch in Sage.

Sage previously built this schema four separate ways — two LLM-facing tools and two internal call
sites — each with its own query string, its own field subset, and its own idea of the return shape.
Two of the four interpolated model-influenced values into GraphQL text, where a malformed query
returns nothing, the fetch returns ``None``, and `_validate_command_parameters` logs
``failed_open ... reason=no_schema`` and permits the task. Consolidating removes that class by
construction rather than patching its instances.

Three properties this module owes its callers:

* **Grouped, not flat.** Mythic already knows which parameters are mutually exclusive. Returning
  ``{group: {"example": ..., "parameters": [...]}}`` hands the model a choice *between* groups
  instead of a bag it must re-partition from a `parameter_group_name` field. Structure beats prose
  for control authority.
* **Never raises.** Every failure — unknown command, absent client, unresolved payload type, an
  exception from upstream's example renderer — becomes ``None`` or a degraded result. Validation
  that cannot run must fail open loudly in the log, never take a task down.
* **Bounded output.** The raw upstream payload is 19 fields per parameter and peaks at 11,741 chars
  for Apollo's `sc`, against a 4,000-char compaction trigger. Field selection is mandatory, and the
  raw payload never reaches the model.

Measured against live Apollo (81 commands, 192 parameters) on 2026-08-01: raw output breaches 4,000
chars for 9 of 81 commands; with the selection below and examples kept, 1 of 81; with examples
dropped, 0 of 81.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Fields forwarded to the model, from the 19 upstream returns. `LLM_Help` is upstream-authored
#: text present only on CredentialJson/AgentConnect/LinkInfo parameters, and it is the reason this
#: work exists — it carries the exact `@cred:` / `@link:` reference form Mythic expects, at the
#: moment the model is choosing a value rather than after a rejected task.
MODEL_VISIBLE_FIELDS = (
    "name",
    "type",
    "required",
    "description",
    "default_value",
    "choices",
    "parameter_group_name",
    "verifier_regex",
    "LLM_Help",
)

#: Mirrors upstream's own query, minus the example renderer. Parameterized, never interpolated:
#: the two values that vary are GraphQL variables, so a hostile or malformed command name cannot
#: reshape the query. This exists so an upstream renderer exception degrades to
#: parameters-without-example instead of losing the schema entirely — the renderer has shipped a
#: call-killing exception in every release that contained it (rc5 KeyError, rc7 KeyError +
#: JSONDecodeError, rc8 ValueError), so Sage does not wait on an upstream fix to be safe.
_PARAMETERS_ONLY_QUERY = """
query SageCommandParameters($command_name: String!, $payload_type_name: String!) {
    commandparameters(
        where: {
            command: {
                cmd: {_eq: $command_name},
                payloadtype: {name: {_eq: $payload_type_name}},
                deleted: {_eq: false}
            }
        },
        order_by: [
            {parameter_group_name: asc},
            {ui_position: asc},
            {id: asc}
        ]
    ) {
        choices
        cli_name
        default_value
        description
        name
        parameter_group_name
        required
        type
        verifier_regex
    }
}
"""

#: Fields `_validate_command_parameters` reads. `cli_name` is the one the model never needs and
#: validation cannot work without: it builds its accepted-key set from BOTH `name` and `cli_name`,
#: so a projection that dropped it would reject every CLI-named parameter as unknown and block real
#: tasks. `default_value` is deliberately absent — the old query fetched it and validation never
#: read it.
VALIDATION_FIELDS = (
    "name",
    "cli_name",
    "type",
    "required",
    "choices",
    "parameter_group_name",
    # Carried for the ISC-4 rejection arm, which is off by default. Measured 2026-08-01: empty on
    # all 192 parameters of every installed payload type, so it costs nothing today and is here for
    # the agent-agnostic case rather than for Apollo.
    "verifier_regex",
)

#: Values that carry no information for the model. Dropping them is most of the size win, and it
#: keeps an empty string from reading as a configured default.
#:
#: `False` and `0` survive on purpose: `x not in _EMPTY` compares by equality, and `False == ""` is
#: False, so a `required: False` is preserved rather than silently dropped. Validation depends on
#: that — a missing `required` and an explicit `False` happen to behave alike there today, but
#: relying on the coincidence would be fragile.
_EMPTY = ("", [], {}, None)


#: Exactly the seven fields the pre-consolidation internal query selected. `_fetch_command_schema`
#: has nine production callers and is stubbed in roughly seventy tests, all of which expect this
#: shape, so it is a compatibility contract rather than a preference.
INTERNAL_SCHEMA_FIELDS = (
    "name",
    "cli_name",
    "type",
    "parameter_group_name",
    "required",
    "choices",
    "default_value",
    # Eighth field, added for the ISC-4 rejection arm. Purely additive: no caller breaks by gaining
    # a key, and validation reads its groups through this projection, so the arm cannot see a regex
    # the internal path has stripped.
    "verifier_regex",
)


def _select_fields(
    parameter: dict[str, Any],
    fields: tuple[str, ...] = MODEL_VISIBLE_FIELDS,
    *,
    drop_empty: bool = True,
) -> dict[str, Any]:
    """Project a parameter onto `fields`.

    ``drop_empty`` exists for the internal path only. Dropping empties is most of the size win for
    model-facing output, but the old internal query always returned all seven keys, and a caller
    doing `param["choices"]` rather than `param.get("choices")` would start raising KeyError if
    they vanished. Preserving them there costs nothing the model ever sees.
    """
    return {
        field: parameter[field]
        for field in fields
        if field in parameter and (not drop_empty or parameter[field] not in _EMPTY)
    }


def _normalize(
    grouped: dict[str, Any],
    *,
    include_example: bool,
    fields: tuple[str, ...] = MODEL_VISIBLE_FIELDS,
    drop_empty: bool = True,
) -> dict[str, dict[str, Any]]:
    """Apply field selection to upstream's grouped payload, preserving the group structure.

    A group with no parameters is preserved rather than dropped: a zero-parameter command really
    does return ``{"Default": {"example": ..., "parameters": []}}``, and that empty list is its
    single valid tasking form, not an absence.
    """
    normalized: dict[str, dict[str, Any]] = {}
    for group_name, group in grouped.items():
        # rc6 returned a bare list per group; rc7 onward returns a dict. Tolerate both so a pin
        # change cannot silently produce an empty schema that fails open on every task.
        if isinstance(group, list):
            parameters, example = group, ""
        else:
            parameters = group.get("parameters") or []
            example = group.get("example") or ""

        entry: dict[str, Any] = {
            "parameters": [
                _select_fields(parameter, fields, drop_empty=drop_empty)
                for parameter in parameters
            ]
        }
        if include_example and example:
            entry["example"] = example
        normalized[group_name] = entry
    return normalized


def group_flat_parameters(
    rows: list[dict[str, Any]] | None,
    *,
    fields: tuple[str, ...] = MODEL_VISIBLE_FIELDS,
    drop_empty: bool = True,
) -> dict[str, dict[str, Any]]:
    """Group an already-fetched flat parameter list, applying the same field selection.

    For callers that legitimately hold the rows already. `get_all_commands_for_payloadtype` fetches
    all 81 Apollo commands in a single upstream call; routing each through the per-command resolver
    would be 81 round trips to learn what one already returned. What that caller needs from this
    module is the *policy* — one definition of grouping and of which fields the model sees — not a
    second fetch. Sharing the policy is the consolidation; duplicating the fetch would be the
    opposite.

    Note the deliberate asymmetry: `example` and `LLM_Help` are produced by upstream's per-command
    renderer and are therefore unavailable on this path. Depth (including the `@cred:`/`@link:`
    reference forms) belongs to the single-command tool; this one is breadth.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows or []:
        grouped.setdefault(row.get("parameter_group_name") or "Default", []).append(row)
    return _normalize(grouped, include_example=False, fields=fields, drop_empty=drop_empty)


def flatten_groups(groups: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Collapse a grouped schema back to the flat list the internal call sites expect.

    Shared so that `_fetch_command_schema` and the live-surface path, which populate the same
    `_cmd_schema_cache`, cannot drift into producing different shapes for the same key. They did
    not share a definition before, and the cache is read by whichever ran first.
    """
    return [
        parameter
        for group in (groups or {}).values()
        for parameter in group.get("parameters", [])
    ]


async def _fetch_parameters_only(
    client: Any, payload_type: str, command: str
) -> dict[str, dict[str, Any]] | None:
    """Rebuild the grouped shape from the raw parameter rows, with no example rendering."""
    from mythic import mythic_utilities

    response = await mythic_utilities.graphql_post(
        mythic=client,
        query=_PARAMETERS_ONLY_QUERY,
        variables={"command_name": command, "payload_type_name": payload_type},
    )
    rows = (response or {}).get("commandparameters")
    if not rows:
        # Zero rows is ambiguous and must not be resolved optimistically: a command that does not
        # exist and a real command with no parameters are indistinguishable here, because this
        # query selects parameters, not commands. Returning an empty "Default" group would let an
        # unknown command validate against an empty schema — strictly worse than the fail-open
        # behaviour this resolver replaced, since the caller would believe it had a schema.
        # `None` means "schema unavailable", and the caller fails open loudly.
        #
        # This costs nothing real: the primary path resolves zero-parameter commands correctly
        # (live `exit` returns its Default group), so this branch is reached only when the example
        # renderer already crashed, and a zero-parameter command has nothing to validate anyway.
        return None

    grouped: dict[str, Any] = {}
    for row in rows:
        group = grouped.setdefault(row.get("parameter_group_name") or "Default", {"parameters": []})
        group["parameters"].append(row)
    return grouped


class CommandSchemaResolver:
    """Caches one schema per (payload type, command) for the life of the resolver.

    Cached because `_fetch_live_command_surface` walks every loaded command on a callback, which
    turns one schema fetch into dozens of identical round trips, and because context cost is the
    binding constraint this work exists to reduce.
    """

    def __init__(self) -> None:
        # Stores the RAW upstream shape, not a projection. Two consumers want different fields from
        # the same fetch — the model must not see `cli_name`, and validation cannot work without it
        # — so projecting before caching would force either a second round trip or a cache key per
        # field set. Cache the source, project on read.
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._cache.clear()

    async def get(
        self,
        client: Any,
        payload_type: str | None,
        command: str | None,
        *,
        include_example: bool = True,
        use_cache: bool = True,
        fields: tuple[str, ...] = MODEL_VISIBLE_FIELDS,
        drop_empty: bool = True,
    ) -> dict[str, dict[str, Any]] | None:
        """Grouped, field-selected schema for one command, or ``None`` if it cannot be resolved.

        ``None`` is the only failure signal. Callers treat it as "schema unavailable" and fail
        open; nothing here raises, because a validator that cannot answer must not block a task.

        ``fields`` selects the projection: :data:`MODEL_VISIBLE_FIELDS` for anything the model
        reads, :data:`VALIDATION_FIELDS` for `_validate_command_parameters`.
        """
        if client is None or not payload_type or not command:
            return None

        key = (payload_type, command)
        if use_cache and key in self._cache:
            return _normalize(
                self._cache[key],
                include_example=include_example,
                fields=fields,
                drop_empty=drop_empty,
            )

        from mythic import mythic

        grouped: dict[str, Any] | None = None
        try:
            grouped = await mythic.get_command_parameter_options(
                mythic=client, command_name=command, payload_type_name=payload_type
            ) or {}
        except Exception as exc:
            # Two very different causes land here and only one is worth a second attempt: an
            # unknown command (upstream raises a bare Exception naming it) is genuinely absent,
            # while a renderer defect means the parameters exist and only the prose failed. The
            # fallback is cheap and distinguishes them by outcome rather than by parsing the
            # message, which would be a prose-classification gate of exactly the kind Sage avoids.
            logger.info(
                "command schema: grouped fetch failed for %s/%s (%s); trying parameters-only",
                payload_type,
                command,
                type(exc).__name__,
            )
            try:
                grouped = await _fetch_parameters_only(client, payload_type, command)
            except Exception as fallback_exc:
                logger.info(
                    "command schema: parameters-only fetch also failed for %s/%s (%s)",
                    payload_type,
                    command,
                    type(fallback_exc).__name__,
                )
                grouped = None

        if grouped is None:
            return None

        if use_cache:
            self._cache[key] = grouped
        return _normalize(
            grouped, include_example=include_example, fields=fields, drop_empty=drop_empty
        )


#: Process-wide default. Callers may construct their own for isolation in tests.
DEFAULT_RESOLVER = CommandSchemaResolver()


async def get_command_schema(
    client: Any,
    payload_type: str | None,
    command: str | None,
    *,
    include_example: bool = True,
    use_cache: bool = True,
    fields: tuple[str, ...] = MODEL_VISIBLE_FIELDS,
    drop_empty: bool = True,
) -> dict[str, dict[str, Any]] | None:
    """Module-level entry point over :data:`DEFAULT_RESOLVER`."""
    return await DEFAULT_RESOLVER.get(
        client,
        payload_type,
        command,
        include_example=include_example,
        use_cache=use_cache,
        fields=fields,
        drop_empty=drop_empty,
    )
