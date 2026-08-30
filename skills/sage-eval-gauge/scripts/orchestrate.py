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
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]            # repository root
PAYLOAD = ROOT / "Payload_Type" / "sage"
PY = str(ROOT / ".venv" / "bin" / "python")
BH = str(os.environ.get("SAGE_BLOODHOUND_MCP_DIR") or "").strip()
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


def _gate1p_stagea_protocol():
    sys.path.insert(0, str(PAYLOAD))
    from ai.hillclimb import gate1p_stagea  # type: ignore

    return gate1p_stagea


def _gate1p_protocol_selector():
    sys.path.insert(0, str(PAYLOAD))
    from ai.hillclimb import gate1p_protocol  # type: ignore

    return gate1p_protocol


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
    ("wipe bloodhound",  ["uv", "--directory", BH, "run", "python",
                          str(ROOT / "skills/sage-goad-reset/scripts/bh_reset.py"), "wipe", "--yes"], ROOT, 180),
    ("restart sage",     ["/bin/bash", "skills/sage-goad-reset/scripts/sage_restart.sh",
                          "SAGE_ENGAGEMENT_GATE=1", f"SAGE_BLOODHOUND_MCP_DIR={BH}"], ROOT, 240),
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


def _poll(
    name,
    argv,
    cwd,
    predicate,
    *,
    timeout,
    interval=20,
    env: dict[str, str] | None = None,
    predicate_returncodes: tuple[int, ...] = (0,),
):
    print(f"\n=== poll: {name} (<= {timeout}s) ===", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=env)
        if proc.returncode not in predicate_returncodes:
            detail = (proc.stderr or proc.stdout or "no diagnostic output").strip()
            raise SystemExit(
                f"ABORT: poll '{name}' command failed (exit {proc.returncode}): {detail}"
            )
        if predicate(proc.stdout):
            return proc.stdout
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


def _bloodhound_is_empty(out: str) -> bool:
    match = re.search(r"\bcount=(\d+)\b", str(out or ""))
    return bool(match and int(match.group(1)) == 0)


def discover_callbacks(foothold: FootholdSpec | None = None) -> int:
    foothold = foothold or FootholdSpec()
    proc = subprocess.run(CALLBACKS, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(
            f"ABORT: callback inventory failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    matches = list(re.finditer(
        r"id=(?P<id>\d+)\s+payloadtype=apollo\s+host=(?P<host>\S+)\s+user=(?P<user>\S+)",
        proc.stdout,
    ))
    exact = [
        match
        for match in matches
        if match.group("host").casefold() == foothold.callback_host.casefold()
        and match.group("user").casefold() in {
            foothold.callback_user.casefold(),
            foothold.user.casefold(),
        }
    ]
    if len(matches) != 1 or len(exact) != 1:
        raise SystemExit(
            f"ABORT: expected the sole active Apollo callback to exactly match {foothold.label()}, "
            f"found apollo={len(matches)} exact={len(exact)}.\n{proc.stdout}"
        )
    return int(exact[0].group("id"))


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
                predicate_returncodes=(0, 1),
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
        if name == "wipe bloodhound":
            _poll(
                "BloodHound authoritative domain count is zero",
                [
                    "uv", "--directory", BH, "run", "python",
                    str(ROOT / "skills/sage-goad-reset/scripts/bh_reset.py"), "status",
                ],
                ROOT,
                _bloodhound_is_empty,
                timeout=120,
                interval=5,
                env=range_env,
            )
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
        predicate_returncodes=(0, 1),
    )
    callback_id = discover_callbacks(foothold)
    return None, callback_id


# Seconds the per-run gauge SUBPROCESS may take ON TOP OF the solve itself — covers reset-independent
# post-solve work (DA settle polling up to ~300s, milestone probes, ScoreCard recording). The step cap is
# solve_timeout + this, floored at the historical 3600 so the default behaviour is unchanged.
_GAUGE_STEP_OVERHEAD_S = 900


def _side_preflight_argv(
    scenario: str,
    *,
    provider: str | None,
    model: str | None,
    max_output_tokens: int | None,
) -> list[str]:
    argv = [PY, "ai/hillclimb/run_gauge_live.py", "preflight", "--scenario", scenario]
    if provider:
        argv.extend(["--provider", provider])
    if model:
        argv.extend(["--model", model])
    if max_output_tokens is not None:
        argv.extend(["--max-output-tokens", str(max_output_tokens)])
    return argv


def _run_side_preflight(
    scenario: str,
    *,
    provider: str | None,
    model: str | None,
    max_output_tokens: int | None,
    route_env: dict[str, str] | None,
) -> None:
    _run(
        f"preflight {scenario}",
        _side_preflight_argv(
            scenario,
            provider=provider,
            model=model,
            max_output_tokens=max_output_tokens,
        ),
        PAYLOAD,
        120,
        env={**os.environ, **(route_env or {})},
    )


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
             phase8_attempt_index: int | None = None,
             measurement_kind: str = "legacy",
             protocol_id: str | None = None,
             pair_id: str | None = None,
             arm_index: int | None = None,
             model_token_budget: int | None = None,
             model_context_limit: int | None = None,
             max_output_tokens: int | None = None,
             source_version: str | None = None,
             live_authority_id: str | None = None,
             skip_preflight: bool = False,
             before_reset: Callable[[], None] | None = None) -> None:
    foothold = _configure_foothold_for_scenario(scenario, foothold)
    if measurement_kind == "row" and pair_id:
        gate1p = _gate1p_stagea_protocol()
        inherited = {**os.environ, **(route_env or {})}
        incompatible = gate1p.active_incompatible_evaluation_overrides(inherited)
        if incompatible:
            raise SystemExit(
                "ABORT: Gate 1P row inherited incompatible evaluation override(s): "
                + ", ".join(incompatible)
            )
    # Fail in seconds, not after a ~60-min range run: assert the scenario objective is completion-recognizable
    # BEFORE spending a reset + live solve. Guards the harness->Sage objective seam that shipped opaque once
    # (the gauge's read-only/seam-injected design makes that seam invisible to offline unit tests). Aborts
    # (via _run's non-zero SystemExit) on a dropped/opaque/unparseable objective.
    if not skip_preflight:
        _run_side_preflight(
            scenario,
            provider=provider,
            model=model,
            max_output_tokens=max_output_tokens,
            route_env=route_env,
        )
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
    if measurement_kind == "row":
        restart_env.update({
            "SAGE_EVAL_MODEL_TOKEN_BUDGET": str(model_token_budget or ""),
            "SAGE_EVAL_MODEL_CONTEXT_LIMIT": str(model_context_limit or ""),
            "SAGE_EVAL_MAX_OUTPUT_TOKENS": str(max_output_tokens or ""),
        })
    reset_kwargs = {
        "restart_env": restart_env,
        "snapshot": snapshot,
        "retained_callback_config": retained_callback_config,
        "foothold": foothold,
        "ludus_range_id": ludus_range_id,
    }
    if ludus_mcp_server is not None:
        reset_kwargs["ludus_mcp_server"] = ludus_mcp_server
    if before_reset is not None:
        before_reset()
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
    if measurement_kind != "legacy":
        argv.extend(["--measurement-kind", measurement_kind])
    if protocol_id:
        argv.extend(["--protocol-id", protocol_id])
    if pair_id:
        argv.extend(["--pair-id", pair_id])
    if arm_index is not None:
        argv.extend(["--arm-index", str(arm_index)])
    if model_token_budget is not None:
        argv.extend(["--model-token-budget", str(model_token_budget)])
    if model_context_limit is not None:
        argv.extend(["--model-context-limit", str(model_context_limit)])
    if max_output_tokens is not None:
        argv.extend(["--max-output-tokens", str(max_output_tokens)])
    if source_version:
        argv.extend(["--source-version", source_version])
    if live_authority_id:
        argv.extend(["--live-authority-id", live_authority_id])
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
    if measurement_kind == "row" and pair_id:
        child_env["SAGE_EVAL_RESET_READINESS_JSON"] = json.dumps({
            "ready": True,
            "snapshot": snapshot,
            "range_id": ludus_range_id,
            "bloodhound_domain_count": 0,
            "clock_gate": "passed",
            "runtime_databases_archived": True,
            "mythic_reset": "passed",
            "initial_callback_display_id": int(apollo_cb),
            "unique_initial_callback": True,
            "pre_arm_task_count": 0,
        }, sort_keys=True)
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
    if measurement_kind == "row" and pair_id:
        artifact_dir = _gate1p_artifact_dir(route_env)
        child_env["SAGE_EVAL_ARTIFACT_DIR"] = str(artifact_dir)
        child_env["SAGE_EVAL_RESULTS_PATH"] = str(_gate1p_results_path(route_env))
        child_env["SAGE_EVAL_PAIR_CONTROL_PATH"] = str(_gate1p_pair_control_path(route_env))
    _run(f"gauge {side}/{scenario}", argv, PAYLOAD, step_timeout, env=child_env)


def compare(
    scenario: str,
    *,
    pair_id: str | None = None,
    results_path: Path | None = None,
) -> None:
    argv = [PY, "ai/hillclimb/run_gauge_live.py", "compare", "--scenario", scenario]
    if pair_id:
        argv.extend(["--pair-id", pair_id])
    child_env = None
    if results_path is not None:
        child_env = os.environ.copy()
        child_env["SAGE_EVAL_RESULTS_PATH"] = str(results_path)
    _run(f"compare {scenario}", argv, PAYLOAD, 120, env=child_env)


def _gate1p_artifact_dir(route_env: dict[str, str] | None = None) -> Path:
    effective_env = {**os.environ, **(route_env or {})}
    override = str(effective_env.get("SAGE_EVAL_ARTIFACT_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return (
        ROOT / ".sage_history" / "evaluation" / "harness-self-improvement"
        / "gate1p-stagea" / _gate1p_stagea_protocol().PAIR_ID
    )


def _gate1p_results_path(route_env: dict[str, str] | None = None) -> Path:
    effective_env = {**os.environ, **(route_env or {})}
    override = str(effective_env.get("SAGE_EVAL_RESULTS_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    return _gate1p_artifact_dir(route_env) / "results.jsonl"


def _gate1p_pair_control_path(route_env: dict[str, str] | None = None) -> Path:
    return _gate1p_artifact_dir(route_env) / "pair-control.json"


def _gate1p_prestart_cleanup_path(route_env: dict[str, str] | None = None) -> Path:
    return _gate1p_artifact_dir(route_env) / "prestart-cleanup.jsonl"


def validate_gate1p_storage(route_env: dict[str, str] | None = None) -> None:
    history_root = (ROOT / ".sage_history").resolve()
    artifact_dir = _gate1p_artifact_dir(route_env).resolve()
    results_path = _gate1p_results_path(route_env).resolve()
    prestart_cleanup_path = _gate1p_prestart_cleanup_path(route_env).resolve()
    try:
        artifact_dir.relative_to(history_root)
        results_path.relative_to(history_root)
        prestart_cleanup_path.relative_to(history_root)
    except ValueError as exc:
        raise SystemExit("ABORT: Gate 1P artifacts and results must stay under private .sage_history") from exc
    artifact_dir.mkdir(parents=True, exist_ok=True)
    probe = artifact_dir / f".write-probe-{os.getpid()}"
    try:
        probe.write_text("write-probe\n", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)


def _read_gate1p_jsonl(path: Path, *, label: str) -> list[dict]:
    if not path.exists():
        return []
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        raise SystemExit(f"ABORT: {label} is unreadable: {exc}") from exc
    if any(not isinstance(record, dict) for record in records):
        raise SystemExit(f"ABORT: {label} contains a malformed record")
    return records


_GATE1P_PAIR_CONTROL_FIELDS = {
    "schema", "protocol_id", "pair_id", "source_version", "next_arm",
    "completed_arms", "prearm_state", "created_at", "updated_at",
}
_GATE1P_PREARM_FIELDS = {"arm", "status", "reset_generation", "updated_at"}


def _finite_json_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _require_gate1p_expected_source(source_version: object) -> str:
    """Return the independently contracted source identity; never consult mutable control state."""
    if (
        type(source_version) is not str
        or not source_version
        or source_version != source_version.strip()
        or source_version == "unrecorded"
    ):
        raise SystemExit("ABORT: an exact independently expected Gate 1P source_version is required")
    return source_version


def _validate_gate1p_pair_control_state(
    state: object,
    *,
    expected_source_version: object,
    allow_complete: bool = False,
) -> dict:
    protocol = _gate1p_stagea_protocol()
    expected_source = _require_gate1p_expected_source(expected_source_version)
    if not isinstance(state, dict) or set(state) != _GATE1P_PAIR_CONTROL_FIELDS:
        raise SystemExit("ABORT: Gate 1P pair control has a non-canonical top-level schema")
    if (
        state.get("schema") != "gate1p-stagea-pair-control-v3"
        or state.get("protocol_id") != protocol.PROTOCOL_ID
        or state.get("pair_id") != protocol.PAIR_ID
        or type(expected_source) is not str
        or not expected_source
        or state.get("source_version") != expected_source
        or not _finite_json_number(state.get("created_at"))
        or not _finite_json_number(state.get("updated_at"))
        or float(state["updated_at"]) < float(state["created_at"])
        or type(state.get("completed_arms")) is not list
    ):
        raise SystemExit("ABORT: Gate 1P pair control identity or metadata is malformed")
    next_arm = state.get("next_arm")
    completed = state.get("completed_arms")
    prearm = state.get("prearm_state")
    legal_active = {
        "shipping_sage": [],
        "true_naive": ["shipping_sage"],
    }
    if next_arm in legal_active:
        reset_generation = prearm.get("reset_generation") if isinstance(prearm, dict) else None
        if (
            completed != legal_active[next_arm]
            or not isinstance(prearm, dict)
            or set(prearm) != _GATE1P_PREARM_FIELDS
            or prearm.get("arm") != next_arm
            or prearm.get("status") not in {"idle", "reset_started", "cleanup_complete"}
            or type(reset_generation) is not int
            or reset_generation < 0
            or (prearm.get("status") == "idle") != (reset_generation == 0)
            or not _finite_json_number(prearm.get("updated_at"))
            or float(prearm["updated_at"]) < float(state["created_at"])
            or float(prearm["updated_at"]) > float(state["updated_at"])
        ):
            raise SystemExit("ABORT: Gate 1P active pair-control state is malformed or illegal")
    elif next_arm == "complete" and allow_complete:
        if completed != list(protocol.ARMS) or prearm is not None:
            raise SystemExit("ABORT: Gate 1P complete pair-control state is malformed")
    else:
        raise SystemExit("ABORT: Gate 1P pair control is consumed or out of order")
    return state


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_gate1p_json(path: Path, payload: dict, *, create_only: bool = False) -> None:
    """Publish one complete canonical JSON object without truncating the prior control state."""
    data = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            os.link(tmp, path)
            tmp.unlink()
        else:
            os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _validate_gate1p_prestart_cleanup(
    records: list[dict],
    *,
    expected_arm: str,
    require_current_arm: bool,
    expected_reset_generation: int | None,
) -> None:
    """Admit one exact completed pre-arm cleanup per reached arm, outside the row ledger."""
    protocol = _gate1p_stagea_protocol()
    expected_steps = ["stop sage", "archive dbs", "verify no running tasks", "ludus poweroff"]
    expected_fields = {
        "schema", "record_type", "measurement_kind", "protocol", "result_retained",
        "episode_artifact_retained", "temporary_evaluator_material", "complete", "errors",
        "reset_generation", "steps", "ts",
    }
    seen_arms: list[str] = []
    timestamps: list[float] = []
    for record in records:
        identity = record.get("protocol")
        steps = record.get("steps")
        timestamp = record.get("ts")
        reset_generation = record.get("reset_generation")
        if set(record) != expected_fields:
            raise SystemExit("ABORT: prestart cleanup record has an undeclared shape")
        if (
            record.get("schema") != "gate1p-stagea-prestart-cleanup-v2"
            or record.get("record_type") != "prestart_cleanup"
            or record.get("measurement_kind") != "prestart_readiness"
            or not isinstance(identity, dict)
            or set(identity) != {"protocol_id", "pair_id", "arm"}
            or identity.get("protocol_id") != protocol.PROTOCOL_ID
            or identity.get("pair_id") != protocol.PAIR_ID
            or identity.get("arm") not in protocol.ARMS
            or record.get("result_retained") is not False
            or record.get("episode_artifact_retained") is not False
            or record.get("temporary_evaluator_material") != {
                "created": False,
                "removal_required": False,
                "disposition": "not_applicable",
            }
            or record.get("complete") is not True
            or record.get("errors") != []
            or type(reset_generation) is not int
            or reset_generation <= 0
            or not isinstance(steps, list)
            or [step.get("name") for step in steps if isinstance(step, dict)] != expected_steps
            or any(
                not isinstance(step, dict)
                or set(step) != {"name", "ok"}
                or step.get("ok") is not True
                for step in steps
            )
            or not _finite_json_number(timestamp)
        ):
            raise SystemExit("ABORT: prestart cleanup record is incomplete or mismatched")
        seen_arms.append(str(identity["arm"]))
        timestamps.append(float(timestamp))
    if len(seen_arms) != len(set(seen_arms)):
        raise SystemExit("ABORT: retry-zero prestart cleanup already consumed an arm recovery")
    arm_indices = [protocol.ARMS.index(arm) for arm in seen_arms]
    if arm_indices != sorted(arm_indices):
        raise SystemExit("ABORT: prestart cleanup arm order is invalid")
    if any(index > protocol.ARMS.index(expected_arm) for index in arm_indices):
        raise SystemExit("ABORT: prestart cleanup is ahead of pair control")
    current_present = expected_arm in seen_arms
    if current_present is not require_current_arm:
        raise SystemExit("ABORT: prestart cleanup does not match the current pre-arm state")
    if require_current_arm:
        current = next(record for record in records if record["protocol"]["arm"] == expected_arm)
        if current["reset_generation"] != expected_reset_generation:
            raise SystemExit("ABORT: prestart cleanup reset generation is stale or mismatched")
    if any(left >= right for left, right in zip(timestamps, timestamps[1:])):
        raise SystemExit("ABORT: prestart cleanup chronology is invalid")


def initialize_gate1p_pair_control(*, source_version: str, route_env: dict[str, str] | None = None) -> Path:
    """Create or exactly resume the one-shot lifecycle state before any reset begins."""
    expected_source = _require_gate1p_expected_source(source_version)
    protocol = _gate1p_stagea_protocol()
    path = _gate1p_pair_control_path(route_env)
    results = _gate1p_results_path(route_env)
    prior = _read_gate1p_jsonl(results, label="existing Gate 1P result ledger")
    prestart = _read_gate1p_jsonl(
        _gate1p_prestart_cleanup_path(route_env),
        label="Gate 1P prestart cleanup ledger",
    )
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"ABORT: existing Gate 1P pair control is unreadable: {exc}") from exc
        _validate_gate1p_pair_control_state(existing, expected_source_version=expected_source)
        next_arm = existing["next_arm"]
        completed_arms = existing["completed_arms"]
        prearm_state = existing["prearm_state"]
        if next_arm == "shipping_sage" and completed_arms == []:
            if prior:
                raise SystemExit("ABORT: shipping resume requires an empty countable result ledger")
        elif next_arm == "true_naive" and completed_arms == ["shipping_sage"]:
            if any(
                (record.get("protocol") or {}).get("arm") == "true_naive"
                for record in prior
            ):
                raise SystemExit("ABORT: retry-zero lifecycle already consumed the naive arm")
            shipping = protocol.completed_arm_lifecycle_disposition(
                prior,
                arm="shipping_sage",
                history_root=(ROOT / ".sage_history").resolve(),
            )
            if shipping.get("countable") is not True:
                raise SystemExit(
                    f"ABORT: naive resume blocked by shipping admission: {shipping.get('reason')}"
                )
        else:
            raise SystemExit("ABORT: Gate 1P pair control is consumed or out of order")
        _validate_gate1p_prestart_cleanup(
            prestart,
            expected_arm=str(next_arm),
            require_current_arm=prearm_state["status"] != "idle",
            expected_reset_generation=prearm_state["reset_generation"],
        )
        if prearm_state["status"] == "reset_started":
            existing["prearm_state"] = {
                "arm": next_arm,
                "status": "cleanup_complete",
                "reset_generation": prearm_state["reset_generation"],
                "updated_at": time.time(),
            }
            existing["updated_at"] = time.time()
            _validate_gate1p_pair_control_state(existing, expected_source_version=expected_source)
            _atomic_write_gate1p_json(path, existing)
        return path
    if prior or prestart:
        raise SystemExit("ABORT: Gate 1P lifecycle evidence exists without pair control")
    created_at = time.time()
    state = {
        "schema": "gate1p-stagea-pair-control-v3",
        "protocol_id": protocol.PROTOCOL_ID,
        "pair_id": protocol.PAIR_ID,
        "source_version": expected_source,
        "next_arm": "shipping_sage",
        "completed_arms": [],
        "prearm_state": {
            "arm": "shipping_sage",
            "status": "idle",
            "reset_generation": 0,
            "updated_at": created_at,
        },
        "created_at": created_at,
        "updated_at": created_at,
    }
    _validate_gate1p_pair_control_state(state, expected_source_version=expected_source)
    try:
        _atomic_write_gate1p_json(path, state, create_only=True)
    except FileExistsError as exc:
        raise SystemExit("ABORT: Gate 1P pair control was concurrently created") from exc
    return path


def mark_gate1p_prearm_reset_started(
    *,
    arm: str,
    source_version: str,
    route_env: dict[str, str] | None = None,
) -> None:
    """Persist reset mutation intent immediately before the reset/readiness controller runs."""
    path = _gate1p_pair_control_path(route_env)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"ABORT: Gate 1P pair control is unreadable: {exc}") from exc
    expected_source = _require_gate1p_expected_source(source_version)
    _validate_gate1p_pair_control_state(state, expected_source_version=expected_source)
    prearm_state = state["prearm_state"]
    if (
        state.get("next_arm") != arm
        or prearm_state.get("arm") != arm
        or prearm_state.get("status") not in {"idle", "cleanup_complete"}
    ):
        raise SystemExit("ABORT: Gate 1P reset start is out of order")
    state["prearm_state"] = {
        "arm": arm,
        "status": "reset_started",
        "reset_generation": prearm_state["reset_generation"] + 1,
        "updated_at": time.time(),
    }
    state["updated_at"] = time.time()
    _validate_gate1p_pair_control_state(state, expected_source_version=expected_source)
    _atomic_write_gate1p_json(path, state)


def mark_gate1p_prestart_cleanup_complete(
    *,
    arm: str,
    source_version: str,
    route_env: dict[str, str] | None = None,
) -> int:
    """Bind a complete pre-arm cleanup to the exact reset-started arm."""
    path = _gate1p_pair_control_path(route_env)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"ABORT: Gate 1P pair control is unreadable: {exc}") from exc
    expected_source = _require_gate1p_expected_source(source_version)
    _validate_gate1p_pair_control_state(state, expected_source_version=expected_source)
    prearm_state = state["prearm_state"]
    if (
        state.get("next_arm") != arm
        or prearm_state.get("arm") != arm
        or prearm_state.get("status") != "reset_started"
    ):
        raise SystemExit("ABORT: Gate 1P prestart cleanup completion is out of order")
    reset_generation = prearm_state["reset_generation"]
    state["prearm_state"] = {
        "arm": arm,
        "status": "cleanup_complete",
        "reset_generation": reset_generation,
        "updated_at": time.time(),
    }
    state["updated_at"] = time.time()
    _validate_gate1p_pair_control_state(state, expected_source_version=expected_source)
    _atomic_write_gate1p_json(path, state)
    return reset_generation


def advance_gate1p_pair_control(
    *,
    arm: str,
    source_version: str,
    route_env: dict[str, str] | None = None,
) -> None:
    """Advance only after the arm returned and its guaranteed cleanup completed."""
    path = _gate1p_pair_control_path(route_env)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"ABORT: Gate 1P pair control is unreadable: {exc}") from exc
    expected_source = _require_gate1p_expected_source(source_version)
    _validate_gate1p_pair_control_state(state, expected_source_version=expected_source)
    expected_completed = [] if arm == "shipping_sage" else ["shipping_sage"]
    prearm_state = state["prearm_state"]
    if (
        state.get("next_arm") != arm
        or state.get("completed_arms") != expected_completed
        or prearm_state.get("arm") != arm
        or prearm_state.get("status") != "reset_started"
    ):
        raise SystemExit("ABORT: Gate 1P pair control transition is out of order")
    state["completed_arms"] = [*expected_completed, arm]
    state["next_arm"] = "true_naive" if arm == "shipping_sage" else "complete"
    state["prearm_state"] = (
        {
            "arm": "true_naive",
            "status": "idle",
            "reset_generation": 0,
            "updated_at": time.time(),
        }
        if arm == "shipping_sage"
        else None
    )
    state["updated_at"] = time.time()
    _validate_gate1p_pair_control_state(
        state,
        expected_source_version=expected_source,
        allow_complete=True,
    )
    _atomic_write_gate1p_json(path, state)


def _gate1p_result_retained(*, arm: str, pair_id: str, route_env: dict[str, str] | None = None) -> bool:
    path = _gate1p_results_path(route_env)
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        protocol = record.get("protocol") if isinstance(record.get("protocol"), dict) else {}
        if (
            record.get("record_type") == "result"
            and protocol.get("pair_id") == pair_id
            and protocol.get("arm") == arm
        ):
            return True
    return False


def _gate1p_arm_started(*, arm: str, pair_id: str, route_env: dict[str, str] | None = None) -> bool:
    path = _gate1p_results_path(route_env)
    if not path.is_file():
        return False
    try:
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception as exc:
        raise SystemExit(f"ABORT: Gate 1P row ledger is unreadable during cleanup: {exc}") from exc
    return any(
        isinstance(record, dict)
        and record.get("record_type") == "arm_started"
        and (record.get("protocol") or {}).get("pair_id") == pair_id
        and (record.get("protocol") or {}).get("arm") == arm
        for record in records
    )


def cleanup_gate1p_arm(
    *,
    arm: str,
    pair_id: str,
    ludus_range_id: str | None,
    ludus_mcp_server: str | None,
    route_env: dict[str, str] | None = None,
    row_started: bool = True,
    source_version: str | None = None,
) -> dict:
    """Always execute the declared post-arm cleanup and retain a typed disposition."""
    range_env = _range_env(ludus_range_id, ludus_mcp_server)
    steps: list[dict[str, object]] = []
    errors: list[str] = []
    cleanup_steps = [
        ("stop sage", ["/bin/bash", "skills/sage-goad-reset/scripts/sage_stop.sh"], ROOT, 120),
        ("archive dbs", [PY, "skills/sage-goad-reset/scripts/archive_runtime_dbs.py"], ROOT, 180),
        (
            "verify no running tasks",
            [PY, "ai/hillclimb/run_gauge_live.py", "verify-no-running-tasks"],
            PAYLOAD,
            120,
        ),
        (
            "ludus poweroff",
            _ludus_argv(
                "poweroff",
                "all",
                range_id=ludus_range_id,
                mcp_server=ludus_mcp_server,
            ),
            ROOT,
            900,
        ),
    ]
    for name, argv, cwd, timeout in cleanup_steps:
        try:
            _run(f"Gate 1P cleanup: {name}", argv, cwd, timeout, env=range_env)
        except (OSError, SystemExit, subprocess.TimeoutExpired) as exc:
            errors.append(f"{name}: {exc}")
            steps.append({"name": name, "ok": False, "error": str(exc)})
        else:
            steps.append({"name": name, "ok": True})
    result_retained = _gate1p_result_retained(arm=arm, pair_id=pair_id, route_env=route_env)
    episode_artifact = _gate1p_artifact_dir(route_env) / f"{arm}-episode.json"
    episode_artifact_retained = episode_artifact.is_file()
    if row_started and not result_retained:
        errors.append("required arm result record is absent")
    if row_started and not episode_artifact_retained:
        errors.append("required episode artifact is absent")
    prestart_reset_generation: int | None = None
    if not row_started and not errors:
        try:
            prestart_reset_generation = mark_gate1p_prestart_cleanup_complete(
                arm=arm,
                source_version=_require_gate1p_expected_source(source_version),
                route_env=route_env,
            )
        except SystemExit as exc:
            errors.append(f"pair control: {exc}")
    elif not row_started:
        try:
            control = json.loads(_gate1p_pair_control_path(route_env).read_text(encoding="utf-8"))
            _validate_gate1p_pair_control_state(
                control,
                expected_source_version=_require_gate1p_expected_source(source_version),
            )
            prearm = control["prearm_state"]
            if control["next_arm"] == arm and prearm["status"] == "reset_started":
                prestart_reset_generation = prearm["reset_generation"]
        except (OSError, TypeError, ValueError, SystemExit):
            prestart_reset_generation = None
    report = {
        "record_type": "cleanup",
        "measurement_kind": "row",
        "protocol": {"protocol_id": _gate1p_stagea_protocol().PROTOCOL_ID, "pair_id": pair_id, "arm": arm},
        "result_retained": result_retained,
        "episode_artifact": str(episode_artifact),
        "episode_artifact_retained": episode_artifact_retained,
        "temporary_evaluator_material": {
            "created": False,
            "removal_required": False,
            "disposition": "not_applicable",
        },
        "complete": not errors,
        "errors": errors,
        "steps": steps,
        "ts": time.time(),
    }
    if row_started:
        path = _gate1p_results_path(route_env)
    else:
        report = {
            "schema": "gate1p-stagea-prestart-cleanup-v2",
            "record_type": "prestart_cleanup",
            "measurement_kind": "prestart_readiness",
            "protocol": report["protocol"],
            "result_retained": result_retained,
            "episode_artifact_retained": episode_artifact_retained,
            "reset_generation": prestart_reset_generation,
            "temporary_evaluator_material": report["temporary_evaluator_material"],
            "complete": report["complete"],
            "errors": errors,
            "steps": steps,
            "ts": report["ts"],
        }
        path = _gate1p_prestart_cleanup_path(route_env)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, sort_keys=True) + "\n")
    if errors:
        raise SystemExit(f"ABORT: Gate 1P cleanup incomplete for {arm}: {'; '.join(errors)}")
    return report


def _validate_gate1p_wrapper_entry(
    *,
    arm: str,
    source_version: object,
    route_env: dict[str, str] | None,
) -> str:
    """Bind the CLI/contract source to mutable control before provider or reset spend."""
    expected_source = _require_gate1p_expected_source(source_version)
    try:
        entry_state = json.loads(
            _gate1p_pair_control_path(route_env).read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise SystemExit(f"ABORT: Gate 1P pair control is unreadable at wrapper entry: {exc}") from exc
    _validate_gate1p_pair_control_state(
        entry_state,
        expected_source_version=expected_source,
    )
    if entry_state.get("next_arm") != arm:
        raise SystemExit("ABORT: Gate 1P wrapper arm is out of order")
    return expected_source


def run_side_with_gate1p_cleanup(scenario: str, side: str, **kwargs) -> None:
    """Run one arm and guarantee cleanup after every started Gate 1P row, including failures."""
    is_pair = kwargs.get("measurement_kind") == "row" and kwargs.get("pair_id")
    protocol = _gate1p_stagea_protocol()
    arm_index = kwargs.get("arm_index")
    arm = protocol.ARMS[int(arm_index)] if arm_index in (0, 1) else f"invalid:{arm_index}"
    run_kwargs = dict(kwargs)
    reset_started = False
    if is_pair:
        expected_source = _validate_gate1p_wrapper_entry(
            arm=arm,
            source_version=kwargs.get("source_version"),
            route_env=kwargs.get("route_env"),
        )
        _run_side_preflight(
            scenario,
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
            max_output_tokens=kwargs.get("max_output_tokens"),
            route_env=kwargs.get("route_env"),
        )

        def before_reset() -> None:
            nonlocal reset_started
            mark_gate1p_prearm_reset_started(
                arm=arm,
                source_version=expected_source,
                route_env=kwargs.get("route_env"),
            )
            reset_started = True

        run_kwargs["skip_preflight"] = True
        run_kwargs["before_reset"] = before_reset
    arm_returned = False
    try:
        run_side(scenario, side, **run_kwargs)
        arm_returned = True
    finally:
        if is_pair and reset_started:
            try:
                row_started = _gate1p_arm_started(
                    arm=arm,
                    pair_id=str(kwargs["pair_id"]),
                    route_env=kwargs.get("route_env"),
                )
            except SystemExit:
                row_started = True
            cleanup_gate1p_arm(
                arm=arm,
                pair_id=str(kwargs["pair_id"]),
                ludus_range_id=kwargs.get("ludus_range_id"),
                ludus_mcp_server=kwargs.get("ludus_mcp_server"),
                route_env=kwargs.get("route_env"),
                row_started=row_started,
                source_version=expected_source,
            )
            if arm_returned:
                if not row_started:
                    raise SystemExit("ABORT: Gate 1P arm returned without an arm_started record")
                advance_gate1p_pair_control(
                    arm=arm,
                    source_version=expected_source,
                    route_env=kwargs.get("route_env"),
                )
            elif row_started:
                mark_gate1p_prestart_cleanup_complete(
                    arm=arm,
                    source_version=expected_source,
                    route_env=kwargs.get("route_env"),
                )


def _dry_run_plan(scenario, side, seeds, solve_timeout, policy_mode, provider=None, model=None,
                  null_model=False,
                  measurement_kind="legacy",
                  protocol_id=None,
                  pair_id=None,
                  model_token_budget=None,
                  model_context_limit=None,
                  max_output_tokens=None,
                  source_version=None,
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
    if protocol_id and measurement_kind == "row":
        selected = _gate1p_protocol_selector().protocol_for_request(
            protocol_id=protocol_id,
            scenario=scenario,
        )
        if not selected.pair_allocated:
            contract = selected.assembled_contract()
            print(json.dumps({"prospective_protocol": contract}, sort_keys=True))
            if contract["required_comparison_pairs"] > 1:
                print(json.dumps({
                    "prospective_matrix_schedule": [
                        {
                            "pair_id": pair_id,
                            "role": "primary" if pair_id in contract["primary_pair_ids"] else "reserve",
                            "arm_order": list(selected.arms),
                            "fresh_range_per_arm": True,
                        }
                        for pair_id in contract["pair_ids"]
                    ],
                    "first_pair_gate": contract["primary_pair_ids"][0],
                    "stop_loss": "two consecutive failed pairs or maximum pair-attempt budget",
                    "automatic_expansion": False,
                }, sort_keys=True))
            print(
                "\nUNALLOCATED: protocol assembly only. No reset, arm, model, Mythic, "
                "BloodHound, range, target, or comparison command is emitted."
            )
            return
    if measurement_kind == "row":
        print(f"  durable-artifact-dir={_gate1p_artifact_dir()}\n")
    for s in range(seeds):
        for arm_index, sd in enumerate([side] if side else ["harness", "bare"]):
            print(f"--- iteration seed={s} side={sd} ---")
            preflight_argv = _side_preflight_argv(
                scenario,
                provider=provider,
                model=model,
                max_output_tokens=max_output_tokens,
            )
            preflight_note = (
                "     # exact model-route smoke clamps to <=8 output tokens, 10s, zero retries"
                if measurement_kind == "row"
                else ""
            )
            print(f"  (cd {PAYLOAD}) {' '.join(preflight_argv)}{preflight_note}")
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
                if name == "wipe bloodhound":
                    print(
                        f"  (cd {ROOT}) uv --directory {BH} run python "
                        f"{ROOT / 'skills/sage-goad-reset/scripts/bh_reset.py'} status     "
                        "# poll every 5s for <=120s until authoritative count=0"
                    )
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
                  f"{' --null-model' if null_model else ''}"
                  f"{f' --measurement-kind {measurement_kind}' if measurement_kind != 'legacy' else ''}"
                  f"{f' --protocol-id {protocol_id}' if protocol_id else ''}"
                  f"{f' --pair-id {pair_id}' if pair_id else ''}"
                  f"{f' --arm-index {arm_index}' if pair_id else ''}"
                  f"{f' --model-token-budget {model_token_budget}' if model_token_budget else ''}"
                  f"{f' --model-context-limit {model_context_limit}' if model_context_limit else ''}"
                  f"{f' --max-output-tokens {max_output_tokens}' if max_output_tokens else ''}"
                  f"{f' --source-version {source_version}' if source_version else ''}")
            if measurement_kind == "row":
                print(f"  (cd {ROOT}) /bin/bash skills/sage-goad-reset/scripts/sage_stop.sh     # cleanup")
                print(f"  (cd {ROOT}) {PY} skills/sage-goad-reset/scripts/archive_runtime_dbs.py     # cleanup")
                print(f"  (cd {PAYLOAD}) {PY} ai/hillclimb/run_gauge_live.py verify-no-running-tasks     # cleanup")
                print(
                    f"  (cd {ROOT}) {' '.join(_ludus_argv('poweroff', 'all', range_id=ludus_range_id, mcp_server=ludus_mcp_server))}"
                    "     # cleanup; retained disposition required"
                )
    if measurement_kind != "diagnostic":
        print(
            f"  (cd {PAYLOAD}) {PY} ai/hillclimb/run_gauge_live.py compare --scenario {scenario}"
            f"{f' --pair-id {pair_id}' if pair_id else ''}"
        )
    gate1p = _gate1p_stagea_protocol()
    if pair_id == gate1p.PAIR_ID and not gate1p.PAIR_ALLOCATED:
        print("\nPAIR UNALLOCATED: this prospective plan cannot execute live until a fresh pair identity is allocated.")
    else:
        print("\nRe-run with --go to execute. ABORTS on any reset/bootstrap/callback failure.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Bare-vs-harness gauge orchestrator (resets the lab per run).")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--side", choices=["harness", "bare"], default=None,
                    help="run only this side; omit to sweep harness+bare then compare")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument(
        "--gate1p-stage-a-diagnostic",
        action="store_true",
        help="run exactly one non-countable shipping-Sage child-domain-control production-shape diagnostic",
    )
    ap.add_argument(
        "--gate1p-stage-a-pair",
        action="store_true",
        help="run the exact accepted one-row shipping-Sage/true-naive Stage A pair",
    )
    ap.add_argument(
        "--gate1p-stage-b-protocol",
        action="store_true",
        help="assemble the prospective unallocated Stage B child-control protocol offline",
    )
    ap.add_argument("--model-context-limit", type=int, default=None,
                    help="verified provider per-request context limit for the paired row")
    ap.add_argument("--max-output-tokens", type=int, default=None,
                    help="shared configured maximum output tokens for the paired row")
    ap.add_argument("--source-version", default=None,
                    help="exact source version recorded in the diagnostic protocol snapshot")
    ap.add_argument(
        "--live-authority-id",
        default=None,
        help="separate explicit live authority identifier; required with --go for the Stage A pair",
    )
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
    if sum(map(bool, (
        args.gate1p_stage_a_diagnostic,
        args.gate1p_stage_a_pair,
        args.gate1p_stage_b_protocol,
    ))) > 1:
        ap.error("Gate 1P diagnostic, Stage A pair, and Stage B protocol modes are mutually exclusive")
    if args.gate1p_stage_a_diagnostic:
        if args.scenario != "child-domain-control" or args.side != "harness" or args.seeds != 1:
            ap.error(
                "--gate1p-stage-a-diagnostic requires --scenario child-domain-control "
                "--side harness --seeds 1"
            )
        if policy_mode != "llm" or null_model:
            ap.error("the Stage A diagnostic uses the shipping llm policy, not an ablation")
        if not BH:
            ap.error(
                "SAGE_BLOODHOUND_MCP_DIR is required for the Gate 1P diagnostic; "
                "set it to the explicit BloodHound MCP checkout"
            )
        if not Path(BH).is_dir():
            ap.error(
                "SAGE_BLOODHOUND_MCP_DIR must name an existing directory for the Gate 1P diagnostic"
            )
    stageb = _gate1p_protocol_selector().protocol_for_scenario("child-control").module
    if args.gate1p_stage_b_protocol:
        if args.scenario != stageb.SCENARIO or args.side is not None or args.seeds != 1:
            ap.error(
                f"--gate1p-stage-b-protocol requires --scenario {stageb.SCENARIO} "
                "with no --side and --seeds 1"
            )
        if policy_mode != stageb.POLICY_MODE or null_model or args.treatment:
            ap.error("the Stage B protocol declares only the shipping llm policy and true-naive arm")
        if args.go:
            ap.error(f"{stageb.PROTOCOL_ID} is unallocated; --go cannot reset or execute anything")
    route_env = None
    if args.treatment:
        route = load_treatment_route(args.route_env, args.treatment)
        args.provider = route["provider"]
        args.model = route["model"]
        route_env = {
            "SAGE_EVAL_API_ENDPOINT": route["api_endpoint"],
            "SAGE_EVAL_API_KEY": route["api_key"],
        }
    gate1p = _gate1p_stagea_protocol()
    if args.gate1p_stage_a_pair:
        if args.scenario != gate1p.SCENARIO or args.side is not None or args.seeds != 1:
            ap.error(
                f"--gate1p-stage-a-pair requires --scenario {gate1p.SCENARIO} "
                "with no --side and --seeds 1"
            )
        if policy_mode != gate1p.POLICY_MODE or null_model or args.null_model_factorial:
            ap.error("the Stage A pair uses only the shipping llm policy and true-naive arm")
        if args.solve_timeout != gate1p.WALL_SECONDS:
            ap.error(f"the Stage A pair requires --solve-timeout {gate1p.WALL_SECONDS}")
        if args.snapshot != gate1p.RESET_SNAPSHOT:
            ap.error(f"the Stage A pair requires --snapshot {gate1p.RESET_SNAPSHOT}")
        actual_foothold = {
            "host": foothold.host,
            "ip": foothold.ip,
            "identity": foothold.user,
            "callback_user": foothold.callback_user,
            "payload_type": "apollo",
        }
        if actual_foothold != gate1p.INITIAL_FOOTHOLD_ROLE:
            ap.error("the Stage A pair requires the fixed CASTELBLACK/Samwell Apollo foothold role")
        args.provider = args.provider or gate1p.MODEL_PROVIDER
        args.model = args.model or gate1p.MODEL_ID
        if args.provider.casefold() != gate1p.MODEL_PROVIDER or args.model != gate1p.MODEL_ID:
            ap.error(
                f"the Stage A pair requires provider/model "
                f"{gate1p.MODEL_PROVIDER}/{gate1p.MODEL_ID}"
            )
        if not args.source_version:
            ap.error("--source-version is required for the Stage A pair")
        if not args.model_context_limit or not args.max_output_tokens:
            ap.error("--model-context-limit and --max-output-tokens are required for the Stage A pair")
        if args.max_output_tokens > args.model_context_limit:
            ap.error("--max-output-tokens cannot exceed --model-context-limit")
        if not BH or not Path(BH).is_dir():
            ap.error(
                "SAGE_BLOODHOUND_MCP_DIR must name an existing directory for the Stage A pair"
            )
        effective_route_env = route_env or os.environ
        if not str(effective_route_env.get("SAGE_EVAL_API_ENDPOINT") or "").strip():
            ap.error("SAGE_EVAL_API_ENDPOINT is required for exact Stage A route parity")
        if args.go and not gate1p.PAIR_ALLOCATED:
            ap.error("the Gate 1P successor pair is not allocated for live execution")
        if args.go and args.live_authority_id != gate1p.PAIR_ID:
            ap.error(
                f"live paired-row execution requires separate --live-authority-id {gate1p.PAIR_ID}"
            )
        if args.go:
            validate_gate1p_storage(route_env)

    if not args.go:
        for selected_mode in policy_modes:
            _dry_run_plan(args.scenario, "harness" if args.null_model_factorial else args.side,
                          args.seeds, args.solve_timeout, selected_mode,
                          args.provider, args.model, null_model,
                          (
                              "row" if args.gate1p_stage_a_pair or args.gate1p_stage_b_protocol
                              else "diagnostic" if args.gate1p_stage_a_diagnostic
                              else "legacy"
                          ),
                          (
                              gate1p.PROTOCOL_ID if args.gate1p_stage_a_pair
                              else stageb.PROTOCOL_ID if args.gate1p_stage_b_protocol
                              else "gate1p-stage-a-diagnostic-v1"
                              if args.gate1p_stage_a_diagnostic
                              else None
                          ),
                          gate1p.PAIR_ID if args.gate1p_stage_a_pair else None,
                          (
                              gate1p.TOKEN_ADMISSION_CEILING if args.gate1p_stage_a_pair
                              else stageb.TOKEN_ADMISSION_CEILING
                              if args.gate1p_stage_b_protocol else None
                          ),
                          args.model_context_limit,
                          args.max_output_tokens,
                          args.source_version,
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
    gate1p_start_arm_index = 0
    if args.gate1p_stage_a_pair:
        control_path = initialize_gate1p_pair_control(
            source_version=str(args.source_version),
            route_env=route_env,
        )
        control = json.loads(control_path.read_text(encoding="utf-8"))
        gate1p_start_arm_index = gate1p.ARMS.index(str(control["next_arm"]))
    for selected_mode in policy_modes:
        for _seed_index in range(args.seeds):
            if args.gate1p_stage_a_pair:
                indexed_sides = list(enumerate(["harness", "bare"]))[gate1p_start_arm_index:]
            else:
                sides = ["harness"] if args.null_model_factorial else (
                    [args.side] if args.side else ["harness", "bare"]
                )
                indexed_sides = list(enumerate(sides))
            for arm_index, side in indexed_sides:
                run_side_with_gate1p_cleanup(
                         args.scenario, side, go=True, solve_timeout=args.solve_timeout,
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
                         phase8_attempt_index=args.phase8_attempt_index,
                         measurement_kind=(
                             "row" if args.gate1p_stage_a_pair
                             else "diagnostic" if args.gate1p_stage_a_diagnostic
                             else "legacy"
                         ),
                         protocol_id=(
                             gate1p.PROTOCOL_ID if args.gate1p_stage_a_pair
                             else "gate1p-stage-a-diagnostic-v1"
                             if args.gate1p_stage_a_diagnostic else None
                         ),
                         pair_id=gate1p.PAIR_ID if args.gate1p_stage_a_pair else None,
                         arm_index=arm_index if args.gate1p_stage_a_pair else None,
                         model_token_budget=(
                             gate1p.TOKEN_ADMISSION_CEILING if args.gate1p_stage_a_pair else None
                         ),
                         model_context_limit=args.model_context_limit,
                         max_output_tokens=args.max_output_tokens,
                         source_version=args.source_version,
                         live_authority_id=(
                             args.live_authority_id if args.gate1p_stage_a_pair else None
                         ))
    if not args.side and not args.null_model_factorial and not args.gate1p_stage_a_diagnostic:
        compare(
            args.scenario,
            pair_id=gate1p.PAIR_ID if args.gate1p_stage_a_pair else None,
            results_path=_gate1p_results_path(route_env) if args.gate1p_stage_a_pair else None,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
