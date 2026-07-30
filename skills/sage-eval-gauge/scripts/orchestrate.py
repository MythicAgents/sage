#!/usr/bin/env python3
"""Unattended orchestrator for bare-vs-harness gauge runs on a FRESH range.

Chains, with guards + per-step timeouts + abort-on-failure (so the gauge never runs on a half-reset
range and produces lies): full reset -> bootstrap callbacks -> readiness ->
discover callbacks -> run ONE side -> record.  A scenario sweep does: reset+harness, reset+bare, compare.

⚠️  RUNS LIVE OFFENSIVE TOOLING + resets the lab with `--go`. Intended for Codex/operator UNATTENDED
    iteration. Safe without `--go`: prints the exact command plan (your attended-cycle runbook).
The reset path is intentionally lab-specific: it restores the Apollo-staged Ludus snapshot, imports the
retained Apollo callback export, clears the snapshot's localuser session, opens Samwell's RDP session, and
starts the preserved SageApolloBootstrap task. It never rebuilds or transfers Apollo during a gauge reset.

Run from the sage repo root:  .venv/bin/python skills/sage-eval-gauge/scripts/orchestrate.py --help
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]            # repository root
WORKSPACE = ROOT.parent                               # sibling checkouts beside Sage
PAYLOAD = ROOT / "Payload_Type" / "sage"
PY = str(ROOT / ".venv" / "bin" / "python")
BH = os.environ.get("SAGE_BLOODHOUND_MCP_DIR") or str(WORKSPACE / "bloodhound_mcp")
DEFAULT_SNAPSHOT = "sage-seed-apollo-staged-20260710"
DEFAULT_RETAINED_CALLBACK_CONFIG = (
    ROOT / "skills" / "sage-callback-bootstrap" / "apollo_callback_config.json"
)
DEFAULT_ROUTE_ENV = ROOT / "skills" / "sage-eval-gauge" / ".env.local"
DEFAULT_ENGAGEMENT_NETBIOS_MAP = (
    '{"NORTH":"north.sevenkingdoms.local",'
    '"SEVENKINGDOMS":"sevenkingdoms.local",'
    '"ESSOS":"essos.local"}'
)
DEFAULT_PURPOSE_RANGE_NETBIOS_MAP = '{"RANGE":"range.local"}'
DEFAULT_REPLICATION_PURPOSE_RANGE_NETBIOS_MAP = '{"REPLICATION":"replication.local"}'


def _active_laps_contract():
    sys.path.insert(0, str(PAYLOAD))
    from ai.hillclimb import laps_family_transfer_holdout as laps_contract  # type: ignore

    return laps_contract


def _active_trust_context_contract():
    sys.path.insert(0, str(PAYLOAD))
    from ai.hillclimb import trust_context_corroboration as trust_contract  # type: ignore

    return trust_contract


def _active_phase8_goad_regression_contract():
    sys.path.insert(0, str(PAYLOAD))
    from ai.hillclimb import phase8_goad_regression as phase8_contract  # type: ignore

    return phase8_contract


def _laps_family_transfer_netbios_map() -> str:
    laps_contract = _active_laps_contract()
    mapping = {laps_contract.ROOT_NETBIOS: laps_contract.ROOT_DOMAIN}
    mapping.update({
        domain.split(".", 1)[0].upper(): domain
        for domain in laps_contract.LAPS_FAMILY_TRANSFER_HOLDOUT.child_domains
    })
    return json.dumps(mapping, separators=(",", ":"), sort_keys=True)


def _laps_family_transfer_forced_path_names() -> list[str]:
    laps_contract = _active_laps_contract()
    return [item.name for item in laps_contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths]


def _trust_context_netbios_map() -> str:
    trust_contract = _active_trust_context_contract()
    mapping = {
        trust_contract.ROOT_NETBIOS: trust_contract.ROOT_DOMAIN,
        trust_contract.CHILD_NETBIOS: trust_contract.CHILD_DOMAIN,
        trust_contract.TRUSTED_NETBIOS: trust_contract.TRUSTED_DOMAIN,
    }
    return json.dumps(mapping, separators=(",", ":"), sort_keys=True)


DEFAULT_LAPS_FAMILY_TRANSFER_NETBIOS_MAP = _laps_family_transfer_netbios_map()
DEFAULT_TRUST_CONTEXT_NETBIOS_MAP = _trust_context_netbios_map()
DEFAULT_PURPOSE_RANGE_GPO_PROOF_ENV = {
    "SAGE_GPO_PROOF_SHARE_NAME": "SageProof",
    "SAGE_GPO_PROOF_LOCAL_ROOT": r"C:\SageProof",
    # SRV02 is provisioned with a one-minute computer GP refresh and zero random offset.
    # Keep the live discriminator conservative but avoid paying the generic five-minute wait twice per GPO run.
    "SAGE_GPO_WAIT_SECONDS": "120",
}
DEFAULT_PURPOSE_RANGE_RECOVERY_BLOCKER_ENV = {
    "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON": json.dumps(
        {
            "capability": "gpo-controlled-system-exec",
            "target_contains": "gpo=srv02-policy;domain=range.local",
            "reason": "endpoint protection blocked the staged GPO payload on srv02",
            "probe": {
                "defender_blocked": True,
                "target_domain": "range.local",
                "target_host": "srv02",
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ),
}
DEFAULT_DIRECT_LAPS_CA_EXPORT_RECOVERY_ENV = {
    "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON": json.dumps(
        {
            "capability": "adcs-ca-private-key-export",
            "target_contains": "target=braavos;target_domain=essos.local",
            "reason": "key not exportable",
            "probe": {
                "key_not_exportable": True,
                "target_domain": "essos.local",
                "target_host": "braavos",
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ),
    # This keeps the live benchmark about post-blocker policy recovery rather than ADCS discovery.
    # Sage consumes only the generic hint schema; the GOAD literals stay in this eval harness.
    "SAGE_EVAL_ADCS_ESC_ENROLLMENT_HINTS_JSON": json.dumps(
        [
            {
                "domain": "essos.local",
                "ca_host": "braavos",
                "ca_name": r"braavos.essos.local\ESSOS-CA",
                "template": "ESC1",
                "esc_type": "esc1",
            },
        ],
        separators=(",", ":"),
        sort_keys=True,
    ),
}
DEFAULT_PURPOSE_RANGE_CA_EXPORT_REPLANNING_ENV = {
    "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON": json.dumps(
        [
            {
                "capability": "read-managed-local-admin-secret",
                "target_contains": "target=ca01;target_domain=range.local",
            },
            {
                "capability": "use-managed-local-admin-secret",
                "target_contains": "target=ca01;target_domain=range.local",
            },
            {
                "capability": "execute-as-local-admin",
                "target_contains": "target=ca01;target_domain=range.local",
            },
            {
                "capability": "adcs-ca-private-key-export",
                "target_contains": "target=ca01;target_domain=range.local",
                "release_on_failure": True,
            },
        ],
        separators=(",", ":"),
        sort_keys=True,
    ),
    "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON": json.dumps(
        {
            "capability": "adcs-ca-private-key-export",
            "target_contains": "target=ca01;target_domain=range.local",
            "reason": "endpoint protection blocked CA export tooling on ca01",
            "failure_class": "transient",
            "skip_if_achieved_effect": "endpoint-protection-adjusted:ca01@range.local",
            "probe": {
                "tool_execution_failed": True,
                "defender_blocked": True,
                "target_domain": "range.local",
                "target_host": "ca01",
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ),
}
DEFAULT_PURPOSE_RANGE_GPO_DC_SCOPE_LATE_BLOCKER_ENV = {
    # Keep the live canary's packet/frontier identity aligned with the generic offline contract.
    # The existing purpose-range variants shorten this wait for throughput, but this benchmark is
    # explicitly about equal visible GPO costs and must preserve the default 300-second metadata.
    "SAGE_GPO_WAIT_SECONDS": "300",
    "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON": json.dumps(
        [
            {
                "capability": "read-managed-local-admin-secret",
                "target_contains": "target=ca01;target_domain=range.local",
            },
            {
                "capability": "use-managed-local-admin-secret",
                "target_contains": "target=ca01;target_domain=range.local",
            },
            {
                "capability": "execute-as-local-admin",
                "target_contains": "target=ca01;target_domain=range.local",
            },
            {
                "capability": "adcs-ca-private-key-export",
                "target_contains": "target=ca01;target_domain=range.local",
            },
            {
                "capability": "adcs-certificate-auth",
                "target_contains": "domain=range.local;account=administrator;ca_host=ca01",
                "release_on_failure": True,
            },
        ],
        separators=(",", ":"),
        sort_keys=True,
    ),
    "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON": json.dumps(
        {
            "capability": "adcs-certificate-auth",
            "target_contains": "domain=range.local;account=administrator;ca_host=ca01",
            "reason": "certificate authentication failed after verified CA export on ca01",
            "failure_class": "genuine",
            "record_failed_effect": "certificate-auth:administrator@range.local",
            "probe": {
                "pkinit_failed": True,
                "target_domain": "range.local",
                "target_host": "ca01",
                "account": "administrator",
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ),
}
DEFAULT_LUDUS_RANGE_ID = os.environ.get("SAGE_LUDUS_RANGE_ID") or None
DEFAULT_LUDUS_MCP_SERVER = os.environ.get("SAGE_LUDUS_MCP_SERVER") or None
DEFAULT_FOOTHOLD_HOST = "CASTELBLACK"
DEFAULT_FOOTHOLD_IP = "10.4.10.22"
DEFAULT_FOOTHOLD_USER = r"NORTH\samwell.tarly"
DEFAULT_FOOTHOLD_CALLBACK_HOST = DEFAULT_FOOTHOLD_HOST
DEFAULT_FOOTHOLD_CALLBACK_USER = "samwell.tarly"
DEFAULT_FOOTHOLD_PASSWORD_ENV = "SAGE_RUN_AS_PASSWORD"
PHASE6_CALLBACK_SETTLE_SECONDS = 90
PHASE6_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES = 1
PHASE7_CALLBACK_SETTLE_SECONDS = 90


class FootholdSpec:
    def __init__(
        self,
        host: str = DEFAULT_FOOTHOLD_HOST,
        ip: str = DEFAULT_FOOTHOLD_IP,
        user: str = DEFAULT_FOOTHOLD_USER,
        callback_host: str | None = None,
        callback_user: str = DEFAULT_FOOTHOLD_CALLBACK_USER,
        password_env: str = DEFAULT_FOOTHOLD_PASSWORD_ENV,
        ludus_range_id: str | None = DEFAULT_LUDUS_RANGE_ID,
        ludus_mcp_server: str | None = DEFAULT_LUDUS_MCP_SERVER,
        callback_settle_seconds: int = 0,
        require_unique_callback: bool = False,
    ) -> None:
        self.host = host
        self.ip = ip
        self.user = user
        self.callback_host = callback_host or host
        self.callback_user = callback_user
        self.password_env = password_env
        self.ludus_range_id = ludus_range_id
        self.ludus_mcp_server = ludus_mcp_server
        self.callback_settle_seconds = max(0, int(callback_settle_seconds))
        self.require_unique_callback = bool(require_unique_callback)

    def launch_argv(self) -> list[str]:
        argv = [
            "/bin/bash",
            "skills/sage-mythic-payload-deploy/scripts/launch_apollo_foothold.sh",
            self.ip,
            self.user,
            "--",
            "--target-host",
            self.host,
            "--callback-host",
            self.callback_host,
            "--callback-user",
            self.callback_user,
        ]
        if self.ludus_range_id:
            argv.extend(["--ludus-range-id", self.ludus_range_id])
        if self.ludus_mcp_server:
            argv.extend(["--ludus-mcp-server", self.ludus_mcp_server])
        if self.callback_settle_seconds > 0:
            argv.extend(["--callback-settle-seconds", str(self.callback_settle_seconds)])
        if self.require_unique_callback:
            argv.append("--require-unique-callback")
        return argv

    def launch_env(self) -> dict[str, str]:
        env = dict(os.environ)
        password = _resolve_password_source(self.password_env)
        if password:
            env["SAGE_RUN_AS_PASSWORD"] = password
        if self.ludus_range_id:
            env["SAGE_LUDUS_RANGE_ID"] = self.ludus_range_id
        if self.ludus_mcp_server:
            env["SAGE_LUDUS_MCP_SERVER"] = self.ludus_mcp_server
        return env

    def readiness_argv(self) -> list[str]:
        return [
            PY,
            "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py",
            "readiness",
            "--runtime-dbs-archived",
            "--foothold-host",
            self.callback_host,
            "--foothold-user-match",
            self.callback_user,
        ]

    def label(self) -> str:
        return f"{self.host}/{self.user}"


def _configure_foothold_for_scenario(scenario: str, foothold: FootholdSpec | None = None) -> FootholdSpec:
    foothold = foothold or FootholdSpec()
    if not (
        scenario.startswith("laps-family-transfer-")
        or scenario == _active_trust_context_contract().SCENARIO_NAME
    ):
        return foothold
    return FootholdSpec(
        host=foothold.host,
        ip=foothold.ip,
        user=foothold.user,
        callback_host=foothold.callback_host,
        callback_user=foothold.callback_user,
        password_env=foothold.password_env,
        ludus_range_id=foothold.ludus_range_id,
        ludus_mcp_server=foothold.ludus_mcp_server,
        callback_settle_seconds=max(
            foothold.callback_settle_seconds,
            PHASE6_CALLBACK_SETTLE_SECONDS
            if scenario.startswith("laps-family-transfer-")
            else PHASE7_CALLBACK_SETTLE_SECONDS,
        ),
        require_unique_callback=True,
    )

# (name, argv, cwd, timeout_s). Reset + bootstrap, in order, per sage-goad-reset + sage-callback-bootstrap.
RESET_STEPS = [
    ("stop sage",        ["/bin/bash", "skills/sage-goad-reset/scripts/sage_stop.sh"], ROOT, 120),
    ("archive dbs",      [PY, "skills/sage-goad-reset/scripts/archive_runtime_dbs.py"], ROOT, 180),
    ("reset mythic",     ["/bin/bash", "skills/sage-goad-reset/scripts/mythic_reset.sh", "--yes"], ROOT, 900),
    ("ludus rollback",   [PY, "skills/sage-goad-reset/scripts/ludus.py", "rollback", "--yes"], ROOT, 2400),
    ("ludus poweron",    [PY, "skills/sage-goad-reset/scripts/ludus.py", "poweron", "all"], ROOT, 900),
    ("sync range time",  [PY, "skills/sage-goad-reset/scripts/sync_range_time.py", "sync", "--yes"], ROOT, 900),
    ("restart sage",     ["/bin/bash", "skills/sage-goad-reset/scripts/sage_restart.sh",
                          "SAGE_ENGAGEMENT_GATE=1", f"SAGE_BLOODHOUND_MCP_DIR={BH}"], ROOT, 240),
    ("wipe bloodhound",  ["uv", "--directory", BH, "run", "python",
                          str(ROOT / "skills/sage-goad-reset/scripts/bh_reset.py"), "wipe", "--yes"], ROOT, 180),
    (
        "bootstrap foothold",
        [
            PY,
            "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py",
            "bootstrap-reset",
            "--use-retained-callback",
            "--retained-callback-config",
            str(DEFAULT_RETAINED_CALLBACK_CONFIG),
        ],
        ROOT,
        900,
    ),
]
LUDUS_STATUS = [PY, "skills/sage-goad-reset/scripts/ludus.py", "status"]
CALLBACKS = [PY, "skills/sage-live-runner/scripts/sage_task.py", "callbacks"]


def _ludus_argv(
    *args: str,
    range_id: str | None = None,
    mcp_server: str | None = None,
) -> list[str]:
    argv = [PY, "skills/sage-goad-reset/scripts/ludus.py"]
    if mcp_server:
        argv.extend(["--mcp-server", mcp_server])
    if range_id:
        argv.extend(["--range-id", range_id])
    argv.extend(args)
    return argv


def _sync_range_time_argv(
    *args: str,
    range_id: str | None = None,
    mcp_server: str | None = None,
) -> list[str]:
    argv = [PY, "skills/sage-goad-reset/scripts/sync_range_time.py"]
    if mcp_server:
        argv.extend(["--mcp-server", mcp_server])
    if range_id:
        argv.extend(["--range-id", range_id])
    argv.extend(args)
    return argv


def _range_env(
    range_id: str | None = None,
    mcp_server: str | None = None,
) -> dict[str, str] | None:
    if not range_id and not mcp_server:
        return None
    env = dict(os.environ)
    if range_id:
        env["SAGE_LUDUS_RANGE_ID"] = range_id
    if mcp_server:
        env["SAGE_LUDUS_MCP_SERVER"] = mcp_server
    return env


def _engagement_netbios_map(scenario: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if scenario == _active_trust_context_contract().SCENARIO_NAME:
        return DEFAULT_TRUST_CONTEXT_NETBIOS_MAP
    if scenario.startswith("laps-family-transfer-"):
        return DEFAULT_LAPS_FAMILY_TRANSFER_NETBIOS_MAP
    if scenario.startswith("replication-purpose-range-"):
        return DEFAULT_REPLICATION_PURPOSE_RANGE_NETBIOS_MAP
    if scenario.startswith("purpose-range-"):
        return DEFAULT_PURPOSE_RANGE_NETBIOS_MAP
    return DEFAULT_ENGAGEMENT_NETBIOS_MAP


def _scenario_restart_env(scenario: str) -> dict[str, str]:
    if scenario.startswith(("purpose-range-", "replication-purpose-range-")):
        env = dict(DEFAULT_PURPOSE_RANGE_GPO_PROOF_ENV)
        if scenario == "purpose-range-ca-export-replanning":
            env.update(DEFAULT_PURPOSE_RANGE_CA_EXPORT_REPLANNING_ENV)
        elif scenario == "purpose-range-gpo-dc-scope-late-blocker":
            env.update(DEFAULT_PURPOSE_RANGE_GPO_DC_SCOPE_LATE_BLOCKER_ENV)
        elif scenario == "purpose-range-recovery":
            env.update(DEFAULT_PURPOSE_RANGE_RECOVERY_BLOCKER_ENV)
        return env
    if scenario == "direct-laps-ca-export-recovery":
        return dict(DEFAULT_DIRECT_LAPS_CA_EXPORT_RECOVERY_ENV)
    return {}


def _phase6_laps_eval_env(
    scenario: str,
    *,
    forced_path: str | None,
    callback_id: int,
    planned_row_id: str | None = None,
    attempt_index: int | None = None,
) -> dict[str, str]:
    """Return frozen Phase 6 row metadata plus an optional exact-target forced prefix.

    The prefix is built only after callback discovery because the exact target strings include
    the live callback display ID. Keeping that derivation here avoids hardcoded callback IDs in
    operator commands while still making every forced label exact-target and non-creditable.
    """
    if not scenario.startswith("laps-family-transfer-"):
        if forced_path or planned_row_id or attempt_index is not None:
            raise SystemExit(
                "ABORT: --laps-forced-path/--phase6-planned-row-id/--phase6-attempt-index "
                "are only valid for laps-family-transfer-* scenarios"
            )
        return {}
    if bool(planned_row_id) != (attempt_index is not None):
        raise SystemExit(
            "ABORT: --phase6-planned-row-id and --phase6-attempt-index must be provided together"
        )
    if planned_row_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", planned_row_id):
        raise SystemExit("ABORT: --phase6-planned-row-id must be a stable token without whitespace")
    if attempt_index is not None and int(attempt_index) < 1:
        raise SystemExit("ABORT: --phase6-attempt-index must be >= 1")
    laps_contract = _active_laps_contract()
    from ai.langgraph import capabilities as laps_capabilities  # type: ignore

    frontier = list(laps_capabilities.actions_from_state(laps_contract.synthetic_collected_state()))
    env = {
        "SAGE_EVAL_PHASE6_MANIFEST_HASH": laps_contract.sealed_manifest()["manifest_hash"],
        "SAGE_EVAL_PHASE6_TOPOLOGY_HASH": laps_contract.topology_hash(),
        "SAGE_EVAL_PHASE6_CANDIDATE_SET_HASH": laps_contract.canonical_candidate_set_hash(frontier),
        "SAGE_EVAL_PHASE6_ORDERED_FRONTIER_HASH": laps_contract.canonical_ordered_frontier_hash(frontier),
    }
    if planned_row_id is not None and attempt_index is not None:
        env["SAGE_EVAL_PHASE6_PLANNED_ROW_ID"] = planned_row_id
        env["SAGE_EVAL_PHASE6_ATTEMPT_INDEX"] = str(int(attempt_index))
        env["SAGE_EVAL_PHASE6_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES"] = str(
            PHASE6_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES
        )
    if not forced_path:
        return env
    path = next(
        (
            item
            for item in laps_contract.LAPS_FAMILY_TRANSFER_HOLDOUT.forced_paths
            if item.name == forced_path
        ),
        None,
    )
    if path is None:
        raise SystemExit(f"ABORT: unknown Phase 6 forced path: {forced_path!r}")
    host = path.first_host.casefold()
    domain = path.first_domain.casefold()
    env["SAGE_EVAL_PHASE6_FORCED_PATH"] = path.name
    env["SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON"] = json.dumps(
        [
            {
                "capability": "read-managed-local-admin-secret",
                "exact_target": (
                    f"account=user1;account_domain={laps_contract.ROOT_DOMAIN};"
                    f"target={host};target_domain={domain};callback={callback_id}"
                ),
                "intervention_id": f"phase6-{scenario}-{path.name}-read",
            },
            {
                "capability": "use-managed-local-admin-secret",
                "exact_target": f"target={host};target_domain={domain};callback={callback_id}",
                "intervention_id": f"phase6-{scenario}-{path.name}-use",
            },
            {
                "capability": "execute-as-local-admin",
                "exact_target": f"target={host};target_domain={domain};callback={callback_id}",
                "intervention_id": f"phase6-{scenario}-{path.name}-exec",
            },
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    return env


def _phase7_trust_context_eval_env(
    scenario: str,
    *,
    control: str | None,
    attempt_index: int | None,
) -> dict[str, str]:
    trust_contract = _active_trust_context_contract()
    if scenario != trust_contract.SCENARIO_NAME:
        if control or attempt_index is not None:
            raise SystemExit(
                "ABORT: --phase7-control/--phase7-attempt-index are only valid for "
                f"{trust_contract.SCENARIO_NAME}"
            )
        return {}
    if not control:
        raise SystemExit(
            f"ABORT: {trust_contract.SCENARIO_NAME} requires --phase7-control"
        )
    allowed_controls = set(trust_contract.LIVE_ROW_CONTROLS)
    if control not in allowed_controls:
        raise SystemExit(
            f"ABORT: Phase 7 gauge rows are positive-only; {control!r} is not allowed. "
            "Run trust-context-corroboration-control-validate for graph-only/missing/stale controls."
        )
    if attempt_index is None or int(attempt_index) < 1:
        raise SystemExit("ABORT: Phase 7 positive rows require --phase7-attempt-index >= 1")
    env = {
        "SAGE_EVAL_PHASE7_MANIFEST_HASH": trust_contract.sealed_manifest()["manifest_hash"],
        "SAGE_EVAL_PHASE7_TOPOLOGY_HASH": trust_contract.topology_hash(),
        "SAGE_EVAL_PHASE7_CONTROL": control,
    }
    env["SAGE_EVAL_PHASE7_ATTEMPT_INDEX"] = str(int(attempt_index))
    return env


def _phase8_goad_regression_eval_env(
    scenario: str,
    *,
    policy_mode: str,
    policy_arm: str | None,
    planned_row_id: str | None,
    attempt_index: int | None,
) -> dict[str, str]:
    supplied = bool(policy_arm or planned_row_id or attempt_index is not None)
    if not supplied:
        return {}
    phase8_contract = _active_phase8_goad_regression_contract()
    if scenario != phase8_contract.SCENARIO_NAME:
        raise SystemExit(
            "ABORT: --phase8-policy-arm/--phase8-planned-row-id/--phase8-attempt-index "
            f"are only valid for {phase8_contract.SCENARIO_NAME}"
        )
    if not policy_arm or not planned_row_id or attempt_index is None:
        raise SystemExit(
            "ABORT: Phase 8 tagged rows require --phase8-policy-arm, "
            "--phase8-planned-row-id, and --phase8-attempt-index together"
        )
    arm = str(policy_arm).strip().casefold()
    mode = str(policy_mode).strip().casefold()
    if arm not in {*phase8_contract.EXPECTED_POLICY_ARMS, *phase8_contract.OPTIONAL_POLICY_ARMS}:
        raise SystemExit(f"ABORT: unsupported Phase 8 policy arm: {policy_arm!r}")
    if arm != mode:
        raise SystemExit(
            f"ABORT: Phase 8 policy arm {arm!r} must match configured --policy-mode {mode!r}"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", planned_row_id):
        raise SystemExit("ABORT: --phase8-planned-row-id must be a stable token without whitespace")
    if planned_row_id not in phase8_contract.allowed_planned_row_ids():
        raise SystemExit(f"ABORT: unknown Phase 8 planned row id: {planned_row_id!r}")
    if int(attempt_index) < 1:
        raise SystemExit("ABORT: --phase8-attempt-index must be >= 1")
    return {
        "SAGE_EVAL_PHASE8_CONTRACT_HASH": phase8_contract.sealed_manifest()["manifest_hash"],
        "SAGE_EVAL_PHASE8_POLICY_ARM": arm,
        "SAGE_EVAL_PHASE8_PLANNED_ROW_ID": planned_row_id,
        "SAGE_EVAL_PHASE8_ATTEMPT_INDEX": str(int(attempt_index)),
    }


def _run(name, argv, cwd, timeout, env: dict[str, str] | None = None):
    print(f"\n=== {name} ===\n$ (cd {cwd}) {' '.join(argv)}", flush=True)
    proc = subprocess.run(argv, cwd=str(cwd), timeout=timeout, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"ABORT: step '{name}' failed (exit {proc.returncode}) — not running the gauge on a bad range.")


def _poll(name, argv, cwd, predicate, *, timeout, interval=20, env: dict[str, str] | None = None):
    print(f"\n=== poll: {name} (<= {timeout}s) ===", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=env).stdout
        if predicate(out):
            return out
        time.sleep(interval)
    raise SystemExit(f"ABORT: '{name}' not ready within {timeout}s — fix the lab before running the gauge.")


def _readiness_ok(out: str) -> bool:
    """True iff bootstrap_payloads.py `readiness` reports overall ready. That command prints a JSON object
    (`json.dumps`), so parse it and check the TOP-LEVEL `ready` key — a substring match would also catch the
    nested runtime_databases/callbacks `ready` flags and pass prematurely. Fail-safe to False (keep polling)
    on any parse error or partial output."""
    try:
        return json.loads(out).get("ready") is True
    except Exception:
        return False


def _foothold_guest_up(out: str, foothold_ip: str = DEFAULT_FOOTHOLD_IP) -> bool:
    return bool(re.search(rf"^\s*ON\s+\S+\s+ip={re.escape(foothold_ip)}\b", out, re.MULTILINE))


def _range_guests_have_ips(out: str) -> bool:
    rows = re.findall(r"^\s*(ON|off)\s+\S+\s+ip=(\S+)\b", out, re.MULTILINE | re.IGNORECASE)
    return bool(rows) and all(state.casefold() == "on" and ip.casefold() != "null" for state, ip in rows)


def _range_clock_probes_reachable(out: str) -> bool:
    """True once the WinRM clock probe can authenticate to every reported Windows guest.

    A rollback can restore guest-agent IP reporting before WinRM is ready to accept inventory credentials.
    Clock skew is expected before sync, so this gate ignores ``ready`` / ``over_limit`` and only requires a
    complete error-free probe surface before the mutating sync step runs.
    """
    try:
        payload = json.loads(out)
    except Exception:
        return False
    return bool(payload.get("hosts")) and not payload.get("errors")


def discover_callbacks(foothold: FootholdSpec | None = None) -> int:
    foothold = foothold or FootholdSpec()
    out = subprocess.run(CALLBACKS, cwd=str(ROOT), capture_output=True, text=True).stdout
    apollo = [
        int(match.group("id"))
        for match in re.finditer(
            r"id=(?P<id>\d+)\s+payloadtype=apollo\s+host=(?P<host>\S+)\s+user=(?P<user>\S+)",
            out,
        )
        if match.group("host").casefold() == foothold.callback_host.casefold()
        and foothold.callback_user.casefold() in match.group("user").casefold()
    ]
    if not apollo:
        raise SystemExit(f"ABORT: missing Apollo foothold callback for {foothold.label()}.\n{out}")
    return apollo[-1]


def _available_snapshots(
    range_id: str | None = None,
    mcp_server: str | None = None,
) -> set[str]:
    proc = subprocess.run(
        _ludus_argv("snapshots", range_id=range_id, mcp_server=mcp_server),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise SystemExit(f"ABORT: could not list Ludus snapshots: {proc.stderr.strip()}")
    try:
        _status, payload = ast.literal_eval(proc.stdout.strip())
        rows = payload.get("snapshots", [])
        return {str(row["name"]) for row in rows if row.get("name") and row.get("name") != "current"}
    except Exception as exc:
        raise SystemExit(f"ABORT: could not parse Ludus snapshots: {exc}") from exc


def _resolve_password_source(password_env: str = DEFAULT_FOOTHOLD_PASSWORD_ENV) -> str | None:
    if os.environ.get(password_env):
        return os.environ[password_env]
    candidates = [
        os.environ.get("SAGE_RUNAS_FILE"),
        str(Path.home() / ".config" / "sage" / "runas.env"),
        str(PAYLOAD / ".env"),
        os.environ.get("MYTHIC_ENV_PATH") or "",
    ]
    for value in candidates:
        path = Path(value).expanduser() if value else None
        if not path or not path.is_file():
            continue
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if not stripped.startswith(f"{password_env}="):
                continue
            value = stripped.split("=", 1)[1].strip().strip("'\"")
            if value:
                return value
    return None


def validate_reset_inputs(
    snapshot: str,
    retained_callback_config: Path,
    foothold: FootholdSpec | None = None,
    ludus_range_id: str | None = None,
    ludus_mcp_server: str | None = None,
) -> None:
    foothold = foothold or FootholdSpec()
    available_snapshots = _available_snapshots(
        ludus_range_id or foothold.ludus_range_id,
        ludus_mcp_server or foothold.ludus_mcp_server,
    )
    if snapshot not in available_snapshots:
        available = ", ".join(sorted(available_snapshots))
        raise SystemExit(f"ABORT: Ludus snapshot {snapshot!r} not found. Available: {available}")
    if not retained_callback_config.is_file():
        raise SystemExit(f"ABORT: retained Apollo callback config missing: {retained_callback_config}")
    try:
        config = json.loads(retained_callback_config.read_text())
        payload_type = config["config"]["payload_type"]["name"]
    except Exception as exc:
        raise SystemExit(f"ABORT: invalid retained callback config: {exc}") from exc
    if str(payload_type).casefold() != "apollo":
        raise SystemExit(
            f"ABORT: retained callback config is for {payload_type!r}, expected 'apollo'."
        )
    if not _resolve_password_source(foothold.password_env):
        raise SystemExit(
            f"ABORT: no durable {foothold.password_env} source for {foothold.label()}; set the "
            "environment variable or add it to Payload_Type/sage/.env or ~/.config/sage/runas.env "
            "before resetting."
        )


def load_treatment_route(path: Path, treatment: str) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"ABORT: evaluation route file missing: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    model_key = {
        "sonnet": "SAGE_EVAL_SONNET_MODEL",
        "haiku": "SAGE_EVAL_HAIKU_MODEL",
    }[treatment]
    required = (
        "SAGE_EVAL_PROVIDER",
        "SAGE_EVAL_API_ENDPOINT",
        "SAGE_EVAL_API_KEY",
        model_key,
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise SystemExit(f"ABORT: evaluation route file is missing values for: {', '.join(missing)}")
    endpoint = values["SAGE_EVAL_API_ENDPOINT"]
    host = (urlparse(endpoint).hostname or "").strip().casefold()
    if host in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            "ABORT: Sonnet/Haiku treatments may not use the loopback proxy because its effective "
            "backend is fixed independently of Sage's requested model."
        )
    return {
        "provider": values["SAGE_EVAL_PROVIDER"].strip().lower(),
        "model": values[model_key],
        "api_endpoint": endpoint,
        "api_key": values["SAGE_EVAL_API_KEY"],
    }


def full_reset_and_ready(
    restart_env: dict | None = None,
    snapshot: str = DEFAULT_SNAPSHOT,
    retained_callback_config: Path = DEFAULT_RETAINED_CALLBACK_CONFIG,
    foothold: FootholdSpec | None = None,
    ludus_range_id: str | None = None,
    ludus_mcp_server: str | None = None,
) -> tuple[None, int]:
    """Full clean reset -> readiness -> callback discovery; returns (None, apollo_cb).

    `restart_env` (e.g. {"SAGE_ENGAGEMENT_ID": <run token>, "SAGE_MODEL": <tier>}) is appended as positional
    KEY=VAL overrides to the `restart sage` step, so the relaunched Sage runs under that engagement id and
    per-config settings. This is the token/config seam the Gate Experiment needs: Sage freezes
    SAGE_ENGAGEMENT_ID at startup and writes its ledger under it, so the gauge can only read THIS run's ground
    truth if Sage was restarted with the run's token. sage_restart.sh applies KEY=VAL after the env snapshot
    (last value wins), so these override the snapshot. Which config keys actually change behavior depends on
    what Sage reads at startup (SAGE_ENGAGEMENT_ID always; model/provider if read from env; a prompt-set
    selector would need its own knob)."""
    foothold = foothold or FootholdSpec()
    ludus_range_id = ludus_range_id or foothold.ludus_range_id
    ludus_mcp_server = ludus_mcp_server or foothold.ludus_mcp_server
    range_env = _range_env(ludus_range_id, ludus_mcp_server)
    for name, argv, cwd, timeout in RESET_STEPS:
        step_argv = argv
        if name == "ludus rollback" and snapshot:
            step_argv = _ludus_argv(
                "rollback",
                snapshot,
                "--yes",
                range_id=ludus_range_id,
                mcp_server=ludus_mcp_server,
            )
        if name == "ludus poweron":
            step_argv = _ludus_argv(
                "poweron",
                "all",
                range_id=ludus_range_id,
                mcp_server=ludus_mcp_server,
            )
        if name == "sync range time":
            _poll(
                "range guests report IPs",
                _ludus_argv("status", range_id=ludus_range_id, mcp_server=ludus_mcp_server),
                ROOT,
                _range_guests_have_ips,
                timeout=1800,
                env=range_env,
            )
            _poll(
                "range WinRM clock probes respond",
                _sync_range_time_argv(
                    "check",
                    range_id=ludus_range_id,
                    mcp_server=ludus_mcp_server,
                ),
                ROOT,
                _range_clock_probes_reachable,
                timeout=600,
                env=range_env,
            )
            step_argv = _sync_range_time_argv(
                "sync",
                "--yes",
                range_id=ludus_range_id,
                mcp_server=ludus_mcp_server,
            )
        if name == "bootstrap foothold":
            step_argv = [
                PY,
                "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py",
                "bootstrap-reset",
                "--use-retained-callback",
                "--retained-callback-config",
                str(retained_callback_config),
            ]
        if name == "restart sage" and restart_env:
            step_argv = list(argv) + [f"{k}={v}" for k, v in restart_env.items()]
        _run(name, step_argv, cwd, timeout, env=range_env)
    _poll(
        f"{foothold.host} powered on",
        _ludus_argv("status", range_id=ludus_range_id, mcp_server=ludus_mcp_server),
        ROOT,
        lambda out: _foothold_guest_up(out, foothold.ip),
        timeout=1800,
        env=range_env,
    )
    _run("launch retained apollo", foothold.launch_argv(), ROOT, 900, env=foothold.launch_env())
    _poll(
        "sage chat + apollo ready",
        foothold.readiness_argv(),
        ROOT,
        _readiness_ok,
        timeout=1200,
        env=range_env,
    )
    return None, discover_callbacks(foothold)


# Seconds the per-run gauge SUBPROCESS may take ON TOP OF the solve itself — covers reset-independent
# post-solve work (DA settle polling up to ~300s, milestone probes, ScoreCard recording). The step cap is
# solve_timeout + this, floored at the historical 3600 so the default behaviour is unchanged.
_GAUGE_STEP_OVERHEAD_S = 900


def run_side(scenario: str, side: str, *, go: bool, solve_timeout: int, policy_mode: str = "llm",
             provider: str | None = None, model: str | None = None,
             null_model: bool = False,
             route_env: dict[str, str] | None = None,
             snapshot: str = DEFAULT_SNAPSHOT,
             retained_callback_config: Path = DEFAULT_RETAINED_CALLBACK_CONFIG,
             foothold: FootholdSpec | None = None,
             ludus_range_id: str | None = None,
             ludus_mcp_server: str | None = None,
             engagement_netbios_map: str | None = None,
             laps_forced_path: str | None = None,
             phase6_planned_row_id: str | None = None,
             phase6_attempt_index: int | None = None,
             phase7_control: str | None = None,
             phase7_attempt_index: int | None = None,
             phase8_policy_arm: str | None = None,
             phase8_planned_row_id: str | None = None,
             phase8_attempt_index: int | None = None) -> None:
    foothold = _configure_foothold_for_scenario(scenario, foothold)
    # Fail in seconds, not after a ~60-min range run: assert the scenario objective is completion-recognizable
    # BEFORE spending a reset + live solve. Guards the harness->Sage objective seam that shipped opaque once
    # (the gauge's read-only/seam-injected design makes that seam invisible to offline unit tests). Aborts
    # (via _run's non-zero SystemExit) on a dropped/opaque/unparseable objective.
    _run(f"preflight {scenario}", [PY, "ai/hillclimb/run_gauge_live.py", "preflight", "--scenario", scenario], PAYLOAD, 120)
    # Sage always uses the bounded execution kernel for autonomous solves. Policy mode identifies who selects
    # each semantic capability: the product LLM policy or the preserved symbolic regression baseline.
    # SAGE_ENGAGEMENT_NETBIOS_MAP is the per-engagement NetBIOS->FQDN map (range-agnostic mechanism; the GOAD
    # values live HERE in the eval harness, not in Sage code). The controller's deterministic frontier needs
    # FQDN-form principal UPNs to match BloodHound's graph (a short forest like 'north' yields
    # samwell.tarly@north, which the graph cypher can't match vs samwell.tarly@north.sevenkingdoms.local) —
    # without it the controller halts at GRAPH_COLLECTED with an empty frontier.
    restart_env = {
        "SAGE_AUTONOMOUS_CONTROLLER": "1",
        "SAGE_POLICY_MODE": policy_mode,
        "SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS": "1",
        "SAGE_ENGAGEMENT_NETBIOS_MAP": _engagement_netbios_map(scenario, engagement_netbios_map),
    }
    restart_env.update(_scenario_restart_env(scenario))
    reset_kwargs = {
        "restart_env": restart_env,
        "snapshot": snapshot,
        "retained_callback_config": retained_callback_config,
        "foothold": foothold,
        "ludus_range_id": ludus_range_id,
    }
    if ludus_mcp_server is not None:
        reset_kwargs["ludus_mcp_server"] = ludus_mcp_server
    _sage_cb, apollo_cb = full_reset_and_ready(**reset_kwargs)
    argv = [PY, "ai/hillclimb/run_gauge_live.py", "run", "--side", side, "--scenario", scenario,
            "--apollo-cb", str(apollo_cb), "--solve-timeout", str(solve_timeout),
            "--policy-mode", policy_mode]
    if provider:
        argv.extend(["--provider", provider])
    if model:
        argv.extend(["--model", model])
    if null_model:
        argv.append("--null-model")
    if go:
        argv.append("--go")
    # The subprocess cap MUST exceed the solve-timeout or it kills a still-progressing solve early (the bug
    # this fixes: a 30-min solve under a 60-min step cap was fine, but raising the solve past 60 min silently
    # hit the old fixed 3600). Scale the cap with the solve budget.
    step_timeout = max(3600, solve_timeout + _GAUGE_STEP_OVERHEAD_S)
    # Native chat runs inside the restarted Sage process, but the headless harness runs in this gauge
    # subprocess. Give both paths the same runtime/scenario environment: BloodHound must be connected before
    # Model.initialize(), and frontier normalization/blocker controls must not disappear just because the
    # solve is in-process instead of channel-backed.
    child_env = {
        **os.environ,
        "SAGE_ENGAGEMENT_GATE": "1",
        "SAGE_BLOODHOUND_MCP_DIR": BH,
        **restart_env,
    }
    if scenario.startswith("laps-family-transfer-"):
        child_env.pop("SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON", None)
        child_env.pop("SAGE_EVAL_PHASE6_FORCED_PATH", None)
        child_env.update(
            _phase6_laps_eval_env(
                scenario,
                forced_path=laps_forced_path,
                callback_id=apollo_cb,
                planned_row_id=phase6_planned_row_id,
                attempt_index=phase6_attempt_index,
            )
        )
    child_env.update(
        _phase7_trust_context_eval_env(
            scenario,
            control=phase7_control,
            attempt_index=phase7_attempt_index,
        )
    )
    child_env.update(
        _phase8_goad_regression_eval_env(
            scenario,
            policy_mode=policy_mode,
            policy_arm=phase8_policy_arm,
            planned_row_id=phase8_planned_row_id,
            attempt_index=phase8_attempt_index,
        )
    )
    if route_env:
        child_env.update(route_env)
    _run(f"gauge {side}/{scenario}", argv, PAYLOAD, step_timeout, env=child_env)


def compare(scenario: str) -> None:
    _run(f"compare {scenario}", [PY, "ai/hillclimb/run_gauge_live.py", "compare", "--scenario", scenario], PAYLOAD, 120)


def _dry_run_plan(scenario, side, seeds, solve_timeout, policy_mode, provider=None, model=None,
                  null_model=False,
                  snapshot=DEFAULT_SNAPSHOT,
                  retained_callback_config=DEFAULT_RETAINED_CALLBACK_CONFIG,
                  foothold: FootholdSpec | None = None,
                  ludus_range_id: str | None = None,
                  ludus_mcp_server: str | None = None):
    foothold = foothold or FootholdSpec()
    ludus_range_id = ludus_range_id or foothold.ludus_range_id
    ludus_mcp_server = ludus_mcp_server or foothold.ludus_mcp_server
    step_timeout = max(3600, solve_timeout + _GAUGE_STEP_OVERHEAD_S)
    print("DRY RUN (no --go). Plan — each gauge run gets its OWN fresh range:\n")
    print(f"  solve-timeout={solve_timeout}s per solve; per-run subprocess cap={step_timeout}s\n")
    for s in range(seeds):
        for sd in ([side] if side else ["harness", "bare"]):
            print(f"--- iteration seed={s} side={sd} ---")
            for name, argv, cwd, _ in RESET_STEPS:
                if name == "ludus rollback" and snapshot:
                    argv = _ludus_argv(
                        "rollback",
                        snapshot,
                        "--yes",
                        range_id=ludus_range_id,
                        mcp_server=ludus_mcp_server,
                    )
                if name == "ludus poweron":
                    argv = _ludus_argv(
                        "poweron",
                        "all",
                        range_id=ludus_range_id,
                        mcp_server=ludus_mcp_server,
                    )
                if name == "sync range time":
                    print(
                        f"  (cd {ROOT}) {' '.join(_ludus_argv('status', range_id=ludus_range_id, mcp_server=ludus_mcp_server))}     "
                        "# poll until every range guest reports an IP"
                    )
                    print(
                        f"  (cd {ROOT}) {' '.join(_sync_range_time_argv('check', range_id=ludus_range_id, mcp_server=ludus_mcp_server))}     "
                        "# poll until WinRM clock probes authenticate"
                    )
                    argv = _sync_range_time_argv(
                        "sync",
                        "--yes",
                        range_id=ludus_range_id,
                        mcp_server=ludus_mcp_server,
                    )
                if name == "bootstrap foothold":
                    argv = [
                        PY,
                        "skills/sage-callback-bootstrap/scripts/bootstrap_payloads.py",
                        "bootstrap-reset",
                        "--use-retained-callback",
                        "--retained-callback-config",
                        str(retained_callback_config),
                    ]
                print(f"  (cd {cwd}) {' '.join(argv)}")
            print(
                f"  (cd {ROOT}) {' '.join(_ludus_argv('status', range_id=ludus_range_id, mcp_server=ludus_mcp_server))}     "
                f"# poll until {foothold.host} is up"
            )
            print(
                f"  (cd {ROOT}) {' '.join(foothold.launch_argv())} "
                f"# log off localuser, RDP {foothold.user}, launch staged Apollo "
                f"(password source={foothold.password_env})"
            )
            print(
                f"  (cd {ROOT}) {' '.join(foothold.readiness_argv())}        "
                "# poll until Sage chat and Apollo are ready"
            )
            print(f"  (cd {ROOT}) {' '.join(CALLBACKS)}        # parse apollo_cb")
            print(f"  (cd {PAYLOAD}) {PY} ai/hillclimb/run_gauge_live.py run --go --side {sd} "
                  f"--scenario {scenario} --apollo-cb <apollo> --solve-timeout {solve_timeout} "
                  f"--policy-mode {policy_mode}"
                  f"{f' --provider {provider}' if provider else ''}"
                  f"{f' --model {model}' if model else ''}"
                  f"{' --null-model' if null_model else ''}")
    print(f"  (cd {PAYLOAD}) {PY} ai/hillclimb/run_gauge_live.py compare --scenario {scenario}")
    print("\nRe-run with --go to execute. ABORTS on any reset/bootstrap/callback failure.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bare-vs-harness gauge orchestrator (resets the lab per run).")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--side", choices=["harness", "bare"], default=None,
                    help="run only this side; omit to sweep harness+bare then compare")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--solve-timeout", type=int, default=1800,
                    help="seconds per gauge solve (default 1800=30min). The per-run subprocess cap is raised "
                         "to cover this + post-solve scoring overhead, so long solves are not killed early.")
    ap.add_argument("--go", action="store_true", help="actually reset the lab + run offensive tooling")
    ap.add_argument("--policy-mode", choices=["llm", "hybrid", "symbolic"], default="llm",
                    help="semantic capability policy for the Sage harness (default: llm)")
    ap.add_argument("--provider", default=None,
                    help="explicit harness model provider for a controlled model treatment")
    ap.add_argument("--model", default=None,
                    help="explicit harness model ID for a controlled model treatment")
    ap.add_argument("--null-model", action="store_true",
                    help="disable the headless harness policy model for one selected policy")
    ap.add_argument("--null-model-factorial", action="store_true",
                    help="run clean-reset null-model harness treatments for symbolic, llm, and hybrid")
    ap.add_argument("--treatment", choices=["sonnet", "haiku"], default=None,
                    help="load a named LiteLLM-backed treatment from --route-env")
    ap.add_argument("--route-env", type=Path, default=DEFAULT_ROUTE_ENV,
                    help=f"gitignored evaluation route file (default: {DEFAULT_ROUTE_ENV})")
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT,
                    help=f"Ludus staged-Apollo restore target (default: {DEFAULT_SNAPSHOT})")
    ap.add_argument(
        "--ludus-range-id",
        default=DEFAULT_LUDUS_RANGE_ID,
        help="Ludus range ID override for reset, inventory, and foothold deployment.",
    )
    ap.add_argument(
        "--ludus-mcp-server",
        default=DEFAULT_LUDUS_MCP_SERVER,
        help="Ludus MCP server/profile override for reset, inventory, and foothold deployment.",
    )
    ap.add_argument(
        "--engagement-netbios-map",
        default=None,
        help=(
            "JSON NetBIOS-to-FQDN map passed to Sage. "
            "Defaults to the GOAD map or the selected purpose-range map based on --scenario."
        ),
    )
    ap.add_argument(
        "--laps-forced-path",
        choices=_laps_family_transfer_forced_path_names(),
        default=None,
        help="Phase 6 only: force the exact first LAPS chain target as a label-only intervention.",
    )
    ap.add_argument(
        "--phase6-planned-row-id",
        default=None,
        help="Phase 6 only: stable preregistered row identifier used for append-only attempt accounting.",
    )
    ap.add_argument(
        "--phase6-attempt-index",
        type=int,
        default=None,
        help="Phase 6 only: 1-based attempt index for --phase6-planned-row-id.",
    )
    ap.add_argument(
        "--phase7-control",
        choices=["positive"],
        default=None,
        help="Phase 7 only: label a countable live positive trust/context row.",
    )
    ap.add_argument(
        "--phase7-attempt-index",
        type=int,
        default=None,
        help="Phase 7 only: required 1-based repetition index for the live positive row.",
    )
    ap.add_argument(
        "--phase8-policy-arm",
        choices=["symbolic", "hybrid", "llm"],
        default=None,
        help="Phase 8 only: label the frozen GOAD regression policy arm for this append-only attempt.",
    )
    ap.add_argument(
        "--phase8-planned-row-id",
        default=None,
        help="Phase 8 only: stable preregistered GOAD row identifier.",
    )
    ap.add_argument(
        "--phase8-attempt-index",
        type=int,
        default=None,
        help="Phase 8 only: 1-based append-only attempt index for --phase8-planned-row-id.",
    )
    ap.add_argument(
        "--retained-callback-config",
        type=Path,
        default=DEFAULT_RETAINED_CALLBACK_CONFIG,
        help="Apollo callback export imported after Mythic reset",
    )
    ap.add_argument("--foothold-host", default=DEFAULT_FOOTHOLD_HOST,
                    help=f"Ludus inventory host for the staged foothold (default: {DEFAULT_FOOTHOLD_HOST})")
    ap.add_argument("--foothold-ip", default=DEFAULT_FOOTHOLD_IP,
                    help=f"staged foothold host IP (default: {DEFAULT_FOOTHOLD_IP})")
    ap.add_argument("--foothold-user", default=DEFAULT_FOOTHOLD_USER,
                    help=f"interactive run-as identity for the foothold (default: {DEFAULT_FOOTHOLD_USER})")
    ap.add_argument("--foothold-callback-host", default=None,
                    help=(
                        "Mythic callback host value used for readiness/discovery "
                        "(default: same as --foothold-host)"
                    ))
    ap.add_argument("--foothold-callback-user", default=DEFAULT_FOOTHOLD_CALLBACK_USER,
                    help=(
                        "callback user value used to match the retained Apollo check-in "
                        f"(default: {DEFAULT_FOOTHOLD_CALLBACK_USER})"
                    ))
    ap.add_argument("--foothold-password-env", default=DEFAULT_FOOTHOLD_PASSWORD_ENV,
                    help=(
                        "environment/durable-env key containing the foothold run-as password "
                        f"(default: {DEFAULT_FOOTHOLD_PASSWORD_ENV})"
                    ))
    ap.add_argument("--controller", action="store_true",
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    foothold = FootholdSpec(
        host=args.foothold_host,
        ip=args.foothold_ip,
        user=args.foothold_user,
        callback_host=args.foothold_callback_host,
        callback_user=args.foothold_callback_user,
        password_env=args.foothold_password_env,
        ludus_range_id=args.ludus_range_id,
        ludus_mcp_server=args.ludus_mcp_server,
    )
    foothold = _configure_foothold_for_scenario(args.scenario, foothold)
    _phase7_trust_context_eval_env(
        args.scenario,
        control=args.phase7_control,
        attempt_index=args.phase7_attempt_index,
    )
    policy_mode = "symbolic" if args.controller else args.policy_mode
    _phase8_goad_regression_eval_env(
        args.scenario,
        policy_mode=policy_mode,
        policy_arm=args.phase8_policy_arm,
        planned_row_id=args.phase8_planned_row_id,
        attempt_index=args.phase8_attempt_index,
    )
    if args.null_model_factorial and args.side not in (None, "harness"):
        ap.error("--null-model-factorial only supports the harness side")
    if args.null_model_factorial and args.treatment:
        ap.error("--null-model-factorial cannot be combined with a named model treatment")
    policy_modes = ["symbolic", "llm", "hybrid"] if args.null_model_factorial else [policy_mode]
    null_model = bool(args.null_model or args.null_model_factorial)
    route_env = None
    if args.treatment:
        route = load_treatment_route(args.route_env, args.treatment)
        args.provider = route["provider"]
        args.model = route["model"]
        route_env = {
            "SAGE_EVAL_API_ENDPOINT": route["api_endpoint"],
            "SAGE_EVAL_API_KEY": route["api_key"],
        }

    if not args.go:
        for selected_mode in policy_modes:
            _dry_run_plan(args.scenario, "harness" if args.null_model_factorial else args.side,
                          args.seeds, args.solve_timeout, selected_mode,
                          args.provider, args.model, null_model,
                          args.snapshot, args.retained_callback_config, foothold,
                          args.ludus_range_id, args.ludus_mcp_server)
            print(f"\n  NOTE: Sage policy mode -> {selected_mode}; null_model={null_model}.")
        return 0

    validate_reset_inputs(
        args.snapshot,
        args.retained_callback_config,
        foothold,
        args.ludus_range_id,
        args.ludus_mcp_server,
    )
    for selected_mode in policy_modes:
        for _ in range(args.seeds):
            sides = ["harness"] if args.null_model_factorial else (
                [args.side] if args.side else ["harness", "bare"]
            )
            for side in sides:
                run_side(args.scenario, side, go=True, solve_timeout=args.solve_timeout,
                         policy_mode=selected_mode, provider=args.provider, model=args.model,
                         null_model=null_model, route_env=route_env,
                         snapshot=args.snapshot,
                         retained_callback_config=args.retained_callback_config,
                         foothold=foothold,
                         ludus_range_id=args.ludus_range_id,
                         ludus_mcp_server=args.ludus_mcp_server,
                         engagement_netbios_map=args.engagement_netbios_map,
                         laps_forced_path=args.laps_forced_path,
                         phase6_planned_row_id=args.phase6_planned_row_id,
                         phase6_attempt_index=args.phase6_attempt_index,
                         phase7_control=args.phase7_control,
                         phase7_attempt_index=args.phase7_attempt_index,
                         phase8_policy_arm=args.phase8_policy_arm,
                         phase8_planned_row_id=args.phase8_planned_row_id,
                         phase8_attempt_index=args.phase8_attempt_index)
    if not args.side and not args.null_model_factorial:
        compare(args.scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
