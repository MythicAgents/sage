import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "skills/sage-eval-gauge/scripts/run_capture_episode.py"
SPEC = importlib.util.spec_from_file_location("run_capture_episode", SCRIPT)
driver = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = driver
assert SPEC.loader is not None; SPEC.loader.exec_module(driver)


def _resource_files(tmp_path):
    config = tmp_path / "budget.json"; ledger = tmp_path / "shared-resource-ledger.jsonl"
    names = ("episodes", "provider_requests", "provider_tokens", "payload_task_attempts", "control_requests",
             "guest_commands", "physical_restores", "callback_launches", "replacement_payloads",
             "new_staged_snapshots", "protected_deployments", "burned_setup_retries", "capture_correction_cycles")
    config.write_text(json.dumps({"schema": "sage-shared-resource-budget-v1", "resources": {
        name: {"limit": 10000000, "cleanup_reserve": 1 if name in {"control_requests", "guest_commands",
            "physical_restores", "callback_launches"} else 0} for name in names},
        "calendar_seconds": 86400, "cleanup_seconds": 3600}))
    return config, ledger


def _frozen(tmp_path):
    config, ledger = _resource_files(tmp_path); source = tmp_path / "source.json"; approved = tmp_path / "approved.json"
    source.write_text(json.dumps({"files": {"x": "y"}})); approved.write_text("{}")
    receiver = tmp_path / "receiver.jsonl"
    value = {
        "schema": driver.SCHEMA,
        "authority": {"path": str(approved), "sha256": "a" * 64, "id": "authority"},
        "source_binding": {"path": str(source), "sha256": "b" * 64},
        "identity": {"episode_id": "episode-1", "protocol_id": "p", "pair_id": "pair", "arm": "baseline",
                     "model_version": "mv", "source_version": "sv", "logical_seed": 1, "side": "harness",
                     "schedule_id": "m3-order-v1:00001", "stage": "capture-canaries", "reset_key": "reset"},
        "range": {"profile": "goad-trust-walker", "range_id": "test-range", "mcp_server": "ludus",
                  "snapshot": "snapshot", "scenario": "cross-forest-objective", "expected_vmids": [114],
                  "retained_callback_config": str(tmp_path / "callback.json"),
                  "retained_callback_config_sha256": "e" * 64},
        "model": {"provider": "openai", "model_id": "test-model", "policy_mode": "llm",
                  "context_limit": 32768, "max_output_tokens": 4096, "temperature": 0, "max_retries": 0,
                  "route_endpoint": "https://example.invalid", "backend_identity_sha256": "f" * 64},
        "resources": {"config": str(config), "config_sha256": "c" * 64, "ledger": str(ledger),
                      "docker_socket": str(tmp_path / "docker.sock"),
                      "episode": {"action_seconds": 2700, "settlement_seconds": 300,
                                  "provider_tokens": 1_000_000, "per_call_reservation": 32768,
                                  "payload_tasks": 128}},
        "observation": {"max_events": 100, "max_bytes": 1000000, "max_trace_bytes": 1000000,
                        "receiver_receipt_path": str(receiver), "capture_results_path": str(tmp_path / "results"),
                        "driver_result_path": str(tmp_path / "driver-result.json"),
                        "bloodhound": {}},
    }
    return driver.FrozenCaptureSpec(tmp_path / "spec.json", "d" * 64, value), config, ledger, receiver, source, approved


def _seal_frozen(frozen, config, source, approved, tmp_path):
    package = tmp_path / "package.md"; package.write_text("frozen package\n")
    package_freeze = tmp_path / "package-freeze.json"; package_freeze.write_text("{}\n")
    callback = Path(frozen.value["range"]["retained_callback_config"]); callback.write_text("{}\n")
    frozen.value["range"]["retained_callback_config_sha256"] = driver._digest(callback.read_bytes())
    row = {"analysis_countable": False, "arm": "baseline", "batch": None, "case_id": None,
        "execution_status": "unstarted", "family": frozen.value["range"]["profile"], "look": None,
        "matched_cell_id": frozen.value["identity"]["pair_id"], "provider_seed": None,
        "reset_key": frozen.value["identity"]["reset_key"], "reset_replicate": frozen.value["identity"]["logical_seed"],
        "schedule_id": frozen.value["identity"]["schedule_id"], "sequence": 1, "stage": "capture-canaries",
        "within_cell_position": 1}
    schedule = tmp_path / "schedule.jsonl"; schedule.write_text(json.dumps(row) + "\n")
    schedule_metadata = tmp_path / "schedule.json"
    schedule_metadata.write_text(json.dumps({"package_sha256": driver._digest(package.read_bytes()),
        "schedule_sha256": driver._digest(schedule.read_bytes()), "schedule_file": schedule.name}))
    config_value = json.loads(config.read_text())
    limits = {"calendar_days": 1, "cleanup_reserve_hours": 1,
              **{name: value["limit"] for name, value in config_value["resources"].items()}}
    reserves = {name: value["cleanup_reserve"] for name, value in config_value["resources"].items()
                if value["cleanup_reserve"]}
    approved.write_text(json.dumps({"status": "active", "live_authorized": True, "authority_id": "authority",
        "package_path": str(package), "package_sha256": driver._digest(package.read_bytes()),
        "source_freeze_path": str(package_freeze), "source_freeze_sha256": driver._digest(package_freeze.read_bytes()),
        "limits": limits, "cleanup_reserves": reserves, "per_episode": {"action_seconds": 2700,
        "settlement_seconds": 300, "nominal_tokens": 1000000, "per_call_token_reservation": 32768,
        "payload_task_attempts": 128, "automatic_provider_retries": 0}}))
    role_files = {}
    for role in driver._CAPTURE_FILE_ROLES:
        path = tmp_path / (role + ".py"); path.write_text("# bound\n"); role_files[role] = str(path)
    source.write_text(json.dumps({"schema": driver.SOURCE_BINDING_SCHEMA,
        "approved_execution_path": str(approved), "approved_execution_sha256": driver._digest(approved.read_bytes()),
        "package": {"path": str(package), "sha256": driver._digest(package.read_bytes())},
        "package_source_freeze": {"path": str(package_freeze), "sha256": driver._digest(package_freeze.read_bytes())},
        "schedule": {"metadata_path": str(schedule_metadata), "metadata_sha256": driver._digest(schedule_metadata.read_bytes()),
            "path": str(schedule), "sha256": driver._digest(schedule.read_bytes()), "selected_row": row},
        "execution": {"side": frozen.value["identity"]["side"], "notification_mode": "disabled",
            "measurement_identity": frozen.value["identity"],
            "family": frozen.value["range"], "generation": frozen.value["model"]},
        "file_roles": role_files,
        "files": {path: driver._digest(Path(path).read_bytes()) for path in role_files.values()}}))
    frozen.value["authority"]["sha256"] = driver._digest(approved.read_bytes())
    frozen.value["source_binding"]["sha256"] = driver._digest(source.read_bytes())
    frozen.value["resources"]["config_sha256"] = driver._digest(config.read_bytes())
    frozen.path.write_text(json.dumps(frozen.value))
    return driver.FrozenCaptureSpec(frozen.path, driver._digest(frozen.path.read_bytes()), frozen.value)


def _bind_capture_recovery(frozen, source, disposition, *, prior=None):
    binding = json.loads(source.read_text())
    binding["capture_recovery"] = {"prior_disposition": prior or {
        "path": str(disposition), "sha256": driver._digest(disposition.read_bytes())}}
    source.write_text(json.dumps(binding))
    frozen.value["source_binding"]["sha256"] = driver._digest(source.read_bytes())
    frozen.path.write_text(json.dumps(frozen.value))
    return driver.FrozenCaptureSpec(
        frozen.path, driver._digest(frozen.path.read_bytes()), frozen.value)


def _snapshot():
    return {"complete": True, "operation_id": 9, "rows": {
        "callback": [{"id": 5, "display_id": 7}], "task": [], "response": [], "credential": [],
        "chat_request": [], "chat_message": []}}


def test_execute_uses_actual_boundary_order_and_projects_only_after_terminal_secret_registration(monkeypatch, tmp_path):
    global_syscall_complete = False
    frozen, config, ledger, receiver_path, source, approved = _frozen(tmp_path)
    monkeypatch.setattr(driver, "_validate", lambda *_a, **_k: {
        "approved": approved, "source_binding": source, "config": config, "ledger": ledger, "approval": {},
        "execution": {"generation": frozen.value["model"]}})
    monkeypatch.setattr(driver, "_validated_route_env", lambda _execution: {})
    @contextmanager
    def runtime(*_args): yield {}
    monkeypatch.setattr(driver, "_prepare_runtime", runtime)
    from ai import resource_clients
    def returned(adapter, request, **_kwargs):
        import requests
        response = requests.Response(); response.status_code = 200; response._content = b"{}"; response.request = request
        return response
    monkeypatch.setattr(resource_clients, "_ORIGINAL_REQUESTS_SEND", returned)
    order = []; inventories = iter([_snapshot(), _snapshot()])
    def collect(operation_id=None):
        order.append("initial_inventory" if operation_id is None else "terminal_inventory")
        if operation_id is not None:
            assert os.environ.get("SAGE_RESOURCE_EPISODE_ID") == "episode-1"
            assert os.environ.get("SAGE_OBSERVATION_DIRECTORY")
            import requests
            resource_clients.install(clients=("requests",)); session = requests.Session(); session.trust_env = False
            assert session.get("https://127.0.0.1:1/schema", timeout=.1).status_code == 200
        return next(inventories)
    def run_side(_scenario, _side, **kwargs):
        from ai.resource_budget import ResourceLedger
        from ai import observation_channel
        old = os.environ.get(observation_channel.ENV_NAME); os.environ[observation_channel.ENV_NAME] = str(kwargs["observation_directory"])
        try:
            kwargs["before_reset"](); observation_channel.emit("process_start", {"attempted": False})
            observation_channel.emit("tool_event", {"event": "observed_process_streams",
                "tracee_pid": os.getpid(), "streams": [{"fd": 1}, {"fd": 2}]})
            kwargs["after_reset"](None, 7); order.append("after_reset")
            ResourceLedger(kwargs["resource_budget_config"], kwargs["resource_budget_ledger"]).activate_episode(
                kwargs["resource_episode_id"])
            observation_channel.emit("control_response", {"transport": "requests", "outcome": "returned",
                "body_base64": "aXNzdWVkLWFkbWluLXRva2Vu"})
            observation_channel.emit("tool_event", {"event": "measurement_capture_episode", "secret": "synthetic-secret"})
            observation_channel.emit("process_end", {"attempted": False})
            config_value = json.loads(Path(kwargs["route_env"]["SAGE_PROCESS_OBSERVER_CONFIG"]).read_text())
            Path(config_value["receipt_path"]).write_text(json.dumps({"complete": global_syscall_complete,
                "failures": [] if global_syscall_complete else ["unsupported_sink_transfer"],
                "tracee_pid": os.getpid(), "observed_pids": [os.getpid()], "ended_pids": [os.getpid()]}))
        finally:
            if old is None: os.environ.pop(observation_channel.ENV_NAME, None)
            else: os.environ[observation_channel.ENV_NAME] = old
    class Index:
        def __init__(self): self.dynamic = []
        def redact_bytes(self, value):
            value = value.replace(b"synthetic-secret", b"<SAGE_REDACTED>")
            for secret in self.dynamic: value = value.replace(secret, b"<SAGE_REDACTED>")
            return {"redacted": value, "metadata": {}}
        def redact_json(self, value):
            encoded = json.dumps(value).replace("synthetic-secret", "<SAGE_REDACTED>")
            for secret in self.dynamic: encoded = encoded.replace(secret.decode(), "<SAGE_REDACTED>")
            return json.loads(encoded)
    def compile_observations(_spec, before, after, process, output, cleanup, _index):
        assert before["operation_id"] == after["operation_id"] == 9
        assert process["complete"] is global_syscall_complete
        assert output["complete"] is True and cleanup["complete"] is True
        class Compiler:
            terminal_controls = 0
            def observe(self, event):
                if event.get("payload", {}).get("event") == "measurement_capture_episode":
                    assert event["payload"]["secret"] == "synthetic-secret"
                if event.get("kind") == "control_request" and event.get("payload", {}).get("transport") == "requests":
                    self.terminal_controls += 1
            def finish(self, events):
                assert self.terminal_controls == 1
                order.append("compile"); assert "synthetic-secret" not in json.dumps(events)
                return {"disposition": "unadmitted", "episode_hash": "f" * 64, "admissible": True}
        return Compiler()
    compile_observations.start_window = lambda: order.append("compiler_window_start")
    compile_observations.end_window = lambda: order.append("compiler_window_end")
    compile_observations.scope = {"trusted_state_prefixes": ["/tmp/caller-supplied-broad-prefix"]}
    def secret_registrar(index):
        def register(event):
            if event.get("kind") == "control_response" and event.get("payload", {}).get("body_base64"):
                index.dynamic.append(b"issued-admin-token"); order.append("dynamic_secret_registration")
        return register
    compile_observations.secret_registrar = secret_registrar
    result = driver.execute_capture(frozen, lambda _a, _b: (order.append("secret_registration") or Index()),
        compile_observations, run_side=run_side, inventory_collector=collect,
        cleanup_runner=lambda *_a: (order.append("cleanup") or {"complete": True, "steps": []}))
    assert order[:7] == ["initial_inventory", "compiler_window_start", "after_reset", "cleanup",
                        "terminal_inventory", "compiler_window_end", "secret_registration"]
    assert order[7:-1] and set(order[7:-1]) == {"dynamic_secret_registration"}
    assert order[-1] == "compile"
    assert result["capture_complete"] is True and result["cleanup"]["complete"] is True
    prefixes=compile_observations.scope["trusted_state_prefixes"]
    assert len(prefixes)==1 and Path(prefixes[0]).name=="engagement"
    assert Path(prefixes[0]).parent.name.startswith("sage-m3-capture-")
    assert result["process_observer_receipt"]["failures"] == ["unsupported_sink_transfer"]
    assert "synthetic-secret" not in receiver_path.read_text()


def test_started_reset_failure_still_runs_cleanup_and_closes_episode(monkeypatch, tmp_path):
    frozen, config, ledger, _receiver_path, source, approved = _frozen(tmp_path)
    monkeypatch.setattr(driver, "_validate", lambda *_a, **_k: {
        "approved": approved, "source_binding": source, "config": config, "ledger": ledger, "approval": {},
        "execution": {"generation": frozen.value["model"]}})
    monkeypatch.setattr(driver, "_validated_route_env", lambda _execution: {})
    @contextmanager
    def runtime(*_args): yield {}
    monkeypatch.setattr(driver, "_prepare_runtime", runtime)
    cleaned = []
    def failed(_scenario, _side, **kwargs):
        kwargs["before_reset"](); raise RuntimeError("reset failed")
    compiler=lambda *_a:{};compiler.scope={}
    result = driver.execute_capture(frozen, lambda *_a: None, compiler, run_side=failed,
        inventory_collector=lambda _id=None: _snapshot(),
        cleanup_runner=lambda *_a: (cleaned.append(True) or {"complete": False, "steps": []}))
    assert cleaned == [True] and result["capture_complete"] is False
    assert result["error_type"] == "RuntimeError" and result["cleanup"]["complete"] is False
    assert compiler.scope["resource_starting_seq"]==2
    from ai.resource_budget import ResourceLedger
    assert ResourceLedger(config, ledger).status()["episodes"]["episode-1"]["closed_epoch"] is not None


def test_actual_reset_stage_failure_is_retained_with_cleanup_without_child_secret(monkeypatch,tmp_path):
    frozen,config,ledger,_receiver,source,approved=_frozen(tmp_path)
    monkeypatch.setattr(driver,"_validate",lambda *_a,**_k:{"approved":approved,"source_binding":source,
        "config":config,"ledger":ledger,"approval":{},"execution":{"generation":frozen.value["model"]}})
    monkeypatch.setattr(driver,"_validated_route_env",lambda _execution:{})
    @contextmanager
    def runtime(*_args):yield {}
    monkeypatch.setattr(driver,"_prepare_runtime",runtime);cleaned=[]
    def actual_reset(_scenario,_side,**kwargs):
        reset=driver._module("orchestrate");reset.RESET_STEPS=[("stop sage",["/bin/true"],tmp_path,5),
            ("archive dbs",[sys.executable,"-c","import os,signal;print('PLANTED-RESET-CHILD-SECRET',flush=True);os.kill(os.getpid(),signal.SIGTERM)"],tmp_path,5)]
        reset._range_env=lambda *_a,**_k:{};kwargs["before_reset"]()
        reset.full_reset_and_ready(setup_stage_callback=kwargs["setup_stage_callback"])
    compiler=lambda *_a:{};compiler.scope={}
    result=driver.execute_capture(frozen,lambda *_a:None,compiler,run_side=actual_reset,
        inventory_collector=lambda *_a:_snapshot(),cleanup_runner=lambda *_a:(cleaned.append(True) or {"complete":True,"steps":[]}))
    assert cleaned==[True] and result["capture_complete"] is False and result["error_type"]=="SystemExit"
    assert [(row["stage"],row["status"]) for row in result["setup_stages"]]==[
        ("stop-sage","started"),("stop-sage","passed"),("archive-dbs","started"),("archive-dbs","failed")]
    assert result["failed_setup_stage"]=="archive-dbs" and result["setup_stages"][-1]["exit_code"]==-15
    retained=tmp_path/"setup-result.json";driver._retain_driver_result(retained,result)
    assert "PLANTED-RESET-CHILD-SECRET" not in retained.read_text()


def test_capture_setup_stage_accepts_only_fixed_mythic_reset_diagnostic():
    records=[];states={}
    started={"stage":"reset-mythic","status":"started","error_class":None,
        "exit_code":None,"timeout_seconds":None,"diagnostic_code":None}
    driver._capture_setup_stage(records,states,started)
    assert "diagnostic_code" not in records[0]
    failed={**started,"status":"failed","error_class":"SystemExit","exit_code":1,
        "diagnostic_code":"postgres_remove_failed"}
    driver._capture_setup_stage(records,states,failed)
    assert records[-1]["diagnostic_code"]=="postgres_remove_failed"

    for invalid in ("PLANTED-SECRET", ["postgres_remove_failed"]):
        with pytest.raises(driver.CaptureEpisodeError):
            driver._capture_setup_stage([], {"reset-mythic":"started"},
                {**failed,"diagnostic_code":invalid})
    with pytest.raises(driver.CaptureEpisodeError):
        driver._capture_setup_stage([], {"archive-dbs":"started"},
            {**failed,"stage":"archive-dbs"})


def test_child_preflight_failure_is_retained_before_reset_without_output_secret(monkeypatch,tmp_path):
    frozen,config,ledger,_receiver,source,approved=_frozen(tmp_path)
    monkeypatch.setattr(driver,"_validate",lambda *_a,**_k:{"approved":approved,"source_binding":source,
        "config":config,"ledger":ledger,"approval":{},"execution":{"generation":frozen.value["model"]}})
    monkeypatch.setattr(driver,"_validated_route_env",lambda _execution:{})
    @contextmanager
    def runtime(*_args):yield {}
    monkeypatch.setattr(driver,"_prepare_runtime",runtime);reset=[]
    def failed(_scenario,_side,**kwargs):
        code="""import sys
sys.path.insert(0,%r)
import run_gauge_live as producer
producer.scenario_preconditions=lambda _cfg,_scenario:[
 ('objective-recognizable','offline',lambda:(True,'recognized')),
 ('mythic-queries-valid','smoke',lambda:(False,'PLANTED-PREFLIGHT-OUTPUT-SECRET'))]
raise SystemExit(producer.main(['preflight','--scenario','cross-forest-objective']))
""" % str(ROOT/"Payload_Type/sage/ai/hillclimb")
        env={**os.environ,"SAGE_OBSERVATION_DIRECTORY":str(kwargs["observation_directory"]),
             "PYTHONDONTWRITEBYTECODE":"1"}
        completed=__import__("subprocess").run([sys.executable,"-P","-c",code],env=env)
        config_value=json.loads(Path(kwargs["route_env"]["SAGE_PROCESS_OBSERVER_CONFIG"]).read_text())
        Path(config_value["receipt_path"]).write_text(json.dumps({"complete":False,
            "failures":["lost_syscall_data"],"tracee_pid":None}))
        raise SystemExit(completed.returncode)
    compiler=lambda *_a:{};compiler.scope={}
    result=driver.execute_capture(frozen,lambda *_a:None,compiler,run_side=failed,
        inventory_collector=lambda *_a:_snapshot(),cleanup_runner=lambda *_a:reset.append(True))
    assert reset==[] and result["capture_complete"] is False and result["error_type"]=="SystemExit"
    assert [{k:row[k] for k in ("name","passed","status","error_type")} for row in result["preflight_checks"]]==[
        {"name":"objective-recognizable","passed":True,"status":"passed","error_type":None},
        {"name":"mythic-queries-valid","passed":False,"status":"failed","error_type":"PreflightCheckFailed"}]
    retained=tmp_path/"retained-result.json";driver._retain_driver_result(retained,result)
    assert "PLANTED-PREFLIGHT-OUTPUT-SECRET" not in retained.read_text()


@pytest.mark.parametrize("payload",[
    {"event":"m3_preflight_check","schema":"sage-m3-preflight-check-v1","name":"unknown-check",
     "passed":True,"status":"passed","error_type":None},
    {"event":"m3_preflight_check","schema":"sage-m3-preflight-check-v1","name":"objective-recognizable",
     "passed":False,"status":"passed","error_type":"PreflightCheckFailed"}])
def test_unknown_or_malformed_preflight_event_is_not_accepted(payload):
    handler=driver._CaptureHandler(lambda _event:{})
    with pytest.raises(driver.CaptureEpisodeError,match="preflight check observation"):
        handler({"kind":"tool_event","payload":payload,"pid":1,"process_uuid":"a"*32,
                 "seq":1,"event_id":"b"*32})
    assert handler.preflight_checks==[]

def test_live_composite_destination_is_required_before_resource_allocation(monkeypatch,tmp_path):
    frozen,config,ledger,_receiver,source,approved=_frozen(tmp_path)
    monkeypatch.setattr(driver,"_validate",lambda *_a,**_k:{"approved":approved,"source_binding":source,
        "config":config,"ledger":ledger,"approval":{},"execution":{"generation":frozen.value["model"]}})
    monkeypatch.setattr(driver,"_validated_route_env",lambda _execution:{})
    compiler=lambda *_a:{};compiler.scope={"live_fact_contract":{}}
    with pytest.raises(driver.CaptureEpisodeError,match="composite evidence directory"):
        driver.execute_capture(frozen,lambda *_a:None,compiler)
    assert not ledger.exists()


def test_capture_recovery_is_charged_before_receiver_setup_and_activation_does_not_double(monkeypatch, tmp_path):
    from ai import resource_budget
    from ai.resource_budget import ResourceLedger
    from tests.test_resource_recovery_amendment import _capture_disposition, _continuation
    account = tmp_path / "account"; account.mkdir()
    budget, _approval, clock = _continuation(account)
    budget.register_episode("prior", action_seconds=60, settlement_seconds=10,
        provider_token_limit=100, per_call_token_reservation=10, payload_task_limit=5)
    budget.activate_episode("prior"); budget.close_episode("prior")
    prior_charges = budget.status()["effective_resource_charges"]
    disposition = _capture_disposition(account, "prior")
    driver_tree = tmp_path / "driver"; driver_tree.mkdir()
    frozen, _config, _ledger, _receiver, source, approved = _frozen(driver_tree)
    monkeypatch.setattr(driver, "_validate", lambda *_a, **_k: {
        "approved": approved, "source_binding": source, "config": budget.config_path,
        "ledger": budget.ledger_path, "approval": {}, "execution": {"generation": frozen.value["model"]},
        "capture_recovery": {"prior_disposition": {"path": disposition,
            "sha256": driver._digest(disposition.read_bytes())}}})
    monkeypatch.setattr(driver, "_validated_route_env", lambda _execution: {})
    real_ledger = ResourceLedger
    monkeypatch.setattr(resource_budget, "ResourceLedger",
        lambda config, ledger: real_ledger(config, ledger, now=lambda: clock[0]))
    real_module = driver._module; effects = []
    class FailingReceiver:
        def __init__(self, *_a, **_k): pass
        def __enter__(self):
            effects.append("receiver_setup")
            opened = real_ledger(budget.config_path, budget.ledger_path, now=lambda: clock[0])
            before = opened.status()["effective_resource_charges"]
            opened.activate_episode("episode-1")
            after = opened.status()["effective_resource_charges"]
            assert before["episodes"] == after["episodes"] and before["burned_setup_retries"] == 1
            raise RuntimeError("synthetic setup failure")
        def __exit__(self, *_a): pass
    monkeypatch.setattr(driver, "_module", lambda name: type("Receiver", (), {"ObservationReceiver": FailingReceiver})
        if name == "observation_receiver" else real_module(name))
    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        driver.execute_capture(frozen, lambda *_a: None, lambda *_a: None)
    status = budget.status(); episode = status["episodes"]["episode-1"]
    assert effects == ["receiver_setup"] and episode["closed_epoch"] is not None
    assert episode["activated_epoch"] is not None
    assert status["effective_resource_charges"]["episodes"] == prior_charges["episodes"] + 1
    assert status["effective_resource_charges"]["burned_setup_retries"] == \
        prior_charges["burned_setup_retries"] + 1


@pytest.mark.parametrize("case", ["malformed", "missing", "hash-changed"])
def test_capture_recovery_binding_fails_before_setup_effects(monkeypatch, tmp_path, case):
    frozen, config, ledger, _receiver, source, approved = _frozen(tmp_path)
    frozen = _seal_frozen(frozen, config, source, approved, tmp_path)
    disposition = tmp_path / "prior-disposition.json"; disposition.write_text("{}")
    if case == "malformed": prior = {"path": str(disposition)}
    elif case == "missing":
        disposition.unlink(); prior = {"path": str(disposition), "sha256": "0" * 64}
    else:
        frozen = _bind_capture_recovery(frozen, source, disposition)
        disposition.write_text('{"changed":true}')
        prior = None
    if prior is not None: frozen = _bind_capture_recovery(frozen, source, disposition, prior=prior)
    monkeypatch.setattr(driver, "_module", lambda _name: (_ for _ in ()).throw(AssertionError("setup effect")))
    with pytest.raises(driver.CaptureEpisodeError):
        driver.execute_capture(frozen, lambda *_a: None, lambda *_a: None)
    assert not ledger.exists()


def test_capture_recovery_binding_resolves_verified_prior_disposition(tmp_path):
    frozen, config, _ledger, _receiver, source, approved = _frozen(tmp_path)
    frozen = _seal_frozen(frozen, config, source, approved, tmp_path)
    disposition = tmp_path / "prior-disposition.json"; disposition.write_text("{}")
    frozen = _bind_capture_recovery(frozen, source, disposition)
    validated = driver._validate(frozen, live=True)
    assert validated["capture_recovery"] == {"prior_disposition": {
        "path": disposition.resolve(), "sha256": driver._digest(disposition.read_bytes())}}


def test_cli_loads_bound_hooks_and_retains_only_result_metadata(monkeypatch, tmp_path, capsys):
    frozen, _config, _ledger, _receiver_path, _source, _approved = _frozen(tmp_path)
    operator = tmp_path / "operator.env"; operator.write_text("VALUE=synthetic-secret\n")
    evidence = tmp_path / "evidence"; evidence.mkdir(mode=0o700)
    composite=tmp_path/"composite";composite.mkdir(mode=0o700)
    compiler = tmp_path / "compiler.json"; compiler.write_text(json.dumps({
        "schema": "sage-m3-capture-compiler-binding-v1",
        "scope": {"resource_config_path": frozen.value["resources"]["config"],
                  "resource_ledger_path": frozen.value["resources"]["ledger"],
                  "resource_episode_id": "episode-1","live_fact_contract":{},
                  "composite_evidence_directory":str(composite)},
        "static_documents": [], "config_paths": [], "artifact_paths": [], "operator_origins": [],
        "evidence_directory": str(evidence)}))
    frozen.value["observation"]["operator_inputs"] = [{
        "path": str(operator), "sha256": driver._digest(operator.read_bytes()), "format": "dotenv",
        "fields": [{"field": "VALUE", "secret_handle": "value"}]}]
    frozen.value["observation"]["compiler_binding"] = {
        "path": str(compiler), "sha256": driver._digest(compiler.read_bytes())}
    spec_path = tmp_path / "spec.json"; spec_path.write_text(json.dumps(frozen.value)); digest = driver._digest(spec_path.read_bytes())
    monkeypatch.setattr(driver, "_validate", lambda *_a, **_k: {})
    monkeypatch.setattr(driver, "_require_recovery_amendment", lambda *_a, **_k: None)
    assert driver.main(["--spec", str(spec_path), "--spec-sha256", digest]) == 0
    def execute(_spec, secret_factory, compiler_factory):
        assert callable(secret_factory) and callable(compiler_factory)
        Path(_spec.value["observation"]["capture_results_path"]).write_text(
            json.dumps({"record_type": "capture_result"}) + "\n")
        return {"schema": "sage-m3-capture-driver-result-v1", "episode_id": "episode-1",
                "capture_complete": False, "error_type": "SyntheticStop"}
    monkeypatch.setattr(driver, "execute_capture", execute)
    assert driver.main(["--spec", str(spec_path), "--spec-sha256", digest, "--go"]) == 0
    measured = Path(frozen.value["observation"]["capture_results_path"])
    retained = Path(frozen.value["observation"]["driver_result_path"])
    assert json.loads(measured.read_text())["record_type"] == "capture_result"
    assert retained.exists() and retained != measured and "synthetic-secret" not in retained.read_text()
    assert json.loads(retained.read_text())["schema"] == "sage-m3-capture-driver-result-v1"
    assert oct(retained.stat().st_mode & 0o777) == "0o600"
    assert "synthetic-secret" not in capsys.readouterr().out


def test_cli_service_readiness_plan_validates_without_execution(monkeypatch, tmp_path, capsys):
    config = tmp_path / "service.json"; config.write_text("{}")
    observed = []
    module = type("Service", (), {"SCHEMA": "service-schema",
        "load": staticmethod(lambda path, digest: observed.append(("load", Path(path), digest)) or object()),
        "validate": staticmethod(lambda frozen, live: observed.append(("validate", frozen, live)) or {
            "probe_id": "service-001"}),
        "execute": staticmethod(lambda *_a: (_ for _ in ()).throw(AssertionError("must not execute")))})
    monkeypatch.setattr(driver, "_module", lambda name: module if name == "capture_service_probe" else None)
    assert driver.main(["--service-readiness", str(config),
                        "--service-readiness-sha256", "a" * 64]) == 0
    assert [item[0] for item in observed] == ["load", "validate"] and observed[-1][-1] is False
    assert json.loads(capsys.readouterr().out)["action"] == "service-readiness"


@pytest.mark.parametrize("inner_present", [True, False])
def test_service_main_retains_outer_disposition_after_single_c_reservation(
        monkeypatch, tmp_path, capsys, inner_present):
    from ai import resource_budget
    service_module = driver._module("capture_service_probe")
    result_path = tmp_path / "service-result.json"; inner_path = result_path.with_suffix(".inner.json")
    isolation = tmp_path / "isolation.json"; isolation.write_text("{}")
    calls = []
    stage = {"stage": "C", "attempt": 1, "started_epoch": 1,
        "active_deadline_epoch": 2, "total_deadline_epoch": 3,
        "active_ended_epoch": None, "closed_epoch": None}
    class Ledger:
        def start_diagnostic_stage(self, probe, label, retry): calls.append((probe, label, retry)); return stage
        def status(self): return {"service_probes": {"probe": {"stages": [stage]}}}
    ledger = Ledger(); monkeypatch.setattr(resource_budget, "ResourceLedger", lambda *_a: ledger)
    frozen = object(); validated = {"config": tmp_path / "config", "ledger": tmp_path / "ledger",
        "probe_id": "probe", "result_path": result_path,
        "amendment": {"schema": "sage-resource-recovery-amendment-v4"}}
    def execute(_frozen, actual, _ledger, **kwargs):
        assert kwargs["diagnostic_stage"] == stage and actual["result_path"] == result_path
        if inner_present:
            inner_path.write_text(json.dumps({"service_ready": True}))
            digest = driver._digest(inner_path.read_bytes())
        else: digest = None
        return {"schema": "sage-m3-capture-service-result-v1", "probe_id": "probe",
            "service_ready": False, "error_type": "ServiceIsolationError",
            "cleanup": {"complete": False, "steps": []}, "model_requests": 0,
            "inner_result_retained": inner_present, "inner_result_sha256": digest,
            "inner_result": ({"path": str(inner_path), "sha256": digest} if inner_present else None),
            "isolation_receipt": {"path": str(isolation), "sha256": driver._digest(isolation.read_bytes())}}
    module = type("Service", (), {"load": staticmethod(lambda *_a: frozen),
        "validate": staticmethod(lambda *_a, **_k: validated), "execute": staticmethod(execute),
        "inner_result_path": staticmethod(lambda path: path.with_suffix(".inner.json")),
        "retain": staticmethod(service_module.retain), "_local_sage_pids": staticmethod(lambda: set()),
        "_listener_owners": staticmethod(lambda _port: set())})
    monkeypatch.setattr(driver, "_module", lambda name: module if name == "capture_service_probe" else None)
    assert driver.main(["--service-readiness", str(tmp_path / "spec"),
                        "--service-readiness-sha256", "a" * 64, "--go"]) == 0
    assert calls == [("probe", "C", None)]
    retained = json.loads(result_path.read_text())
    assert retained["service_ready"] is False and retained["inner_result_retained"] is inner_present
    if inner_present: assert json.loads(inner_path.read_text())["service_ready"] is True
    summary = json.loads(capsys.readouterr().out); assert summary["service_ready"] is False
    assert summary["result_sha256"] == driver._digest(result_path.read_bytes())


def test_live_bare_fails_before_resource_allocation(monkeypatch, tmp_path):
    frozen, config, _ledger, _receiver_path, source, approved = _frozen(tmp_path)
    frozen.value["identity"]["side"] = "bare"
    frozen = _seal_frozen(frozen, config, source, approved, tmp_path)
    try:
        driver.execute_capture(frozen, lambda *_a: None, lambda *_a: {})
    except driver.CaptureEpisodeError as exc:
        assert "source-bound OS observer" in str(exc)
    else:
        assert False
    assert not Path(frozen.value["resources"]["ledger"]).exists()


def test_generated_observer_config_loads_through_actual_boundary(monkeypatch, tmp_path):
    frozen, _config, _ledger, receiver_path, source, _approved = _frozen(tmp_path)
    target = tmp_path / "bound.py"; target.write_text("# source-bound\n")
    source.write_text(json.dumps({"files": {str(target): driver._digest(target.read_bytes())}}))
    frozen.value["source_binding"]["sha256"] = driver._digest(source.read_bytes())
    buffer_mod = driver._module("capture_event_buffer"); receiver_mod = driver._module("observation_receiver")
    buffer = buffer_mod.CaptureEventBuffer(max_events=100, max_bytes=1000000)
    work = tmp_path / "work"; work.mkdir(mode=0o700)
    with receiver_mod.ObservationReceiver(receiver_path, buffer, temporary_parent=work) as receiver:
        config_path, config_sha, _receipt = driver._observer_config(
            frozen, {"source_binding": source}, receiver.directory, work
        )
        monkeypatch.setenv("SAGE_OBSERVATION_DIRECTORY", str(receiver.directory))
        process = driver._module("process_observer")
        config = json.loads(config_path.read_text())
        loaded = process.load_exec_config(config_path, config_sha, cwd=config["cwd"], python=config["python"])
    assert loaded["schema"] == "sage-process-observer-exec-config-v1"
    assert loaded["limits"]["max_line_bytes"] == 1_048_576
    assert loaded["limits"]["max_events"] == frozen.value["observation"]["max_events"]
    assert loaded["limits"]["max_trace_bytes"] == frozen.value["observation"]["max_trace_bytes"]


def test_pump_derives_start_from_transient_source_event_not_process_start(tmp_path):
    receiver_mod = driver._module("observation_receiver"); buffer_mod = driver._module("capture_event_buffer")
    buffer = buffer_mod.CaptureEventBuffer(max_events=100, max_bytes=1000000)
    handler = driver._CaptureHandler(buffer); work = tmp_path / "work"; work.mkdir(mode=0o700)
    with receiver_mod.ObservationReceiver(tmp_path / "records.jsonl", handler, temporary_parent=work) as receiver:
        pump = driver._Pump(receiver, handler); pump.start()
        process_start = """import os
from ai import observation_channel
observation_channel.emit('process_start', {'attempted':False})
"""
        env = {**os.environ, "PYTHONPATH": str(ROOT / "Payload_Type/sage"),
               "SAGE_OBSERVATION_DIRECTORY": str(receiver.directory)}
        subprocess = __import__("subprocess")
        completed = subprocess.run([str(ROOT / ".venv/bin/python"), "-c", process_start], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        assert completed.returncode == 0 and handler.observer_started is None
        stream_start = """import os
from ai import observation_channel
observation_channel.emit('tool_event', {'event':'observed_process_streams','tracee_pid':os.getpid(),'streams':[{'fd':1},{'fd':2}]})
"""
        process = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-c", stream_start], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert process.wait(timeout=5) == 0
        deadline = __import__("time").monotonic() + 2
        while handler.observer_started is None and __import__("time").monotonic() < deadline:
            __import__("time").sleep(.02)
        pump.finish()
    assert handler.tracee_pid == process.pid and buffer.freeze()["event_count"] == 2


def test_controlled_output_sink_hashes_without_exposing_content(capfd):
    subprocess = __import__("subprocess"); hashlib = __import__("hashlib")
    class Index:
        def redact_bytes(self, content):
            return {"redacted": content.replace(b"synthetic-output", b"<SAGE_REDACTED>"),
                    "metadata": {"matches": ([{"handle": "opaque"}] if b"synthetic-output" in content else [])}}
    with driver._BoundedOutputSink(1024) as sink:
        subprocess.run([str(ROOT / ".venv/bin/python"), "-c", "print('synthetic-output')"], check=True)
        sink.start_measurement()
        subprocess.run([str(ROOT / ".venv/bin/python"), "-c",
                        "import os,time;os.write(1,b'synthetic-');time.sleep(.05);os.write(1,b'output');os.write(2,b'synthetic-error')"], check=True)
        sink.end_measurement()
        subprocess.run([str(ROOT / ".venv/bin/python"), "-c", "print('synthetic-output')"], check=True)
    captured = capfd.readouterr(); receipt = sink.scan(Index())
    assert "synthetic" not in captured.out + captured.err and receipt["complete"] is True
    measured = receipt["streams"][0]["segments"]["measured"]
    assert measured["sha256"] == hashlib.sha256(b"synthetic-output").hexdigest()
    assert measured["opaque_matches"] == [{"handle": "opaque"}]
    assert receipt["streams"][0]["segments"]["pre"]["bytes"] > 0
    assert receipt["streams"][0]["segments"]["post"]["bytes"] > 0
    with driver._BoundedOutputSink(4) as bounded:
        bounded.start_measurement()
        subprocess.run([str(ROOT / ".venv/bin/python"), "-c", "import os;os.write(1,b'12345')"], check=True)
        bounded.end_measurement()
    assert bounded.scan(Index())["complete"] is False


def test_dynamic_module_loader_registers_actual_dataclass_module():
    module = driver._module("orchestrate")
    assert module.FootholdSpec.__module__ in sys.modules


def test_evidence_deadline_reuses_ledger_and_earlier_producer_window():
    import time
    limits = {"action_seconds": 2700, "settlement_seconds": 300}
    class Ledger:
        def status(self):
            return {"schema": "sage-shared-resource-budget-status-v1", "episodes": {"episode-1": {
                **limits, "activated_epoch": time.time() - 2998, "closed_epoch": None}}}
    deadline = driver._evidence_deadline(Ledger(), "episode-1", limits, time.monotonic() + .5)
    remaining = deadline - time.monotonic()
    assert 0 < remaining <= .5


def test_collect_native_installs_meter_before_login(monkeypatch):
    if str(driver.RUNTIME) not in sys.path: sys.path.insert(0, str(driver.RUNTIME))
    from ai import resource_clients
    from ai.hillclimb import live_seams
    order = []
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", "/bound/config")
    monkeypatch.setattr(resource_clients, "install", lambda: order.append("install"))
    class Client: current_operation_id = 4
    async def login(): order.append("login"); return Client()
    async def collect(*_args, **_kwargs): order.append("collect"); return _snapshot()
    monkeypatch.setattr(live_seams, "_mythic_login_async_safe", login)
    monkeypatch.setattr(driver, "_module", lambda _name: type("Inventory", (), {
        "collect_snapshot_with_configured_client": staticmethod(collect)}))
    assert __import__("asyncio").run(driver._collect_native()) == _snapshot()
    assert order == ["install", "login", "collect"]


def test_mutated_frozen_value_and_receiver_failure_fail_closed(monkeypatch, tmp_path):
    frozen, config, ledger, _receiver_path, source, approved = _frozen(tmp_path)
    frozen = _seal_frozen(frozen, config, source, approved, tmp_path)
    changed = driver.FrozenCaptureSpec(frozen.path, frozen.sha256, json.loads(json.dumps(frozen.value)))
    changed.value["identity"]["arm"] = "changed"
    try: driver._validate(changed, live=True)
    except driver.CaptureEpisodeError as exc: assert "changed in memory" in str(exc)
    else: assert False
    monkeypatch.setattr(driver, "_validate", lambda *_a, **_k: {
        "approved": approved, "source_binding": source, "config": config, "ledger": ledger, "approval": {},
        "execution": {"generation": frozen.value["model"]}})
    monkeypatch.setattr(driver, "_validated_route_env", lambda _execution: {})
    real_module = driver._module
    class FailingReceiver:
        def __init__(self, *_a, **_k): pass
        def __enter__(self): raise RuntimeError("receiver failed")
        def __exit__(self, *_a): pass
    monkeypatch.setattr(driver, "_module", lambda name: type("Receiver", (), {"ObservationReceiver": FailingReceiver})
                        if name == "observation_receiver" else real_module(name))
    try: driver.execute_capture(frozen, lambda *_a: None, lambda *_a: None)
    except RuntimeError: pass
    else: assert False
    from ai.resource_budget import ResourceLedger
    assert ResourceLedger(config, ledger).status()["episodes"]["episode-1"]["closed_epoch"] is not None


def test_execution_binding_rejects_model_range_route_and_budget_substitution(tmp_path):
    frozen, config, _ledger, _receiver_path, source, approved = _frozen(tmp_path)
    frozen = _seal_frozen(frozen, config, source, approved, tmp_path)
    mutations = (
        lambda value: value["model"].update(model_id="arbitrary-model"),
        lambda value: value["model"].update(route_endpoint="https://elsewhere.invalid"),
        lambda value: value["range"].update(range_id="OTHER"),
        lambda value: value["resources"]["episode"].update(provider_tokens=1_000_001),
    )
    for index, mutate in enumerate(mutations):
        value = json.loads(json.dumps(frozen.value)); mutate(value)
        path = tmp_path / f"substitute-{index}.json"; path.write_text(json.dumps(value))
        substitute = driver.FrozenCaptureSpec(path, driver._digest(path.read_bytes()), value)
        try: driver._validate(substitute, live=True)
        except driver.CaptureEpisodeError: pass
        else: assert False


def test_configured_route_must_match_bound_generation(monkeypatch):
    generation = {"provider": "openai", "model_id": "model", "route_endpoint": "https://bound",
                  "temperature": 0}
    module = type("Orchestrate", (), {"_load_sage_model_environment": staticmethod(lambda: {
        "provider": "openai", "model": "other", "API_ENDPOINT": "https://bound", "API_KEY": "secret"})})
    monkeypatch.setattr(driver, "_module", lambda _name: module)
    try: driver._validated_route_env({"generation": generation})
    except driver.CaptureEpisodeError as exc: assert "configured provider route" in str(exc)
    else: assert False
    module._load_sage_model_environment = staticmethod(lambda: {
        "provider": "openai", "model": "model", "API_ENDPOINT": "https://bound", "API_KEY": "synthetic"})
    route = driver._validated_route_env({"generation": generation})
    assert route["SAGE_FINDINGS_SLACK_WEBHOOK_URL"] == "" \
        and route["SAGE_FINDINGS_SLACK_CHANNEL_ID"] == "" and route["SAGE_EVAL_TEMPERATURE"] == "0"

def test_engagement_snapshot_allows_initial_absence_but_rejects_corrupt_final(tmp_path):
    assert driver._strict_engagement_snapshot(tmp_path,"engagement",allow_missing=True)=={
        "engagement_id":"engagement","hops":[],"graph_facts":[]}
    path=tmp_path/"state_engagement.json";path.write_text('{"engagement_id":"engagement","hops":[]}')
    with pytest.raises(driver.CaptureEpisodeError,match="invalid"):
        driver._strict_engagement_snapshot(tmp_path,"engagement",allow_missing=False)
    path.write_text(json.dumps({"engagement_id":"engagement","hops":[],"graph_facts":[]}))
    assert driver._strict_engagement_snapshot(tmp_path,"engagement",allow_missing=False)["graph_facts"]==[]
