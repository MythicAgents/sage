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

ROOT = Path(__file__).resolve().parents[3]            # /home/john/dev/sage
PAYLOAD = ROOT / "Payload_Type" / "sage"
PY = str(ROOT / ".venv" / "bin" / "python")
BH = "/home/john/dev/bloodhound_mcp"
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
DEFAULT_LUDUS_RANGE_ID = os.environ.get("SAGE_LUDUS_RANGE_ID") or None
DEFAULT_FOOTHOLD_HOST = "CASTELBLACK"
DEFAULT_FOOTHOLD_IP = "10.4.10.22"
DEFAULT_FOOTHOLD_USER = r"NORTH\samwell.tarly"
DEFAULT_FOOTHOLD_CALLBACK_HOST = DEFAULT_FOOTHOLD_HOST
DEFAULT_FOOTHOLD_CALLBACK_USER = "samwell.tarly"
DEFAULT_FOOTHOLD_PASSWORD_ENV = "SAGE_RUN_AS_PASSWORD"


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
    ) -> None:
        self.host = host
        self.ip = ip
        self.user = user
        self.callback_host = callback_host or host
        self.callback_user = callback_user
        self.password_env = password_env
        self.ludus_range_id = ludus_range_id

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
        return argv

    def launch_env(self) -> dict[str, str]:
        env = dict(os.environ)
        password = _resolve_password_source(self.password_env)
        if password:
            env["SAGE_RUN_AS_PASSWORD"] = password
        if self.ludus_range_id:
            env["SAGE_LUDUS_RANGE_ID"] = self.ludus_range_id
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

# (name, argv, cwd, timeout_s). Reset + bootstrap, in order, per sage-goad-reset + sage-callback-bootstrap.
RESET_STEPS = [
    ("stop sage",        ["/bin/bash", "skills/sage-goad-reset/scripts/sage_stop.sh"], ROOT, 120),
    ("archive dbs",      [PY, "skills/sage-goad-reset/scripts/archive_runtime_dbs.py"], ROOT, 180),
    ("reset mythic",     ["/bin/bash", "skills/sage-goad-reset/scripts/mythic_reset.sh", "--yes"], ROOT, 900),
    ("ludus rollback",   [PY, "skills/sage-goad-reset/scripts/ludus.py", "rollback", "--yes"], ROOT, 2400),
    ("ludus poweron",    [PY, "skills/sage-goad-reset/scripts/ludus.py", "poweron", "all"], ROOT, 900),
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


def _ludus_argv(*args: str, range_id: str | None = None) -> list[str]:
    argv = [PY, "skills/sage-goad-reset/scripts/ludus.py"]
    if range_id:
        argv.extend(["--range-id", range_id])
    argv.extend(args)
    return argv


def _range_env(range_id: str | None = None) -> dict[str, str] | None:
    if not range_id:
        return None
    return {**os.environ, "SAGE_LUDUS_RANGE_ID": range_id}


def _engagement_netbios_map(scenario: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
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
        elif scenario == "purpose-range-recovery":
            env.update(DEFAULT_PURPOSE_RANGE_RECOVERY_BLOCKER_ENV)
        return env
    if scenario == "direct-laps-ca-export-recovery":
        return dict(DEFAULT_DIRECT_LAPS_CA_EXPORT_RECOVERY_ENV)
    return {}


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


def _available_snapshots(range_id: str | None = None) -> set[str]:
    proc = subprocess.run(
        _ludus_argv("snapshots", range_id=range_id),
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
        os.environ.get("MYTHIC_ENV_PATH") or str(Path.home() / "dev" / "mythic_v4" / ".env"),
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
) -> None:
    foothold = foothold or FootholdSpec()
    available_snapshots = _available_snapshots(ludus_range_id or foothold.ludus_range_id)
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
    range_env = _range_env(ludus_range_id)
    for name, argv, cwd, timeout in RESET_STEPS:
        step_argv = argv
        if name == "ludus rollback" and snapshot:
            step_argv = _ludus_argv("rollback", snapshot, "--yes", range_id=ludus_range_id)
        if name == "ludus poweron":
            step_argv = _ludus_argv("poweron", "all", range_id=ludus_range_id)
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
        _ludus_argv("status", range_id=ludus_range_id),
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
             engagement_netbios_map: str | None = None) -> None:
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
    _sage_cb, apollo_cb = full_reset_and_ready(
        restart_env=restart_env,
        snapshot=snapshot,
        retained_callback_config=retained_callback_config,
        foothold=foothold,
        ludus_range_id=ludus_range_id,
    )
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
    child_env = None
    if route_env:
        child_env = {**os.environ, **route_env}
    _run(f"gauge {side}/{scenario}", argv, PAYLOAD, step_timeout, env=child_env)


def compare(scenario: str) -> None:
    _run(f"compare {scenario}", [PY, "ai/hillclimb/run_gauge_live.py", "compare", "--scenario", scenario], PAYLOAD, 120)


def _dry_run_plan(scenario, side, seeds, solve_timeout, policy_mode, provider=None, model=None,
                  null_model=False,
                  snapshot=DEFAULT_SNAPSHOT,
                  retained_callback_config=DEFAULT_RETAINED_CALLBACK_CONFIG,
                  foothold: FootholdSpec | None = None,
                  ludus_range_id: str | None = None):
    foothold = foothold or FootholdSpec()
    ludus_range_id = ludus_range_id or foothold.ludus_range_id
    step_timeout = max(3600, solve_timeout + _GAUGE_STEP_OVERHEAD_S)
    print("DRY RUN (no --go). Plan — each gauge run gets its OWN fresh range:\n")
    print(f"  solve-timeout={solve_timeout}s per solve; per-run subprocess cap={step_timeout}s\n")
    for s in range(seeds):
        for sd in ([side] if side else ["harness", "bare"]):
            print(f"--- iteration seed={s} side={sd} ---")
            for name, argv, cwd, _ in RESET_STEPS:
                if name == "ludus rollback" and snapshot:
                    argv = _ludus_argv("rollback", snapshot, "--yes", range_id=ludus_range_id)
                if name == "ludus poweron":
                    argv = _ludus_argv("poweron", "all", range_id=ludus_range_id)
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
            print(f"  (cd {ROOT}) {' '.join(_ludus_argv('status', range_id=ludus_range_id))}     # poll until {foothold.host} is up")
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
        "--engagement-netbios-map",
        default=None,
        help=(
            "JSON NetBIOS-to-FQDN map passed to Sage. "
            "Defaults to the GOAD map or the selected purpose-range map based on --scenario."
        ),
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
    )
    policy_mode = "symbolic" if args.controller else args.policy_mode
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
                          args.ludus_range_id)
            print(f"\n  NOTE: Sage policy mode -> {selected_mode}; null_model={null_model}.")
        return 0

    validate_reset_inputs(args.snapshot, args.retained_callback_config, foothold, args.ludus_range_id)
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
                         engagement_netbios_map=args.engagement_netbios_map)
    if not args.side and not args.null_model_factorial:
        compare(args.scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
