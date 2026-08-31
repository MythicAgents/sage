"""Live driver: bare-model vs harness on GOAD, scored by ONE shared ground-truth ruler.

⚠️  RUNS LIVE OFFENSIVE TOOLING with `--go`. Each run mutates the range, so a FAIR comparison requires a
    CLEAN RESET BEFORE EVERY RUN (your row, via `sage-goad-reset`). Therefore this driver runs exactly
    ONE side on ONE scenario per `--go` invocation and RECORDS the ScoreCard; you reset between runs;
    then `compare` combines the records. Safe to import / dry-run; only `--go` acts.

Both sides are scored by the SAME ledger-independent probes (collection-independent where it matters):
  * KRBTGT_DUMPED -> Mythic loot (mythic_credential_probe): FAIR — reflects what was actually dumped.
  * DA_CHILD / OBJECTIVE -> BloodHound cypher: COLLECTION-BIASED (favors Sage, which ingests BloodHound);
    treat as provisional until an AD-direct probe replaces them.

Operator loop per scenario:
  reset -> `run --go --side harness --scenario child-da`
  reset -> `run --go --side bare    --scenario child-da`
  `compare --scenario child-da`
"""
from __future__ import annotations

import argparse
import math
import os
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

try:
    from . import (gate1p_protocol, gate1p_stagea, gate1p_stageb, live_seams, bare_runner,
                   bare_mythic_tools, bare_bloodhound, probes as probes_mod)
    from .scenarios import all_scenarios, CHILD, CHILD_DC, OBJECTIVE
    from .range_state import Milestone
    from .fitness import ScoreCard
except Exception:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai.hillclimb import (  # type: ignore
        gate1p_stagea,
        gate1p_stageb,
        gate1p_protocol,
        live_seams,
        bare_runner,
        bare_mythic_tools,
        bare_bloodhound,
        probes as probes_mod,
    )
    from ai.hillclimb.scenarios import all_scenarios, CHILD, CHILD_DC, OBJECTIVE  # type: ignore
    from ai.hillclimb.range_state import Milestone  # type: ignore
    from ai.hillclimb.fitness import ScoreCard  # type: ignore


@dataclass
class Config:
    sage_cb: int | None = None  # legacy payload path only
    apollo_cb: int = 4
    engagement_op: str = "Operation_Chimera_1"
    max_steps: int = 0          # 0 = UNLIMITED steps for the bare model (parity with Sage's solve)
    solve_timeout: int = 1800   # also the bare loop's wall-clock ceiling (not a step/recursion cap)
    # AD-direct DA probes poll for up to this long (re-reading every interval) so GPO/SYSTEM-on-DC
    # membership changes have time to propagate before scoring — returns True the instant they appear.
    da_settle_timeout: int = 300
    da_settle_interval: int = 20
    policy_mode: str = "llm"
    model_provider: str | None = None
    model_id: str | None = None
    model_api_endpoint: str | None = None
    model_api_key: str | None = None
    null_model: bool = False
    measurement_kind: str = "legacy"  # legacy | diagnostic | row
    protocol_id: str | None = None
    pair_id: str | None = None
    arm_index: int | None = None
    model_token_budget: int = gate1p_stagea.TOKEN_ADMISSION_CEILING
    model_context_limit: int | None = None
    max_output_tokens: int | None = None
    source_version: str | None = None
    live_authority_id: str | None = None

    @property
    def results_path(self) -> Path:
        override = str(os.environ.get("SAGE_EVAL_RESULTS_PATH") or "").strip()
        if override:
            return Path(override).expanduser()
        return Path(__file__).resolve().parents[2] / ".hillclimb" / "results" / "bare_vs_harness.jsonl"

    @property
    def artifact_dir(self) -> Path | None:
        value = str(os.environ.get("SAGE_EVAL_ARTIFACT_DIR") or "").strip()
        return Path(value).expanduser() if value else None


def _gate1p_arm_artifact_path(cfg: Config, arm: str) -> Path:
    if cfg.artifact_dir is None:
        raise gate1p_stagea.ProtocolError("SAGE_EVAL_ARTIFACT_DIR is required for paired rows")
    return cfg.artifact_dir / f"{arm}-episode.json"


def _private_history_root() -> Path:
    return Path(__file__).resolve().parents[4] / ".sage_history"


def _validate_gate1p_artifact_destinations(cfg: Config, arm: str) -> Path:
    artifact = _gate1p_arm_artifact_path(cfg, arm)
    history_root = _private_history_root()
    try:
        artifact.resolve().relative_to(history_root.resolve())
        cfg.results_path.resolve().relative_to(history_root.resolve())
    except ValueError as exc:
        raise gate1p_stagea.ProtocolError(
            "paired-row result and artifact destinations must be under the repo's private .sage_history"
        ) from exc
    artifact.parent.mkdir(parents=True, exist_ok=True)
    probe = artifact.parent / f".gate1p-write-probe-{os.getpid()}"
    try:
        probe.write_text("write-probe\n", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)
    return artifact


def _write_gate1p_episode_artifact(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        raise gate1p_stagea.ProtocolError(f"cannot read the paired-row ledger: {exc}") from exc


def _validate_gate1p_pair_control(
    cfg: Config,
    arm: str,
    *,
    protocol: gate1p_protocol.Gate1PProtocol,
) -> None:
    """Require the selected protocol's orchestrator control before any row spend."""
    module = protocol.module
    error_type = module.ProtocolError
    expected_pair_id = str(cfg.pair_id or "").strip()
    arms = tuple(protocol.arms)
    if len(arms) != 2 or arm not in arms or expected_pair_id not in protocol.pair_ids:
        raise error_type("pair orchestrator control identity is not allocated by this protocol")
    raw = str(os.environ.get("SAGE_EVAL_PAIR_CONTROL_PATH") or "").strip()
    if not raw or cfg.artifact_dir is None:
        raise error_type(
            "paired rows may run only as children of the Gate 1P pair orchestrator"
        )
    path = Path(raw).expanduser()
    history_root = _private_history_root()
    try:
        path.resolve().relative_to(cfg.artifact_dir.resolve())
        path.resolve().relative_to(history_root.resolve())
    except ValueError as exc:
        raise error_type("pair control state must be retained in the pair artifact directory") from exc
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise error_type(f"pair orchestrator control state is missing or unreadable: {exc}") from exc
    control_fields = {
        "schema", "protocol_id", "pair_id", "source_version", "next_arm",
        "completed_arms", "prearm_state", "created_at", "updated_at",
    }
    finite_number = lambda value: (  # noqa: E731 - bounded local schema predicate
        type(value) in (int, float) and math.isfinite(float(value))
    )
    expected_completed = [] if arm == arms[0] else [arms[0]]
    if (
        not isinstance(state, dict)
        or set(state) != control_fields
        or state.get("schema") != getattr(
            module, "PAIR_CONTROL_SCHEMA", "gate1p-stagea-pair-control-v3"
        )
        or state.get("protocol_id") != protocol.protocol_id
        or state.get("pair_id") != expected_pair_id
        or type(state.get("source_version")) is not str
        or not state.get("source_version")
        or state.get("source_version") != cfg.source_version
        or state.get("next_arm") != arm
        or type(state.get("completed_arms")) is not list
        or state.get("completed_arms") != expected_completed
        or not finite_number(state.get("created_at"))
        or not finite_number(state.get("updated_at"))
        or float(state["updated_at"]) < float(state["created_at"])
    ):
        raise error_type("pair orchestrator control state does not admit this exact arm")
    prearm_state = state.get("prearm_state")
    if (
        not isinstance(prearm_state, dict)
        or set(prearm_state) != {"arm", "status", "reset_generation", "updated_at"}
        or prearm_state.get("arm") != arm
        or prearm_state.get("status") != "reset_started"
        or type(prearm_state.get("reset_generation")) is not int
        or prearm_state.get("reset_generation") <= 0
        or not finite_number(prearm_state.get("updated_at"))
        or float(prearm_state["updated_at"]) < float(state["created_at"])
        or float(prearm_state["updated_at"]) > float(state["updated_at"])
    ):
        raise error_type("pair control does not prove reset start for this exact arm")
    records = _read_jsonl(cfg.results_path)
    lifecycle_types = {"arm_started", "result", "cleanup"}
    existing_current = [
        row for row in records
        if row.get("record_type") in lifecycle_types
        and (row.get("protocol") or {}).get("pair_id") == expected_pair_id
        and (row.get("protocol") or {}).get("arm") == arm
    ]
    if existing_current:
        raise error_type(f"retry-zero lifecycle already consumed arm {arm!r}")
    if arm == arms[0]:
        any_started = [
            row for row in records
            if row.get("record_type") in lifecycle_types
            and (row.get("protocol") or {}).get("pair_id") == expected_pair_id
        ]
        if any_started or state.get("completed_arms") != []:
            raise error_type("shipping must be the first and only unconsumed pair arm")
    else:
        if state.get("completed_arms") != [arms[0]]:
            raise error_type("naive arm requires the completed shipping lifecycle")
        disposition_kwargs = {"arm": arms[0], "history_root": history_root}
        if protocol.pair_id is None:
            disposition_kwargs["pair_id"] = expected_pair_id
        shipping = module.completed_arm_lifecycle_disposition(records, **disposition_kwargs)
        if shipping.get("countable") is not True:
            raise error_type(
                f"naive arm blocked by shipping admission: {shipping.get('reason')}"
            )


def _resolved_harness_model_route(cfg: Config) -> dict[str, str | None]:
    """Resolve the route the harness should use when no explicit treatment override is supplied.

    Native chat gets its defaults from Sage's local env-backed metadata. The headless controller path bypasses
    that channel metadata entirely, so it must carry the same provider/model/endpoint/key explicitly or it
    constructs ``Model`` with an empty config and fails before the first controller cycle.
    """
    defaults = live_seams.load_sage_defaults()
    return {
        "provider": cfg.model_provider or defaults.get("provider"),
        "model": cfg.model_id or defaults.get("model"),
        "api_endpoint": cfg.model_api_endpoint or defaults.get("base_url"),
        "api_key": cfg.model_api_key or defaults.get("api_key"),
    }


def _stageb_proof_adapters(
    reader,
    baseline: dict,
    *,
    objective_domain: str,
    directory_probe: Callable[[], bool] | None = None,
):
    baseline_members = set(baseline.get(objective_domain, set()))

    def directory_delta() -> bool:
        return bool(set(reader(objective_domain)) - baseline_members)

    def typed(name: str, probe: Callable[[], bool], *, require_fresh: bool = True):
        def read() -> bool:
            try:
                value = probe()
            except live_seams.ProbeReadError as exc:
                raise gate1p_stageb.ProofUnavailable(str(exc)) from exc
            if type(value) is not bool:
                raise gate1p_stagea.ObserverUnavailable(
                    f"malformed {name} proof baseline: Boolean state required"
                )
            return value

        baseline_available = True
        baseline_value = False
        if require_fresh:
            try:
                baseline_value = read()
            except gate1p_stageb.ProofUnavailable:
                baseline_available = False

        def observe() -> bool:
            if not baseline_available:
                raise gate1p_stageb.ProofUnavailable(
                    f"{name} proof baseline was unavailable"
                )
            current = read()
            return current and not baseline_value if require_fresh else current

        return gate1p_stageb.make_proof_adapter(name, observe)

    return (
        typed("directory_delta", directory_probe or directory_delta, require_fresh=False),
        typed(
            "certificate_control",
            live_seams.certificate_admin_control_probe(None, realm=objective_domain),
        ),
        typed("ticket_control", live_seams.ticket_admin_control_probe(realm=objective_domain)),
        typed("krbtgt_control", live_seams.krbtgt_dumped_probe(realm=objective_domain)),
        typed(
            "system_on_child_dc",
            live_seams.system_execution_probe(realm=objective_domain, host=CHILD_DC),
        ),
        typed(
            "remote_exec_child_dc",
            live_seams.remote_execution_probe(CHILD_DC, realm=objective_domain),
        ),
    )


def build_probes(reader, baseline: dict, scenario, *, settle_timeout: float = 0,
                 settle_interval: float = 20) -> dict:
    """Build ledger-independent range probes shared by harness and bare-model runs.

    The scorer consumes typed probe callables, not a protocol. SYSTEM/KRBTGT and task-backed objective
    proofs come from Mythic control-plane records. Directory-membership proof uses the injected,
    logically observation-only evaluation-plane reader, which may use any contract-declared protocol.
    """
    sub = set(getattr(scenario, "milestone_subset", None) or tuple(Milestone))
    domains = dict(getattr(scenario, "domains", {}) or {})
    child_domain = str(domains.get("child") or CHILD)
    objective_domain = str(domains.get("objective") or OBJECTIVE)
    probes = {}
    if Milestone.SYSTEM_ON_HOST in sub:
        probes[Milestone.SYSTEM_ON_HOST] = live_seams.system_execution_probe(realm=child_domain)
    if Milestone.KRBTGT_DUMPED in sub:
        probes[Milestone.KRBTGT_DUMPED] = live_seams.krbtgt_dumped_probe(realm=child_domain)
    if Milestone.DA_CHILD in sub:
        probes[Milestone.DA_CHILD] = live_seams.ad_domain_admins_probe_via_reader(
            reader, child_domain, baseline=baseline.get(child_domain, set()),
            settle_timeout=settle_timeout, settle_interval=settle_interval)
    if Milestone.OBJECTIVE in sub:
        if getattr(scenario, "name", "") == gate1p_stageb.SCENARIO:
            objective_oracle = gate1p_stageb.build_objective_oracle(
                _stageb_proof_adapters(reader, baseline, objective_domain=objective_domain),
                artifact_pointer="unretained:stageb-probe-assembly",
                domain=objective_domain,
            )
            probes[Milestone.OBJECTIVE] = live_seams.any_probe(
                lambda: bool(objective_oracle()["success"]),
                settle_timeout=settle_timeout,
                settle_interval=settle_interval,
            )
        elif _objective_uses_remote_exec_probe(scenario):
            remote_targets = _objective_remote_exec_targets(scenario)
            if len(remote_targets) != 1:
                raise ValueError(
                    "remote-exec OBJECTIVE scenarios must declare exactly one host/domain target"
                )
            target_host, target_domain = remote_targets[0]
            target_domain = target_domain or objective_domain
            probes[Milestone.OBJECTIVE] = live_seams.any_probe(
                live_seams.remote_execution_probe(target_host, realm=target_domain),
                settle_timeout=settle_timeout,
                settle_interval=settle_interval,
            )
        else:
            probes[Milestone.OBJECTIVE] = live_seams.any_probe(
                live_seams.certificate_admin_control_probe(
                    None if getattr(scenario, "name", "") == gate1p_stagea.SCENARIO else "administrator",
                    realm=objective_domain,
                ),
                live_seams.ticket_admin_control_probe(realm=objective_domain),
                live_seams.ad_domain_admins_probe_via_reader(
                    reader,
                    objective_domain,
                    baseline=baseline.get(objective_domain, set()),
                    settle_timeout=0,
                ),
                settle_timeout=settle_timeout,
                settle_interval=settle_interval,
            )
    if Milestone.GRAPH_COLLECTED in sub:
        # Ground-truth, ledger-independent: BloodHound holds >=1 ingested Domain == a SharpHound
        # collection was successfully run AND uploaded. Independent of Sage's self-reported
        # `graph-built:` predicate, so range_state's disagreement check ("who verifies the verifier")
        # can finally catch a collection self-report that reality does not back. Scenarios that do not
        # score GRAPH_COLLECTED (e.g. child-da) never build this probe.
        probes[Milestone.GRAPH_COLLECTED] = live_seams.graph_collected_probe()
    return probes


def _scenario(cfg: Config, name: str):
    scenarios = all_scenarios(cfg.engagement_op)
    for s in scenarios:
        if s.name == name:
            return s
    raise SystemExit(f"unknown scenario {name!r}; choices: {[s.name for s in scenarios]}")


def objective_recognizable(objective: str) -> tuple[bool, str]:
    """Cheap pre-run contract check: is completion-recognition REACHABLE for this objective?

    Guards the harness->Sage objective seam that regressed in the Phase-0 re-set (the gauge's read-only/
    seam-injected architecture made the seam untestable offline, so the only detector was a ~60-min live
    range run). The HARD contract is the exact thing that regressed: the objective Sage runs with must NOT
    be blank/opaque (`sage-engagement:*`), since engagement_state._objective_is_complete deliberately never
    completes an opaque objective. Target-domain parse is reported as a DIAGNOSTIC only, NOT a failure:
    objectives like single-hop-system ("SYSTEM on a host") and cross-forest-objective ("control of THE
    objective domain X") legitimately have no DA-phrase target and complete via the no-next-hop/milestone
    fallback — gating on target-parse would wrongly abort valid scenarios. Returns (ok, reason)."""
    o = str(objective or "").strip()
    if not o or o.casefold().startswith("sage-engagement"):
        return False, f"objective is empty/opaque (completion-recognition unreachable): {o!r}"
    try:
        from ..langgraph import engagement_state as _es
    except Exception:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
        import engagement_state as _es  # type: ignore
    targets = _es._objective_target_domains(o)
    if targets:
        return True, f"recognizable: target-matched completion (targets={sorted(targets)})"
    remote_exec_targets = _es._objective_remote_exec_targets(o)
    if remote_exec_targets:
        return True, f"recognizable: remote-exec target-matched completion (targets={remote_exec_targets})"
    return True, "recognizable: non-opaque; completion via no-next-hop/milestone fallback (no DA-phrase target)"


def _scored_referee_domains(scn) -> set:
    """AD-direct domains a scenario SCORES (so the run needs out-of-band referee LDAP creds for each).
    Mirrors run_side's baseline scope EXACTLY, so the preflight validates precisely the creds the run will
    require — no more, no less."""
    sub = set(getattr(scn, "milestone_subset", None) or tuple(Milestone))
    domains = dict(getattr(scn, "domains", {}) or {})
    scored: set[str] = set()
    if Milestone.DA_CHILD in sub:
        child = str(domains.get("child") or "").strip()
        if child:
            scored.add(child)
    if Milestone.OBJECTIVE in sub and (
        getattr(scn, "name", "") == gate1p_stageb.SCENARIO
        or not _objective_uses_remote_exec_probe(scn)
    ):
        objective = str(domains.get("objective") or "").strip()
        if objective:
            scored.add(objective)
    return scored


def _objective_effect_prefixes(scenario) -> tuple[str, ...]:
    try:
        spec = scenario.spec()
    except Exception:
        return ()
    objective_spec = spec.get(Milestone.OBJECTIVE)
    return tuple(str(prefix or "").strip().casefold() for prefix in getattr(objective_spec, "effect_prefixes", ()) or ())


def _objective_uses_remote_exec_probe(scenario) -> bool:
    return "remote-exec:" in _objective_effect_prefixes(scenario)


def _objective_remote_exec_targets(scenario) -> list[tuple[str, str]]:
    try:
        from ..langgraph import engagement_state as _es
    except Exception:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "langgraph"))
        import engagement_state as _es  # type: ignore
    return list(_es._objective_remote_exec_targets(str(getattr(scenario, "objective", "") or "")))


def _referee_creds_present(domain: str) -> tuple[bool, str]:
    """OFFLINE check: the referee config has dc_ip+user+password for `domain` (reads the JSON config; does
    NOT bind to LDAP). This is the cheap tier of the cred precondition — the live tier (can we actually bind
    to the DC) genuinely needs the range up, so only the offline tier is hoisted before the reset."""
    try:
        from . import live_seams as _ls
    except Exception:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import live_seams as _ls  # type: ignore
    try:
        _ls.referee_domain_entry(domain, config=_ls.load_referee_ldap_config())
        return True, f"referee creds present for {domain}"
    except Exception as e:
        return False, str(e)


def scenario_preconditions(cfg: "Config", scenario_name: str) -> list:
    """DECLARED preconditions for a scenario — the manifest the preflight ITERATES (vs a hand-maintained
    allowlist). Each entry is (name, cost, check) where check()->(ok, detail) and cost is:
      'offline' — validatable with no lab/reset (hoisted before any expensive step), or
      'live'    — genuinely needs the running range (cannot be hoisted; runs in-path).
    The fail-cheap-before-expensive contract: declare a new precondition HERE, never validate it lazily at
    point-of-use inside the post-reset path. New preconditions are then gated automatically — no one has to
    "remember to add it to preflight" after the next incident."""
    scn = _scenario(cfg, scenario_name)
    checks: list = [("objective-recognizable", "offline", lambda: objective_recognizable(scn.objective))]
    # 'smoke' tier: needs Mythic/BloodHound UP (but NOT the GOAD range), so still hoistable before the
    # expensive reset+solve. Fail-CLOSED validity checks that run the EXACT probe queries against the live
    # schema — a GraphQL field typo / down service fails in ~5s here instead of after a ~2h solve (the
    # 2026-06-21 `responses{response}` bug). All scenarios score KRBTGT_DUMPED (Mythic); only graph-scoring
    # scenarios use the BloodHound REST path.
    checks.append(("mythic-queries-valid", "smoke", lambda: live_seams.mythic_queries_valid()))
    if cfg.model_provider or cfg.model_id:
        route = _resolved_harness_model_route(cfg)
        checks.append((
            "llm-endpoint-authenticated",
            "smoke",
            lambda route=route: live_seams.model_route_auth_smoke(
                route.get("provider"),
                route.get("model"),
                api_key=route.get("api_key"),
                base_url=route.get("api_endpoint"),
                max_output_tokens=cfg.max_output_tokens,
            ),
        ))
    sub = set(getattr(scn, "milestone_subset", None) or tuple(Milestone))
    if Milestone.GRAPH_COLLECTED in sub:
        checks.append(("bloodhound-reachable", "smoke", lambda: live_seams.bloodhound_reachable()))
    if scenario_name in _GATE1P_PRIMARY_SCENARIOS:
        checks.append((
            "bloodhound-raw-surface-valid",
            "smoke",
            lambda: live_seams.bloodhound_raw_surface_valid(),
        ))
    for d in sorted(_scored_referee_domains(scn)):
        checks.append((f"referee-creds:{d}", "offline", (lambda d=d: _referee_creds_present(d))))
    return checks


def validate_harness_runtime_telemetry(
    configured_policy_mode: str,
    telemetry: dict,
    *,
    configured_provider: str | None = None,
    configured_model: str | None = None,
    require_all_response_provenance: bool = True,
    require_complete_usage: bool = False,
    expected_token_budget: int | None = None,
    expected_model_context_limit: int | None = None,
    expected_max_output_tokens: int | None = None,
) -> None:
    """Reject mislabeled or incompletely attributed harness runs."""
    if not isinstance(telemetry, dict) or not telemetry:
        raise RuntimeError("Sage returned no observed runtime telemetry")
    observed = str(telemetry.get("policy_mode") or "").strip().casefold()
    configured = str(configured_policy_mode or "").strip().casefold()
    if observed != configured:
        raise RuntimeError(
            f"configured policy mode {configured!r} did not match observed mode {observed!r}"
        )
    if str(telemetry.get("configured_policy_mode") or "").strip().casefold() != configured:
        raise RuntimeError("runtime telemetry did not preserve the configured policy mode")
    switches = telemetry.get("policy_switches")
    if not isinstance(switches, list):
        raise RuntimeError("runtime telemetry omitted policy switch records")
    if switches or telemetry.get("policy_identity_valid") is not True:
        raise RuntimeError(f"runtime policy identity invalid; switches={switches!r}")
    for label, expected, key in (
        ("provider", configured_provider, "model_provider"),
        ("model", configured_model, "model_id"),
    ):
        if not expected:
            continue
        actual = str(telemetry.get(key) or "").strip()
        if actual.casefold() != str(expected).strip().casefold():
            raise RuntimeError(
                f"configured {label} {expected!r} did not match observed {label} {actual!r}"
            )
    total = int(telemetry.get("semantic_transaction_count", 0) or 0)
    authorized = int(telemetry.get("authorized_transaction_count", 0) or 0)
    coverage = float(telemetry.get("semantic_policy_coverage", 0.0) or 0.0)
    if total < 0 or authorized < 0 or authorized > total:
        raise RuntimeError(
            f"invalid semantic transaction counts: authorized={authorized}, total={total}"
        )
    expected_coverage = authorized / total if total else 1.0
    if abs(coverage - expected_coverage) > 1e-9:
        raise RuntimeError(
            f"semantic policy coverage {coverage} disagrees with counts "
            f"{authorized}/{total}"
        )
    if authorized != total:
        raise RuntimeError(
            f"semantic policy provenance incomplete: {authorized}/{total} transactions authorized"
        )
    model_calls = int(telemetry.get("model_calls", 0) or 0)
    if require_all_response_provenance and configured in {"llm", "hybrid"} and model_calls:
        backend_requests = telemetry.get("effective_backend_requests")
        if not isinstance(backend_requests, list):
            raise RuntimeError("runtime telemetry omitted effective backend request records")
        if len(backend_requests) != model_calls:
            raise RuntimeError(
                f"effective backend provenance incomplete: {len(backend_requests)}/{model_calls} model responses"
            )
        for request in backend_requests:
            if not isinstance(request, dict):
                raise RuntimeError("runtime telemetry contains a malformed effective backend record")
            effective_backend = str(request.get("effective_backend") or "").strip()
            if not effective_backend:
                raise RuntimeError("runtime telemetry omitted the response-derived effective backend")
            if configured_provider and configured_model:
                expected_backend = f"{configured_provider}:{configured_model}"
                if effective_backend.casefold() != expected_backend.casefold():
                    raise RuntimeError(
                        "response-derived effective backend differs from the declared route"
                    )
            if str(request.get("backend_provenance_source") or "").strip() in {"", "unavailable"}:
                raise RuntimeError("runtime telemetry omitted the effective backend provenance source")
        if telemetry.get("backend_provenance_complete") is not True:
            raise RuntimeError("runtime telemetry marked effective backend provenance incomplete")
    if require_complete_usage:
        records = telemetry.get("model_response_records")
        if not isinstance(records, list) or len(records) != model_calls:
            raise RuntimeError(
                f"model response usage incomplete: {len(records) if isinstance(records, list) else 0}/"
                f"{model_calls} records"
            )
        for index, record in enumerate(records, 1):
            if not isinstance(record, dict):
                raise RuntimeError(f"model response {index} is malformed")
            usage = gate1p_stagea.normalize_usage(
                record.get("token_usage") if isinstance(record.get("token_usage"), dict) else {},
                response_id=str(record.get("decision_id") or index),
            )
            if usage["usage_complete"] is not True:
                raise RuntimeError(f"model response {index} omitted reconciling input/output/total usage")
        if telemetry.get("token_usage_complete") is not True:
            raise RuntimeError("runtime telemetry marked token usage incomplete")
        expected_fields = (
            ("token budget", expected_token_budget, "token_budget"),
            ("model context limit", expected_model_context_limit, "model_context_limit"),
            ("max output tokens", expected_max_output_tokens, "max_output_tokens"),
        )
        for label, expected, key in expected_fields:
            if expected is not None and int(telemetry.get(key, 0) or 0) != int(expected):
                raise RuntimeError(
                    f"runtime {label} {telemetry.get(key)!r} did not match configured {expected!r}"
                )


def runtime_evidence_fields(telemetry: dict | None) -> dict:
    """Persist the controller evidence needed to audit branch quality after the next lab reset."""
    if not isinstance(telemetry, dict):
        return {}
    keys = (
        "controller_status",
        "controller_cycle_count",
        "controller_cycles",
        "controller_blocker",
        "achieved_effects",
        "model_calls",
        "backend_provenance_complete",
        "policy_switches",
        "decisions",
        "transactions",
    )
    return {key: telemetry[key] for key in keys if key in telemetry}


_GATE1P_PRIMARY_SCENARIOS = gate1p_protocol.primary_scenarios()


def _row_outcome_disposition(
    row_protocol: gate1p_protocol.Gate1PProtocol | None,
    pair_id: str | None,
) -> Callable[..., dict]:
    if row_protocol is None:
        return gate1p_stagea.episode_outcome_disposition
    return lambda observer, **kwargs: row_protocol.episode_outcome_disposition(
        observer,
        pair_id=pair_id,
        **kwargs,
    )


def measurement_dimensions(
    card: ScoreCard,
    *,
    measurement_kind: str,
    cost_countable: dict,
    cost_values: dict | None = None,
    stop_observation_available: bool,
    outcome_measurement_error: str | None = None,
    observer_result: dict | None = None,
    arm: str | None = None,
    model_response_records: object = None,
    model_calls: object = None,
    token_ceiling: object = None,
    outcome_disposition: Callable[..., dict] = gate1p_stagea.episode_outcome_disposition,
) -> dict:
    """Report independent countability dimensions without converting missing evidence to failure or zero."""
    kind = str(measurement_kind or "legacy")
    probe_available = not bool(getattr(card, "probe_disagreements", []))
    clean_stop_available = bool(stop_observation_available)
    usage = (
        gate1p_stagea.response_usage_disposition(
            model_response_records,
            model_calls=model_calls,
        )
        if kind == "row"
        else {"countable": False}
    )
    observer_semantics = (
        outcome_disposition(
            observer_result,
            arm=str(arm or ""),
            total_tokens=usage.get("total_tokens"),
            token_ceiling=token_ceiling,
        )
        if kind == "row" and usage.get("countable") is True
        else {"countable": False, "reason": usage.get("reason")}
    )
    if kind == "row" and observer_semantics.get("countable") is True:
        clean_stop_available = bool(
            stop_observation_available and observer_semantics.get("stop_countable") is True
        )
    if kind == "diagnostic":
        outcome_reason = "prospectively non-countable production-shape diagnostic"
    elif kind == "row" and outcome_measurement_error:
        outcome_reason = f"measurement defect: {outcome_measurement_error}"
    elif kind == "row" and not probe_available:
        outcome_reason = "outcome oracle unavailable or errored"
    elif kind == "row" and usage.get("countable") is not True:
        outcome_reason = f"measurement defect: {usage.get('reason')}"
    elif kind == "row" and observer_semantics.get("countable") is not True:
        outcome_reason = f"measurement defect: {observer_semantics.get('reason')}"
    elif kind == "row" and not clean_stop_available:
        outcome_reason = "objective evidence available; evaluator-owned task-stop evidence unavailable"
    elif kind == "row":
        outcome_reason = "outcome evidence available"
    else:
        outcome_reason = "legacy record outside Gate 1P admission"
    outcome_countable = bool(
        kind == "row"
        and not outcome_measurement_error
        and probe_available
        and observer_semantics.get("countable") is True
    )
    semantic_projection = (
        observer_semantics
        if kind == "row" and observer_semantics.get("countable") is True
        else None
    )
    canonical_cost_values = dict(cost_values or {})
    if kind == "row" and usage.get("countable") is True:
        canonical_cost_values["total_tokens"] = usage["total_tokens"]
    outcome = {
            "countable": outcome_countable,
            "reason": outcome_reason,
            "stop_countable": (
                clean_stop_available if kind == "row" else bool(clean_stop_available)
            ),
            "objective_success": (
                semantic_projection["objective_success"]
                if semantic_projection is not None
                else bool(card.objective_proven) if kind != "row" and probe_available else None
            ),
            "clean_stop": (
                semantic_projection["clean_stop"]
                if semantic_projection is not None
                else bool(card.clean_stop) if kind != "row" and clean_stop_available else None
            ),
            "objective_recognized": (
                semantic_projection["objective_recognized"]
                if semantic_projection is not None
                else bool(card.objective_recognized)
                if kind != "row" and clean_stop_available
                else None
            ),
            "objective_clean_stop": (
                semantic_projection["objective_clean_stop"]
                if semantic_projection is not None
                else bool(card.objective_clean_stop)
                if kind != "row" and probe_available and clean_stop_available
                else None
            ),
    }
    if semantic_projection is not None:
        for field in ("control_achieved", "control_exercised", "strict_control_result"):
            if field in semantic_projection:
                outcome[field] = semantic_projection[field]
    return {
        "outcome": outcome,
        "comparison": {
            "countable": False,
            "reason": (
                "requires a prospectively paired row with matching model, authority, start state, and budgets"
                if outcome_countable
                else "outcome dimension is not countable"
            ),
        },
        "cost": {
            key: {
                "countable": bool(value),
                "value": (
                    canonical_cost_values.get(key, getattr(card, key, None))
                    if value
                    else None
                ),
            }
            for key, value in cost_countable.items()
        },
    }


def harness_terminal_cause(wrapper_status: object, runtime_telemetry: dict) -> dict[str, str]:
    """Classify only typed wrapper/controller state; never infer context failure from prose."""
    wrapper = str(wrapper_status or "").strip().casefold()
    if wrapper == "timeout":
        return {"cause": "wall_time", "subtype": "wrapper_timeout"}
    if wrapper == "interrupted":
        return {"cause": "other_declared_boundary", "subtype": "interrupted"}
    if wrapper.startswith("error:"):
        return {"cause": "other_declared_boundary", "subtype": "wrapper_error"}
    status = str(runtime_telemetry.get("controller_status") or "").strip().casefold()
    reason = str(runtime_telemetry.get("controller_terminal_reason") or "").strip().casefold()
    if status == "complete":
        return {"cause": "explicit_terminal_decision", "subtype": "objective_met"}
    if status == "halted_budget":
        if reason == "token budget exhausted":
            subtype = "overshoot" if runtime_telemetry.get("token_budget_overshoot") is True else "exhausted"
            return {"cause": "cumulative_token_ceiling", "subtype": subtype}
        if reason == "wall-clock budget exhausted":
            return {"cause": "wall_time", "subtype": "wrapper_timeout"}
    controller_subtypes = {
        "halted_aborted": "controller_aborted",
        "halted_blocked": "controller_blocked",
        "halted_denied": "controller_denied",
        "halted_max_cycles": "controller_max_cycles",
        "halted_no_action": "controller_no_action",
        "halted_no_progress": "controller_no_progress",
    }
    if status in controller_subtypes:
        return {"cause": "other_declared_boundary", "subtype": controller_subtypes[status]}
    return {"cause": "other_declared_boundary", "subtype": "unknown"}


def episode_protocol_snapshot(cfg: Config, *, side: str, scenario_name: str, model_route: dict) -> dict:
    """The concise per-attempt development-measurement protocol; no sealing or forensic receipt layer."""
    snapshot = {
        "protocol_id": cfg.protocol_id,
        "measurement_kind": cfg.measurement_kind,
        "scenario": scenario_name,
        "side": side,
        "model_provider": model_route.get("provider"),
        "model_id": model_route.get("model"),
        "solve_timeout_seconds": cfg.solve_timeout,
        "max_steps": cfg.max_steps,
        "source_version": cfg.source_version or os.environ.get("SAGE_EVAL_SOURCE_VERSION") or "unrecorded",
        "invalidation_rules": [
            "outcome oracle unavailable or errored",
            "evaluator-owned clean-stop observation unavailable",
            "arm model/authority/start-state/budget mismatch",
            "evaluation-plane data leaked into Sage or model context",
        ],
    }
    if cfg.measurement_kind == "row" and scenario_name in _GATE1P_PRIMARY_SCENARIOS:
        protocol = gate1p_protocol.protocol_for_request(
            protocol_id=cfg.protocol_id, scenario=scenario_name,
        )
        arm = protocol.arms[int(cfg.arm_index)] if cfg.arm_index in (0, 1) else ""
        snapshot.update({
            "pair_id": cfg.pair_id,
            "arm": arm,
            "arm_index": cfg.arm_index,
            "wrapper": "shipping_native_sage" if side == "harness" else "true_naive_raw_tools",
            "pair_invariants": protocol.module.pair_invariants(
                provider=str(model_route.get("provider") or ""),
                model=str(model_route.get("model") or ""),
                api_endpoint=str(model_route.get("api_endpoint") or ""),
                source_version=str(cfg.source_version or ""),
                model_context_limit=int(cfg.model_context_limit or 0),
                max_output_tokens=int(cfg.max_output_tokens or 0),
                pair_id=str(cfg.pair_id or ""),
            ),
        })
    return snapshot


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def _pair_arm_has_started(path: Path, *, pair_id: str, arm: str) -> bool:
    """Fail closed when this append-only results ledger already consumed an arm attempt."""
    if not path.exists():
        return False
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        raise gate1p_stagea.ProtocolError(f"cannot validate the existing row ledger: {exc}") from exc
    for record in records:
        protocol = record.get("protocol") if isinstance(record, dict) else None
        if not isinstance(protocol, dict):
            continue
        if protocol.get("pair_id") != pair_id or protocol.get("arm") != arm:
            continue
        if record.get("record_type") in {"arm_started", "result"} or "card" in record:
            return True
    return False


def run_side(
    cfg: Config,
    side: str,
    scenario_name: str,
    *,
    evaluation_reader_factory: Callable[[], Callable[[str], set]] | None = None,
) -> ScoreCard:
    """Run ONE side on ONE scenario (range assumed freshly reset), score via the shared probes, record."""
    row_arm = ""
    row_protocol = None
    if cfg.measurement_kind == "row" and scenario_name in _GATE1P_PRIMARY_SCENARIOS:
        row_protocol = gate1p_protocol.protocol_for_request(
            protocol_id=cfg.protocol_id,
            scenario=scenario_name,
        )
        incompatible = gate1p_stagea.active_incompatible_evaluation_overrides(os.environ)
        if incompatible:
            raise gate1p_stagea.ProtocolError(
                "Gate 1P row inherited incompatible evaluation override(s): "
                + ", ".join(incompatible)
            )
        row_arm = row_protocol.validate_request(
            require_allocation=True,
            protocol_id=cfg.protocol_id,
            pair_id=cfg.pair_id,
            scenario=scenario_name,
            side=side,
            arm_index=cfg.arm_index,
            provider=cfg.model_provider,
            model=cfg.model_id,
            policy_mode=cfg.policy_mode,
            solve_timeout=cfg.solve_timeout,
            token_ceiling=cfg.model_token_budget,
            model_context_limit=cfg.model_context_limit,
            max_output_tokens=cfg.max_output_tokens,
            source_version=cfg.source_version,
            api_endpoint=cfg.model_api_endpoint,
        )
        if cfg.live_authority_id != row_protocol.allocation_id:
            raise gate1p_stagea.ProtocolError(
                f"live paired-row execution requires separate authority ID {row_protocol.allocation_id!r}"
            )
    scn = _scenario(cfg, scenario_name)
    if row_arm and scn.objective != row_protocol.objective:
        raise gate1p_stagea.ProtocolError("assembled scenario objective bytes differ from the accepted protocol")
    if row_arm and _pair_arm_has_started(
        cfg.results_path,
        pair_id=str(cfg.pair_id),
        arm=row_arm,
    ):
        raise gate1p_stagea.ProtocolError(
            f"the retry-zero ledger already contains a started {row_arm!r} arm for {cfg.pair_id!r}"
        )
    gate1p_artifact_path = _validate_gate1p_artifact_destinations(cfg, row_arm) if row_arm else None
    if row_arm:
        _validate_gate1p_pair_control(cfg, row_arm, protocol=row_protocol)
    resolved_model_route = _resolved_harness_model_route(cfg)
    client = live_seams.default_mythic_client()
    mythic_operation_id = getattr(client, "current_operation_id", None)
    if row_arm and (type(mythic_operation_id) is not int or mythic_operation_id <= 0):
        raise gate1p_stagea.ProtocolError("the authenticated Mythic operation identity is unavailable")
    runtime_telemetry: dict = {}
    if row_arm:
        prestart_tasks = live_seams.mythic_operation_task_states(
            expected_operation_id=int(mythic_operation_id)
        )
        if prestart_tasks:
            raise gate1p_stagea.ProtocolError(
                f"the current Mythic operation has {len(prestart_tasks)} pre-arm task(s)"
            )

    # GROUND TRUTH is read through a separately controlled evaluation plane, NOT the agent callback.
    # Routing it through that callback would (a) pollute the HARNESS — Sage reconciles the callback's task
    # history at solve start, so it would see the recon AND the answer, asymmetrically vs the bare model —
    # and (b) misread the domain (`net group /domain` only enumerates the host's own domain). Scope the
    # baseline to the AD-direct milestones THIS scenario scores, so e.g. child-da neither queries nor needs
    # referee creds for essos. The baseline is captured pre-solve but off-callback, so it cannot pollute.
    needed = _scored_referee_domains(scn)   # same scope the preflight validates creds for (single source)
    if evaluation_reader_factory is not None:
        reader_factory = evaluation_reader_factory
    elif row_arm:
        reader_factory = lambda: live_seams.make_referee_reader(strict=True)
    else:
        reader_factory = live_seams.make_referee_reader
    reader = reader_factory() if needed else (lambda _d: set())
    baseline = {d: reader(d) for d in needed}
    probes = build_probes(reader, baseline, scn,
                          settle_timeout=cfg.da_settle_timeout, settle_interval=cfg.da_settle_interval)
    protocol_snapshot = episode_protocol_snapshot(
        cfg,
        side=side,
        scenario_name=scenario_name,
        model_route={
            "provider": resolved_model_route.get("provider"),
            "model": resolved_model_route.get("model"),
            "api_endpoint": resolved_model_route.get("api_endpoint"),
        },
    )
    if row_arm:
        protocol_snapshot["initial_callback_display_id"] = int(cfg.apollo_cb)
        protocol_snapshot["mythic_operation_id"] = int(mythic_operation_id)
        try:
            reset_readiness = json.loads(os.environ.get("SAGE_EVAL_RESET_READINESS_JSON") or "")
        except (TypeError, ValueError) as exc:
            raise gate1p_stagea.ProtocolError("reset/readiness report is missing or malformed") from exc
        if not isinstance(reset_readiness, dict) or reset_readiness.get("ready") is not True:
            raise gate1p_stagea.ProtocolError("reset/readiness report did not explicitly pass")
        protocol_snapshot["reset_readiness"] = reset_readiness
        snapshot_record = {
            "record_type": "pre_run_protocol_snapshot",
            "measurement_kind": "row",
            "protocol": protocol_snapshot,
            "ts": time.time(),
        }
        _append_jsonl(cfg.results_path, snapshot_record)
    observer = None
    gate1p_oracle = None
    gate1p_evaluator_state: dict[str, object] = {}
    if row_arm:
        objective_domain = str((getattr(scn, "domains", {}) or {}).get("objective") or CHILD)
        baseline_members = set(baseline.get(objective_domain, set()))
        gate1p_evaluator_state = {
            "domain": objective_domain,
            "baseline_members": sorted(baseline_members),
            "final_members": sorted(baseline_members),
        }

        def _directory_delta_probe() -> bool:
            final_members = set(reader(objective_domain))
            gate1p_evaluator_state["final_members"] = sorted(final_members)
            return bool(final_members - baseline_members)

        if (
            row_protocol.module is gate1p_stageb
            or getattr(row_protocol.module, "PROOF_ADAPTER_FAMILY", "") == "stageb"
        ):
            gate1p_oracle = row_protocol.build_objective_oracle(
                _stageb_proof_adapters(
                    reader, baseline, objective_domain=objective_domain,
                    directory_probe=_directory_delta_probe,
                ),
                artifact_pointer=str(gate1p_artifact_path),
                domain=objective_domain,
            )
        else:
            gate1p_oracle = row_protocol.build_objective_oracle(
                {
                    "directory_delta": _directory_delta_probe,
                    "certificate_control": live_seams.certificate_admin_control_probe(
                        None, realm=objective_domain,
                    ),
                    "ticket_control": live_seams.ticket_admin_control_probe(realm=objective_domain),
                },
                artifact_pointer=str(gate1p_artifact_path),
                summary_factory=lambda: {
                    "domain": objective_domain,
                    "baseline_member_count": len(baseline_members),
                    "final_member_count": len(gate1p_evaluator_state.get("final_members") or []),
                },
            )
        observer = row_protocol.build_episode_observer(
            pair_id=str(cfg.pair_id),
            arm=row_arm,
            objective_probe=gate1p_oracle,
            task_fetcher=lambda: live_seams.mythic_operation_task_states(
                expected_operation_id=int(mythic_operation_id)
            ),
            authority_checker=lambda rows: gate1p_stagea.stagea_authority_evidence(
                rows,
                initial_callback_display_id=int(cfg.apollo_cb),
                mythic_operation_id=int(mythic_operation_id),
            ),
        )

    canonical_outcome: dict[str, object] = {"countable": False, "reason": "not a paired row"}
    canonical_response_records: list[dict[str, object]] = []
    bare_model_contract: dict[str, object] | None = None
    if side == "harness":
        harness_route = resolved_model_route
        if cfg.null_model or os.environ.get("SAGE_EVAL_HEADLESS"):
            # Option A (Phase-4 migration): run the solve IN-PROCESS via the chat Model instead of tasking
            # the PayloadType `query` on the virtual callback. Alongside path — selected only when the flag
            # is set — so a migration run can compare in-process vs task-based on the same reset. Ledger key
            # comes from SAGE_ENGAGEMENT_ID (the run token the reset sets) or the operation name.
            _eng = os.environ.get("SAGE_ENGAGEMENT_ID") or cfg.engagement_op
            treatment = "null-model" if cfg.null_model else "headless"
            print(
                f"[harness/{scenario_name}] {treatment} in-process solve (engagement={_eng})",
                flush=True,
            )
            solve = live_seams.make_headless_solver(client, engagement_id=_eng,
                                                    timeout=cfg.solve_timeout, max_steps=0,
                                                    policy_mode=cfg.policy_mode,
                                                    provider=harness_route["provider"],
                                                    model=harness_route["model"],
                                                    api_endpoint=harness_route["api_endpoint"],
                                                    api_key=harness_route["api_key"],
                                                    null_model=cfg.null_model)
        else:
            solve = live_seams.make_native_chat_solver(
                client,
                timeout=cfg.solve_timeout,
                provider=cfg.model_provider,
                model=cfg.model_id,
                api_endpoint=cfg.model_api_endpoint,
                api_key=cfg.model_api_key,
                eval_force_capability_prefix_json=os.environ.get(
                    "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON"
                ) or None,
            )
        _start_wall = time.time()
        _start_monotonic = time.monotonic()
        _deadline = _start_wall + cfg.solve_timeout
        if observer is not None:
            observer.start()
            _append_jsonl(cfg.results_path, {
                "record_type": "arm_started",
                "measurement_kind": "row",
                "protocol": protocol_snapshot,
                "ts": time.time(),
                "event": dict(observer.coordinator.events[0]),
            })
        print(f"[harness/{scenario_name}] started {time.strftime('%H:%M:%S', time.localtime(_start_wall))} · "
              f"times out by {time.strftime('%H:%M:%S', time.localtime(_deadline))} "
              f"(+{cfg.solve_timeout // 60} min via --solve-timeout, unless Sage finishes first)", flush=True)
        solve_status = "done"
        solve_error = None
        try:
            # make_harness_solver returns the Mythic task status: "success" when Sage finished,
            # "timeout" when the wall-clock poll expired (Sage may still be churning in the background).
            solve_status = solve(scn.objective) or "done"
        except KeyboardInterrupt:
            solve_status = "interrupted"
            print("⎈ interrupted — scoring the current range state", flush=True)
        except Exception as exc:
            solve_error = exc
            solve_status = f"error:{type(exc).__name__}"
            print(f"✗ harness wrapper error: {type(exc).__name__}: {exc}", flush=True)
        _elapsed = max(0.0, time.monotonic() - _start_monotonic)
        _elapsed_display = int(_elapsed)
        print(f"[harness/{scenario_name}] solve returned {time.strftime('%H:%M:%S')} "
              f"(elapsed {_elapsed_display // 60}m{_elapsed_display % 60}s, "
              f"status={solve_status})", flush=True)
        native_result = getattr(solve, "last_result", None)
        runtime_telemetry = (
            dict(native_result.get("runtime_telemetry") or {})
            if isinstance(native_result, dict)
            else {}
        )
        termination = harness_terminal_cause(solve_status, runtime_telemetry)
        observer_result = (
            observer.finish(
                wrapper_status=solve_status,
                wrapper_objective_recognized=(
                    runtime_telemetry.get("objective_recognized")
                    if isinstance(runtime_telemetry.get("objective_recognized"), bool)
                    else None
                ),
            )
            if observer is not None
            else {}
        )
        telemetry_validation_error = None
        try:
            validate_harness_runtime_telemetry(
                cfg.policy_mode,
                runtime_telemetry,
                configured_provider=harness_route["provider"],
                configured_model=harness_route["model"],
                require_all_response_provenance=cfg.measurement_kind != "diagnostic",
                require_complete_usage=bool(row_arm),
                expected_token_budget=cfg.model_token_budget if row_arm else None,
                expected_model_context_limit=cfg.model_context_limit if row_arm else None,
                expected_max_output_tokens=cfg.max_output_tokens if row_arm else None,
            )
        except RuntimeError as exc:
            if cfg.measurement_kind not in {"diagnostic", "row"}:
                raise
            telemetry_validation_error = str(exc)
        if row_arm:
            usage_evidence = gate1p_stagea.response_usage_disposition(
                runtime_telemetry.get("model_response_records"),
                model_calls=runtime_telemetry.get("model_calls"),
            )
            canonical_response_records = list(usage_evidence.get("canonical_records") or [])
            canonical_outcome = (
                row_protocol.episode_outcome_disposition(
                    observer_result,
                    arm=row_arm,
                    total_tokens=usage_evidence["total_tokens"],
                    token_ceiling=cfg.model_token_budget,
                    pair_id=cfg.pair_id,
                )
                if usage_evidence.get("countable") is True
                else {"countable": False, "reason": usage_evidence.get("reason")}
            )
        if (
            observer_result
            and canonical_outcome.get("countable") is True
            and canonical_outcome.get("stop_countable") is True
        ):
            runtime_telemetry.update({
                "clean_stop": canonical_outcome["clean_stop"],
                "objective_recognized": canonical_outcome["objective_recognized"],
                "request_completed": canonical_outcome["clean_stop"] is True,
            })
        if canonical_outcome.get("countable") is True:
            probes[Milestone.OBJECTIVE] = lambda: bool(canonical_outcome["objective_success"])
        elif observer_result:
            def _observer_unavailable():
                raise gate1p_stagea.ObserverUnavailable(
                    str(canonical_outcome.get("reason") or "observer unavailable")
                )
            probes[Milestone.OBJECTIVE] = _observer_unavailable
        # Record the REAL terminal status (incl. "timeout") AND the wall-clock cost in the card -> jsonl.
        # wall_seconds is the discriminating signal once capability saturates: a completion-recognized clean
        # stop (status="stopped" well under the budget) vs a churn-to-timeout shows up here, not in capability.
        card = bare_runner.score_from_probes(
            scn,
            probes,
            status=solve_status,
            wall_seconds=_elapsed,
            runtime_telemetry=runtime_telemetry,
            configured_policy_mode=cfg.policy_mode,
        )
    elif side == "bare":
        telemetry_validation_error = None
        bare = build_bare_runner(
            cfg,
            require_bloodhound=scenario_name in _GATE1P_PRIMARY_SCENARIOS,
        )
        bare_model_contract = gate1p_stagea.bare_model_contract_from_tool_specs(bare.tools_spec)
        if row_arm:
            _append_jsonl(cfg.results_path, {
                "record_type": "pre_model_bare_contract",
                "measurement_kind": "row",
                "protocol": protocol_snapshot,
                "bare_model": bare_model_contract,
                "ts": time.time(),
            })
        if observer is not None:
            observer.start()
            _append_jsonl(cfg.results_path, {
                "record_type": "arm_started",
                "measurement_kind": "row",
                "protocol": protocol_snapshot,
                "ts": time.time(),
                "event": dict(observer.coordinator.events[0]),
            })
        result = bare.run(scn.objective)
        termination = dict(result.termination)
        runtime_telemetry = {
            "controller_cycle_count": 0,
            "achieved_effects": [],
            "action_response_ids": list(result.action_response_ids),
        }
        if result.recoverable_tool_errors:
            print(
                f"[bare/{scenario_name}] retained {len(result.recoverable_tool_errors)} "
                "recoverable tool error(s); post-run admission will decide countability",
                flush=True,
            )
        observer_result = (
            observer.finish(
                wrapper_status=result.stopped,
                wrapper_objective_recognized=result.objective_recognized,
            )
            if observer is not None
            else {}
        )
        if row_arm:
            usage_evidence = gate1p_stagea.response_usage_disposition(
                result.model_response_records,
                model_calls=result.model_calls,
            )
            canonical_response_records = list(usage_evidence.get("canonical_records") or [])
            canonical_outcome = (
                row_protocol.episode_outcome_disposition(
                    observer_result,
                    arm=row_arm,
                    total_tokens=usage_evidence["total_tokens"],
                    token_ceiling=cfg.model_token_budget,
                    pair_id=cfg.pair_id,
                )
                if usage_evidence.get("countable") is True
                else {"countable": False, "reason": usage_evidence.get("reason")}
            )
        if canonical_outcome.get("countable") is True:
            probes[Milestone.OBJECTIVE] = lambda: bool(canonical_outcome["objective_success"])
        elif observer_result:
            def _observer_unavailable():
                raise gate1p_stagea.ObserverUnavailable(
                    str(canonical_outcome.get("reason") or "observer unavailable")
                )
            probes[Milestone.OBJECTIVE] = _observer_unavailable
        card = bare_runner.score_bare_run(
            result,
            scn,
            probes,
            evaluator_clean_stop=(
                canonical_outcome.get("clean_stop")
                if canonical_outcome.get("countable") is True
                else None
            ),
            evaluator_objective_recognized=(
                canonical_outcome.get("objective_recognized")
                if canonical_outcome.get("countable") is True
                else None
            ),
        )
    else:
        raise SystemExit("--side must be harness|bare")

    # Record which LLM produced this run, for provenance and multi-LLM matrices. Today both sides read
    # the model from Sage's .env (bare via build_bare_runner -> load_sage_defaults; harness = whatever Sage
    # is running). When a bare-side --model override is added, record THAT for the bare side instead.
    _defs = live_seams.load_sage_defaults()
    if cfg.model_provider:
        _defs["provider"] = cfg.model_provider
    if cfg.model_id:
        _defs["model"] = cfg.model_id
    if side == "bare":
        card.policy_mode = "llm"
        card.configured_policy_mode = "llm"
        card.policy_identity_valid = True
        card.model_provider = str(_defs.get("provider") or "")
        card.model_id = str(_defs.get("model") or "")
    _now = time.time()
    wrapper_tool_countable = side == "bare" or "tool_calls" in runtime_telemetry
    token_cost_countable = (
        bool(getattr(locals().get("result"), "token_usage_complete", False))
        if side == "bare"
        else runtime_telemetry.get("token_usage_complete") is True
    )
    observer_evidence = (
        row_protocol.observer_facts_disposition(
            observer_result, arm=row_arm, pair_id=cfg.pair_id,
        )
        if row_arm and observer_result
        else {"countable": False}
    )
    if row_arm:
        cost_countable = {
            "model_calls": side == "bare" or "model_calls" in runtime_telemetry,
            "total_tokens": token_cost_countable,
            "environment_actions": bool(
                observer_evidence.get("countable") is True
                and observer_evidence.get("task_window_observed") is True
            ),
            "wrapper_tool_invocations": wrapper_tool_countable,
            "control_plane_reads": False,
            "wall_seconds": True,
        }
        cost_values = {
            "model_calls": int(getattr(card, "model_calls", 0) or 0),
            "total_tokens": int(getattr(card, "total_tokens", 0) or 0),
            "environment_actions": len(observer_result.get("tasks") or []),
            "wrapper_tool_invocations": int(getattr(card, "tool_calls", 0) or 0),
            "control_plane_reads": None,
            "wall_seconds": float(getattr(card, "wall_seconds", 0.0) or 0.0),
        }
    else:
        cost_countable = {
            "tool_calls": wrapper_tool_countable,
            "model_calls": side == "bare" or "model_calls" in runtime_telemetry,
            "total_tokens": token_cost_countable,
            "wall_seconds": True,
        }
        cost_values = None
    stop_observation_available = bool(
        canonical_outcome.get("stop_countable") is True
        if observer_result
        else side == "harness" and any(
            key in runtime_telemetry
            for key in ("clean_stop", "controller_status", "request_completed", "objective_recognized")
        )
    )
    runtime_summary_evidence = gate1p_stagea.runtime_summary_disposition(runtime_telemetry)
    runtime_summary = dict(runtime_summary_evidence.get("canonical_summary") or {})
    artifact_capture_error = None
    artifact_capture: dict[str, object] = {}
    if row_arm:
        native_artifact = getattr(locals().get("solve"), "last_result", None)
        transcript = (
            {
                "events": list(result.transcript),
                "final": result.final_text,
                "recoverable_tool_errors": list(result.recoverable_tool_errors),
            }
            if side == "bare"
            else list((native_artifact or {}).get("messages") or [])
            if isinstance(native_artifact, dict)
            else []
        )
        artifact_payload = {
            "schema": "gate1p-stagea-episode-artifact-v17",
            "protocol": protocol_snapshot,
            "side": side,
            "arm": row_arm,
            "evaluator_private": gate1p_evaluator_state,
            "reset_readiness": protocol_snapshot.get("reset_readiness"),
            "oracle": observer_result.get("oracle") or {},
            "observer": observer_result,
            "transcript": transcript,
            "runtime_summary": runtime_summary,
            "response_evidence": list(canonical_response_records),
            "bare_model": bare_model_contract if side == "bare" else None,
            "scorecard": asdict(card),
        }
        try:
            if side == "harness" and not transcript:
                raise RuntimeError("native-chat transcript export is empty")
            _write_gate1p_episode_artifact(gate1p_artifact_path, artifact_payload)
            artifact_capture = {
                "complete": True,
                "episode_artifact": str(gate1p_artifact_path),
                "result_ledger": str(cfg.results_path),
                "transcript_entries": (
                    len(transcript.get("events") or []) + 1
                    if isinstance(transcript, dict)
                    else len(transcript)
                ),
            }
        except Exception as exc:
            artifact_capture_error = f"{type(exc).__name__}: {exc}"
            artifact_capture = {
                "complete": False,
                "episode_artifact": str(gate1p_artifact_path),
                "result_ledger": str(cfg.results_path),
                "error": artifact_capture_error,
            }
    outcome_measurement_error = None
    if row_arm and telemetry_validation_error:
        outcome_measurement_error = telemetry_validation_error
    if row_arm and side == "harness" and locals().get("solve_error") is not None:
        outcome_measurement_error = (
            f"harness wrapper error: {type(solve_error).__name__}: {solve_error}"
        )
    if row_arm and runtime_summary_evidence.get("countable") is not True:
        outcome_measurement_error = (
            f"runtime diagnostic summary failed: {runtime_summary_evidence.get('reason')}"
        )
    if row_arm and observer_result.get("task_window_observed") is True:
        try:
            authority_evidence = gate1p_stagea.stagea_authority_evidence(
                observer_result.get("tasks") or [],
                initial_callback_display_id=int(cfg.apollo_cb),
                mythic_operation_id=int(mythic_operation_id),
            )
        except (gate1p_stagea.ProtocolError, TypeError, ValueError):
            authority_evidence = {"valid": False}
        if authority_evidence.get("valid") is not True:
            outcome_measurement_error = "Stage A authority evidence failed or found a violation"
    if row_arm and artifact_capture_error:
        outcome_measurement_error = f"required artifact capture failed: {artifact_capture_error}"
    rec = {"record_type": "result", "side": side, "scenario": scenario_name,
           "model": _defs.get("model"), "provider": _defs.get("provider"),
           "policy_mode": card.policy_mode,
           "configured_policy_mode": card.configured_policy_mode,
           "policy_identity_valid": card.policy_identity_valid,
           "request_completed": card.request_completed,
           "objective_recognized": card.objective_recognized,
           "objective_proven": card.objective_proven,
           "clean_stop": card.clean_stop,
           "objective_clean_stop": getattr(card, "objective_clean_stop", False),
           "controller_terminal_reason": card.controller_terminal_reason,
           "semantic_transaction_count": card.semantic_transaction_count,
           "authorized_transaction_count": card.authorized_transaction_count,
           "semantic_policy_coverage": card.semantic_policy_coverage,
           "effective_backends": list(card.effective_backends),
           "effective_backend_requests": list(card.effective_backend_requests),
           "model_calls": int(getattr(card, "model_calls", 0) or 0),
           "model_response_records": list(canonical_response_records),
           "token_usage_complete": (
               bool(result.token_usage_complete)
               if side == "bare"
               else runtime_telemetry.get("token_usage_complete") is True
           ),
           "measurement_kind": cfg.measurement_kind,
           "termination": dict(termination),
           "protocol": dict(protocol_snapshot),
           "countability": measurement_dimensions(
               card,
               measurement_kind=cfg.measurement_kind,
               cost_countable=cost_countable,
               cost_values=cost_values,
               stop_observation_available=stop_observation_available,
               outcome_measurement_error=outcome_measurement_error,
               observer_result=observer_result,
               arm=row_arm,
               model_response_records=list(canonical_response_records),
               model_calls=getattr(card, "model_calls", None),
               token_ceiling=cfg.model_token_budget,
               outcome_disposition=_row_outcome_disposition(row_protocol, cfg.pair_id),
           ),
           "cost_countable": cost_countable,
           "telemetry_validation_error": telemetry_validation_error,
           "observer": observer_result,
           "runtime_summary": runtime_summary,
           "artifact_capture": artifact_capture,
           # ts = epoch (sortable); ts_iso = local-time human stamp to eyeball-correlate to the archived
           # sage_<YYYYMMDD-HHMM>.db / phoenix_<...>.db moved at the NEXT reset (which holds THIS run's data).
           "ts": _now, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_now)),
           "card": asdict(card)}
    for env_key, row_key in (
        ("SAGE_EVAL_PHASE6_MANIFEST_HASH", "phase6_manifest_hash"),
        ("SAGE_EVAL_PHASE6_TOPOLOGY_HASH", "phase6_topology_hash"),
        ("SAGE_EVAL_PHASE6_CANDIDATE_SET_HASH", "phase6_candidate_set_hash"),
        ("SAGE_EVAL_PHASE6_ORDERED_FRONTIER_HASH", "phase6_ordered_frontier_hash"),
        ("SAGE_EVAL_PHASE6_FORCED_PATH", "phase6_forced_path"),
        ("SAGE_EVAL_PHASE6_PLANNED_ROW_ID", "phase6_planned_row_id"),
        ("SAGE_EVAL_PHASE6_ATTEMPT_INDEX", "phase6_attempt_index"),
        (
            "SAGE_EVAL_PHASE6_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES",
            "phase6_max_pre_frontier_diagnostic_retries",
        ),
        ("SAGE_EVAL_PHASE7_MANIFEST_HASH", "phase7_manifest_hash"),
        ("SAGE_EVAL_PHASE7_TOPOLOGY_HASH", "phase7_topology_hash"),
        ("SAGE_EVAL_PHASE7_CONTROL", "phase7_control"),
        ("SAGE_EVAL_PHASE7_ATTEMPT_INDEX", "phase7_attempt_index"),
        ("SAGE_EVAL_PHASE8_CONTRACT_HASH", "phase8_contract_hash"),
        ("SAGE_EVAL_PHASE8_POLICY_ARM", "phase8_policy_arm"),
        ("SAGE_EVAL_PHASE8_PLANNED_ROW_ID", "phase8_planned_row_id"),
        ("SAGE_EVAL_PHASE8_ATTEMPT_INDEX", "phase8_attempt_index"),
    ):
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            rec[row_key] = (
                int(value)
                if row_key in {
                    "phase6_attempt_index",
                    "phase6_max_pre_frontier_diagnostic_retries",
                    "phase7_attempt_index",
                    "phase8_attempt_index",
                }
                else value
            )
    if not row_arm:
        rec.update(runtime_evidence_fields(runtime_telemetry))
    native_result = getattr(locals().get("solve"), "last_result", None)
    if isinstance(native_result, dict):
        rec["chat_channel_id"] = native_result.get("chat_channel_id")
        rec["chat_request_id"] = native_result.get("chat_request_id")
    p = cfg.results_path
    _append_jsonl(p, rec)
    print(f"[{side}/{scenario_name}] capability={card.capability:.3f} furthest={card.furthest_milestone} "
          f"-> recorded to {p}", flush=True)
    if row_arm:
        arm_admission = row_protocol.module.arm_comparison_disposition(rec)
        if arm_admission.get("countable") is not True:
            raise RuntimeError(
                "the arm was retained but cannot admit its mate; stop the pair: "
                f"{arm_admission.get('reason')}"
            )
    return card


def build_bare_runner(cfg: Config, *, require_bloodhound: bool = False) -> bare_runner.BareModelRunner:
    route = _resolved_harness_model_route(cfg)
    if not route.get("provider") or not route.get("model"):
        raise RuntimeError("the shared Gate 1P provider/model route is unavailable")
    model_fn = live_seams.make_model_fn(
        route["provider"],
        route["model"],
        api_key=route["api_key"],
        base_url=route["api_endpoint"],
        max_output_tokens=cfg.max_output_tokens,
    )
    # Bare uses the SAME Mythic interface as Sage (enumerate payloads -> commands -> args -> task), via the
    # STRIPPED (Sage-free) toolset — NOT a hardcoded Apollo command list. The dispatcher routes the model's
    # tool calls straight to the raw Mythic SDK. No step cap (cfg.max_steps=0); solve_timeout is a
    # wall-clock ceiling; a live stdout logger lets the operator watch the model between Mythic tasks.
    client = live_seams.default_mythic_client()

    # BloodHound: discover EVERY MCP tool dynamically (not hardcoded). Raw external tool — Sage's
    # BloodHound *agent* (ingest reconciliation, collect-once gate, graph-fact injection) stays excluded.
    bh_specs, bh_registry, bh_upload_tool = bare_bloodhound.load_bloodhound_mcp_surface()
    if require_bloodhound and (not bh_specs or bh_upload_tool is None):
        raise RuntimeError(
            "the true-naive Gate 1P arm requires the exact raw BloodHound query and ingest surface; "
            "set SAGE_BLOODHOUND_MCP_DIR and verify discovery"
        )
    collection_ingestor = (
        bare_bloodhound.make_collection_ingestor(bh_upload_tool)
        if bh_upload_tool is not None
        else None
    )
    mythic_exec = bare_mythic_tools.make_mythic_dispatcher(
        client,
        collection_ingestor=collection_ingestor,
    )
    bh_exec = bare_bloodhound.make_bloodhound_dispatcher(bh_registry)
    print(f"[bare] toolset: {len(bare_mythic_tools.TOOLS)} Mythic + {len(bh_specs)} BloodHound MCP tools", flush=True)

    def executor(call: dict) -> str:
        name = call.get("tool", "")
        if name in bare_mythic_tools.TOOLS:
            return mythic_exec(call)
        bh = bh_exec(call)
        if bh is not None:
            return bh
        return f"[unknown tool] {name!r}"

    return bare_runner.BareModelRunner(
        model_fn, executor,
        tools_spec=bare_mythic_tools.bare_tool_specs() + bh_specs,
        max_steps=cfg.max_steps,
        timeout=cfg.solve_timeout,
        logger=bare_runner.make_stdout_logger(),
        token_ceiling=(cfg.model_token_budget if cfg.measurement_kind == "row" else None),
        require_complete_usage=cfg.measurement_kind == "row",
    )


def compare(cfg: Config, scenario_name: str) -> None:
    p = cfg.results_path
    if not p.exists():
        raise SystemExit(f"no records at {p}; run both sides first")
    parsed_records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed_records.append(json.loads(line))
    if scenario_name in _GATE1P_PRIMARY_SCENARIOS:
        protocol = gate1p_protocol.protocol_for_scenario(scenario_name)
        if protocol.pair_id is not None and cfg.pair_id != protocol.pair_id:
            raise SystemExit("Gate 1P comparison pair identity is mismatched")
        if protocol.pair_id is None and cfg.pair_id is not None and cfg.pair_id not in protocol.pair_ids:
            raise SystemExit("Gate 1P comparison pair identity is mismatched")
        disposition = protocol.comparison_disposition(parsed_records, pair_id=cfg.pair_id)
        print(json.dumps({"pair_disposition": disposition}, sort_keys=True))
        if disposition.get("countable") is not True:
            raise SystemExit(f"Gate 1P pair is not comparison-countable: {disposition.get('reason')}")
        comparison_inputs = disposition["comparison_inputs"]
        bare, harness = [], []
        for item in comparison_inputs:
            card = ScoreCard(**item["card"])
            card.cost_countable = dict(item["cost_countable"])
            (harness if item["side"] == "harness" else bare).append(card)
    else:
        bare, harness = [], []
        for rec in parsed_records:
            if rec.get("record_type") != "result" or rec.get("scenario") != scenario_name:
                continue
            if scenario_name in _GATE1P_PRIMARY_SCENARIOS and rec.get("measurement_kind") != "row":
                continue
            card = ScoreCard(**rec["card"])
            card.cost_countable = dict(rec.get("cost_countable") or {})
            (harness if rec["side"] == "harness" else bare).append(card)
    if not bare or not harness:
        raise SystemExit(f"need >=1 bare and >=1 harness record for {scenario_name} "
                         f"(have bare={len(bare)} harness={len(harness)})")
    cmp = bare_runner.compare_bare_vs_harness(scenario_name, bare, harness)
    print(f"[{scenario_name}] verdict={cmp.verdict} delta={cmp.delta:+.3f} "
          f"(harness={cmp.harness_objective_success_rate:.3f} "
          f"bare={cmp.bare_objective_success_rate:.3f} mde={cmp.min_detectable_effect:.3f})")


def main(argv=None) -> int:
    from dotenv_bootstrap import load_sage_dotenv

    applied = load_sage_dotenv(str(Path(__file__).resolve().parents[2]))
    try:
        sage_model_environment = {
            key: value
            for key in ("API_ENDPOINT", "API_KEY")
            if (value := str(os.environ.get(key) or "").strip())
        }
    finally:
        for key in applied:
            os.environ.pop(key, None)
    ap = argparse.ArgumentParser(description="Live bare-vs-harness gauge (GOAD). Reset before EVERY --go run.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run ONE side on ONE scenario (range must be freshly reset)")
    r.add_argument("--side", choices=["harness", "bare"], required=True)
    r.add_argument("--scenario", required=True)
    r.add_argument("--go", action="store_true", help="actually run live offensive tooling")
    r.add_argument("--sage-cb", type=int, default=None)
    r.add_argument("--apollo-cb", type=int, default=None)
    r.add_argument("--solve-timeout", type=int, default=None,
                   help="seconds to wait for the harness solve (default 1800=30min); raise for full solves")
    r.add_argument("--policy-mode", choices=["llm", "hybrid", "symbolic"], default="llm",
                   help="policy identity of the running Sage harness")
    r.add_argument("--provider", default=None,
                   help="explicit harness model provider; required for controlled multi-model runs")
    r.add_argument("--model", default=None,
                   help="explicit harness model ID; required for controlled multi-model runs")
    r.add_argument("--null-model", action="store_true",
                   help="headless ablation: disable the policy model after Model initialization")
    r.add_argument("--measurement-kind", choices=["legacy", "diagnostic", "row"], default="legacy",
                   help="measurement admission class; Gate 1P row mode requires the exact paired protocol")
    r.add_argument("--protocol-id", default=None,
                   help="concise prospective protocol identity for diagnostic/row attempts")
    r.add_argument("--pair-id", default=None,
                   help="prospective Gate 1P pair identity")
    r.add_argument("--arm-index", type=int, choices=[0, 1], default=None,
                   help="Gate 1P pair arm: 0=shipping Sage, 1=true naive")
    r.add_argument("--model-token-budget", type=int, default=gate1p_stagea.TOKEN_ADMISSION_CEILING)
    r.add_argument("--model-context-limit", type=int, default=None)
    r.add_argument("--max-output-tokens", type=int, default=None)
    r.add_argument("--source-version", default=None,
                   help="exact tested source version; may also come from SAGE_EVAL_SOURCE_VERSION")
    r.add_argument(
        "--live-authority-id",
        default=None,
        help="separate explicit live authority identifier; required with --go in paired-row mode",
    )
    r.add_argument("--da-settle-timeout", type=int, default=None,
                   help="seconds the DA probes poll for GPO/SYSTEM-on-DC membership to propagate before "
                        "scoring (default 300=5min). Returns True the instant it appears; 0 = immediate.")
    c = sub.add_parser("compare", help="combine recorded ScoreCards for a scenario into a verdict")
    c.add_argument("--scenario", required=True)
    c.add_argument("--pair-id", default=None)
    pf = sub.add_parser("preflight",
                        help="fast (<5s) check that the scenario objective is completion-recognizable; "
                             "run BEFORE a reset+solve so a dropped/opaque objective fails cheap, not after 60 min")
    pf.add_argument("--scenario", required=True)
    pf.add_argument("--provider", default=None, help="exact model provider to authenticate")
    pf.add_argument("--model", default=None, help="exact model ID to authenticate")
    pf.add_argument("--max-output-tokens", type=int, default=None,
                    help="episode output cap; the preflight itself is capped at eight tokens")
    sub.add_parser(
        "verify-no-running-tasks",
        help="read the current Mythic operation task ledger and fail unless every task is completed",
    )
    args = ap.parse_args(argv)

    cfg = Config()
    cfg.model_api_endpoint = sage_model_environment.get("API_ENDPOINT") or None
    cfg.model_api_key = sage_model_environment.get("API_KEY") or None
    if getattr(args, "provider", None):
        cfg.model_provider = args.provider
    if getattr(args, "model", None):
        cfg.model_id = args.model
    if getattr(args, "max_output_tokens", None) is not None:
        cfg.max_output_tokens = args.max_output_tokens

    if args.cmd == "verify-no-running-tasks":
        rows = live_seams.mythic_operation_task_states()
        malformed = [row for row in rows if not isinstance(row.get("completed"), bool)]
        running = [row for row in rows if row.get("completed") is False]
        if malformed or running:
            print(
                json.dumps({
                    "task_quiescent": False,
                    "malformed_task_ids": [row.get("display_id") for row in malformed],
                    "running_task_ids": [row.get("display_id") for row in running],
                }, sort_keys=True),
                flush=True,
            )
            return 2
        print(json.dumps({"task_quiescent": True, "task_count": len(rows)}, sort_keys=True), flush=True)
        return 0

    if args.cmd == "preflight":
        # Iterate the DECLARED manifest and run every OFFLINE precondition before any expensive step.
        # Exit non-zero on ANY failure so orchestrate aborts before spending a reset (fail-cheap-first).
        failures = []
        offline = [(n, c) for (n, cost, c) in scenario_preconditions(cfg, args.scenario)
                   if cost in ("offline", "smoke")]
        for name, check in offline:
            try:
                ok, detail = check()
            except Exception as e:
                ok, detail = False, f"check raised: {e}"
            print(f"[preflight/{args.scenario}] {'OK  ' if ok else 'FAIL'} {name}: {detail}", flush=True)
            if not ok:
                failures.append(name)
        if failures:
            print(f"[preflight/{args.scenario}] {len(failures)} precondition(s) FAILED before any reset: "
                  f"{failures} — fix these (they are all knowable offline) then re-run.", flush=True)
            return 2
        print(f"[preflight/{args.scenario}] all {len(offline)} offline preconditions passed", flush=True)
        return 0
    if getattr(args, "sage_cb", None) is not None:
        cfg.sage_cb = args.sage_cb
    if getattr(args, "apollo_cb", None) is not None:
        cfg.apollo_cb = args.apollo_cb
    if getattr(args, "solve_timeout", None) is not None:
        cfg.solve_timeout = args.solve_timeout
    if getattr(args, "da_settle_timeout", None) is not None:
        cfg.da_settle_timeout = args.da_settle_timeout
    if getattr(args, "policy_mode", None):
        cfg.policy_mode = args.policy_mode
    if getattr(args, "provider", None):
        cfg.model_provider = args.provider
    if getattr(args, "model", None):
        cfg.model_id = args.model
    if getattr(args, "null_model", False):
        cfg.null_model = True
    if getattr(args, "measurement_kind", None):
        cfg.measurement_kind = args.measurement_kind
    if getattr(args, "protocol_id", None):
        cfg.protocol_id = args.protocol_id
    if getattr(args, "pair_id", None):
        cfg.pair_id = args.pair_id
    if getattr(args, "arm_index", None) is not None:
        cfg.arm_index = args.arm_index
    if getattr(args, "model_token_budget", None) is not None:
        cfg.model_token_budget = args.model_token_budget
    if getattr(args, "model_context_limit", None) is not None:
        cfg.model_context_limit = args.model_context_limit
    if getattr(args, "max_output_tokens", None) is not None:
        cfg.max_output_tokens = args.max_output_tokens
    if getattr(args, "source_version", None):
        cfg.source_version = args.source_version
    if getattr(args, "live_authority_id", None):
        cfg.live_authority_id = args.live_authority_id

    if cfg.measurement_kind == "diagnostic":
        if args.scenario != "child-domain-control" or args.side != "harness":
            ap.error("the approved Gate 1P diagnostic is exactly harness/child-domain-control")
        if not cfg.protocol_id:
            ap.error("--protocol-id is required for the Gate 1P diagnostic")
    selected_protocol = None
    if cfg.measurement_kind == "row" and args.scenario in _GATE1P_PRIMARY_SCENARIOS:
        try:
            selected_protocol = gate1p_protocol.protocol_for_request(
                protocol_id=cfg.protocol_id,
                scenario=args.scenario,
            )
            if cfg.pair_id is not None or selected_protocol.pair_id is not None:
                selected_protocol.validate_request(
                    require_allocation=False,
                    protocol_id=cfg.protocol_id,
                    pair_id=cfg.pair_id,
                    scenario=args.scenario,
                    side=args.side,
                    arm_index=cfg.arm_index,
                    provider=cfg.model_provider,
                    model=cfg.model_id,
                    policy_mode=cfg.policy_mode,
                    solve_timeout=cfg.solve_timeout,
                    token_ceiling=cfg.model_token_budget,
                    model_context_limit=cfg.model_context_limit,
                    max_output_tokens=cfg.max_output_tokens,
                    source_version=cfg.source_version,
                    api_endpoint=cfg.model_api_endpoint,
                )
        except gate1p_stagea.ProtocolError as exc:
            ap.error(str(exc))
        if args.cmd == "run" and args.go and not selected_protocol.pair_allocated:
            ap.error(
                f"{selected_protocol.protocol_id} is unallocated; live execution is not authorized"
            )
        if (
            args.cmd == "run" and args.go and selected_protocol.pair_id is None
            and cfg.pair_id not in selected_protocol.pair_ids
        ):
            ap.error("allocated matrix row execution requires one exact --pair-id")
        if args.cmd == "run" and args.go and cfg.live_authority_id != selected_protocol.allocation_id:
            ap.error(
                f"live paired-row execution requires separate --live-authority-id "
                f"{selected_protocol.allocation_id}"
            )

    if args.cmd == "compare":
        compare(cfg, args.scenario)
        return 0

    if not args.go:
        if (
            selected_protocol is not None
            and (not selected_protocol.pair_allocated or cfg.pair_id is None)
        ):
            print(json.dumps({"prospective_protocol": selected_protocol.assembled_contract()}, sort_keys=True))
            print(
                f"{'ALLOCATED' if selected_protocol.pair_allocated else 'UNALLOCATED'}: "
                "protocol assembly only. No reset, arm, model, Mythic, "
                "BloodHound, range, target, or comparison call is authorized."
            )
            return 0
        d = live_seams.load_sage_defaults()
        print(f"DRY RUN (no --go). Would run side={args.side} scenario={args.scenario}")
        print(f"  RESET the range first ({{sage-goad-reset}}) — each run needs a clean lab.")
        print(f"  bare model (Sage .env): provider={d['provider']} model={d['model']}")
        print(f"  probes (all collection-independent): KRBTGT_DUMPED (Mythic loot), "
              f"DA_CHILD/OBJECTIVE (AD-direct: live DC Domain Admins vs post-reset baseline)")
        print(f"  objective: {_scenario(cfg, args.scenario).objective}")
        print(f"  measurement: {cfg.measurement_kind} protocol={cfg.protocol_id or 'none'}")
        if cfg.null_model:
            print("  treatment: null policy model (headless; no policy inference calls)")
        return 0

    try:
        run_side(cfg, args.side, args.scenario)
    except KeyboardInterrupt:
        # Backstop for a Ctrl-C OUTSIDE the model loop / harness wait (e.g. during baseline LDAP or
        # scoring); interrupts inside those are already caught and scored. Exit cleanly, no traceback.
        print("\n⎈ interrupted by operator — exiting cleanly.", flush=True)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
