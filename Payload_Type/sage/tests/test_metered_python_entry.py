import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from ai import resource_budget


ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / "skills/sage-eval-gauge/scripts/run_metered_python.py"
BUILDER = ROOT / "skills/sage-eval-gauge/scripts/prepare_metered_launcher.py"
RECEIVER_SPEC = importlib.util.spec_from_file_location(
    "metered_launcher_receiver", ROOT / "skills/sage-eval-gauge/scripts/observation_receiver.py"
)
receiver_module = importlib.util.module_from_spec(RECEIVER_SPEC)
assert RECEIVER_SPEC and RECEIVER_SPEC.loader
RECEIVER_SPEC.loader.exec_module(receiver_module)


def _budget(tmp_path):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {"control_requests": {"limit": 4, "cleanup_reserve": 0}},
        "calendar_seconds": 100,
        "cleanup_seconds": 10,
    }))
    return config, ledger


def _run(entry, digest, config, ledger, *arguments, cwd=None, episode=None, env=None):
    command = [
        sys.executable, str(ENTRY), "--source-root", str(ROOT), "--entry", str(entry),
        "--entry-sha256", digest, "--budget-config", str(config), "--budget-ledger", str(ledger),
    ]
    if episode is not None:
        command.extend(["--episode-id", episode])
    command.extend(["--", *map(str, arguments)])
    return subprocess.run(command, cwd=cwd or ROOT, text=True, capture_output=True, timeout=20,
                          check=False, env=env)


def _serve_until_exit(receiver, process):
    receipts, deadline = [], time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        try:
            receipts.append(receiver.serve_one(0.5))
        except receiver_module.ObservationReceiverTimeout:
            pass
    assert process.poll() is not None
    return receipts


def _episode_budget(tmp_path):
    config, ledger = tmp_path / "episode-config.json", tmp_path / "episode-ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {
            "episodes": {"limit": 2, "cleanup_reserve": 0},
            "control_requests": {"limit": 10, "cleanup_reserve": 2},
            "provider_requests": {"limit": 4, "cleanup_reserve": 0},
            "provider_tokens": {"limit": 200, "cleanup_reserve": 0},
            "payload_task_attempts": {"limit": 4, "cleanup_reserve": 0},
        },
        "calendar_seconds": 100, "cleanup_seconds": 10,
    }))
    operator = resource_budget.ResourceLedger(config, ledger)
    operator.register_episode("ep", action_seconds=50, settlement_seconds=20,
                              provider_token_limit=100, per_call_token_reservation=50,
                              payload_task_limit=2)
    return config, ledger, operator


def test_metered_entry_preserves_args_cwd_and_charges_actual_requests_boundary(tmp_path):
    config, ledger = _budget(tmp_path)
    output, cwd = tmp_path / "output.json", tmp_path / "caller-cwd"
    cwd.mkdir()
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        "import json,os,sys,requests\n"
        "from ai import resource_clients\n"
        "resource_clients._ORIGINAL_REQUESTS_SEND=lambda self,request,**kwargs:'returned'\n"
        "result=requests.adapters.HTTPAdapter(max_retries=0).send('synthetic-request')\n"
        "open(sys.argv[1],'w').write(json.dumps({'argv':sys.argv[2:],'cwd':os.getcwd(),"
        "'active':requests.adapters.HTTPAdapter.send is resource_clients._requests_send,'result':result}))\n"
    )
    original = fixture.read_bytes()
    result = _run(fixture, hashlib.sha256(original).hexdigest(), config, ledger, output, "one", "two", cwd=cwd)
    assert result.returncode == 0
    assert json.loads(output.read_text()) == {
        "argv": ["one", "two"], "cwd": str(cwd), "active": True, "result": "returned",
    }
    assert resource_budget.ResourceLedger(config, ledger).status()["effective_resource_charges"] == {
        "control_requests": 1
    }
    assert fixture.read_bytes() == original


def test_hash_config_and_missing_entry_refuse_before_fixture_executes(tmp_path):
    config, ledger = _budget(tmp_path)
    marker, fixture = tmp_path / "marker", tmp_path / "fixture.py"
    fixture.write_text(f"open({str(marker)!r},'w').write('executed')\n")
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    wrong_hash = _run(fixture, "0" * 64, config, ledger)
    assert wrong_hash.returncode == 2
    assert not marker.exists()

    bad_config = tmp_path / "bad-config.json"
    bad_config.write_text("{}")
    bad = _run(fixture, digest, bad_config, ledger)
    assert bad.returncode == 2
    assert not marker.exists()
    missing = _run(tmp_path / "missing.py", digest, config, ledger)
    assert missing.returncode == 2
    assert not marker.exists()


def test_entry_system_exit_is_preserved_and_exception_does_not_log_sentinel(tmp_path):
    config, ledger = _budget(tmp_path)
    exiting = tmp_path / "exit.py"
    exiting.write_text("raise SystemExit(7)\n")
    result = _run(exiting, hashlib.sha256(exiting.read_bytes()).hexdigest(), config, ledger)
    assert result.returncode == 7

    sentinel = "SYNTHETIC_PRIVATE_SENTINEL"
    failing = tmp_path / "failure.py"
    failing.write_text(f"raise RuntimeError({sentinel!r})\n")
    result = _run(failing, hashlib.sha256(failing.read_bytes()).hexdigest(), config, ledger)
    assert result.returncode == 2
    assert sentinel not in result.stdout + result.stderr
    assert "metered entry failed" in result.stderr


def test_episode_scope_must_be_known_nonclosed_and_is_not_autoactivated(tmp_path):
    config, ledger, operator = _episode_budget(tmp_path)
    output, fixture = tmp_path / "status.json", tmp_path / "fixture.py"
    fixture.write_text(
        "import json,os\nfrom ai import resource_budget\n"
        f"open({str(output)!r},'w').write(json.dumps(resource_budget.configured_ledger().status()['episodes']['ep']))\n"
    )
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    result = _run(fixture, digest, config, ledger, episode="ep")
    assert result.returncode == 0
    assert json.loads(output.read_text())["activated_epoch"] is None

    output.unlink(); operator.activate_episode("ep")
    assert _run(fixture, digest, config, ledger, episode="ep").returncode == 0
    operator.close_episode("ep"); output.unlink()
    assert _run(fixture, digest, config, ledger, episode="ep").returncode == 2
    assert not output.exists()
    assert _run(fixture, digest, config, ledger, episode="missing").returncode == 2
    assert not output.exists()

    environment = os.environ.copy(); environment[resource_budget._PHASE_ENV] = "cleanup"
    assert _run(fixture, digest, config, ledger, episode="ep", env=environment).returncode == 2
    assert not output.exists()


def _build_launcher(config, ledger, external, entry, channel_directory, launcher, *, digest=None, source_root=ROOT,
                    client_families=()):
    command = [
        sys.executable, str(BUILDER), "--python", sys.executable, "--source-root", str(source_root),
        "--external-directory", str(external), "--entry", str(entry),
        "--entry-sha256", digest or hashlib.sha256(entry.read_bytes()).hexdigest(),
        "--budget-config", str(config), "--budget-ledger", str(ledger), "--episode-id", "ep",
        "--observation-directory", str(channel_directory), "--output", str(launcher),
    ]
    for family in client_families:
        command.extend(["--client-family", family])
    return subprocess.run(command, text=True, capture_output=True, timeout=20, check=False)


def test_temporary_launcher_accepts_only_canonical_bound_entry_and_preserves_credential_env(tmp_path):
    config, ledger, operator = _episode_budget(tmp_path)
    external = (tmp_path / "external").resolve(); external.mkdir()
    output, entry = tmp_path / "child.json", external / "main.py"
    entry.write_text(
        "import json,os,requests,sys,types\nfrom ai import resource_clients\n"
        "resource_clients._ORIGINAL_REQUESTS_SEND=lambda self,request,**kwargs:types.SimpleNamespace(status_code=200,content=b'ok')\n"
        "request=requests.Request('POST','https://synthetic.invalid/',data=b'x').prepare()\n"
        f"try:\n requests.adapters.HTTPAdapter(max_retries=0).send(request,stream=False)\n"
        f"except BaseException as error:\n open({str(output)!r},'w').write(type(error).__name__);raise\n"
        f"open({str(output)!r},'w').write(json.dumps(dict(argv=sys.argv,cwd=os.getcwd(),"
        "credential_seen=os.environ.get('BLOODHOUND_TOKEN_ID')=='synthetic-token')))\n"
    )
    original = entry.read_bytes()
    record = (tmp_path / "observations.jsonl").resolve()
    with receiver_module.ObservationReceiver(record, lambda event: {}, temporary_parent=tmp_path.resolve()) as receiver:
        launcher = (tmp_path / "metered-launcher.py").resolve()
        assert _build_launcher(config, ledger, external, entry, receiver.directory, launcher).returncode == 0
        assert oct(launcher.stat().st_mode & 0o777) == "0o700"
        assert "synthetic-token" not in launcher.read_text()
        environment = os.environ.copy(); environment["BLOODHOUND_TOKEN_ID"] = "synthetic-token"
        process = subprocess.Popen(
            [str(launcher), "--directory", str(external), "run", "main.py"], cwd=external,
            env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.2)
        assert process.poll() is None, process.communicate(timeout=2)
        receipts = _serve_until_exit(receiver, process)
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0 and stdout == stderr == ""
    assert json.loads(output.read_text()) == {
        "argv": [str(entry)], "cwd": str(external), "credential_seen": True,
    }
    kinds = [value["kind"] for value in receipts]
    assert kinds[0] == "process_start" and kinds[-1] == "process_end"
    assert kinds.index("control_request") < kinds.index("control_response")
    assert "tool_event" in kinds
    assert operator.status()["episodes"]["ep"]["effective_resource_charges"]["control_requests"] == 1
    assert entry.read_bytes() == original

    output.unlink()
    invalid = subprocess.run([str(launcher), "run", "main.py"], cwd=external, text=True,
                             capture_output=True, timeout=5, check=False, env=environment)
    assert invalid.returncode == 2 and not output.exists()
    entry.write_bytes(original + b"\n")
    changed = subprocess.run(
        [str(launcher), "--directory", str(external), "run", "main.py"], cwd=external,
        text=True, capture_output=True, timeout=5, check=False, env=environment,
    )
    assert changed.returncode == 2 and not output.exists()
    assert "synthetic-token" not in invalid.stdout + invalid.stderr + changed.stdout + changed.stderr


def test_launcher_builder_wrong_hash_and_existing_output_refuse(tmp_path):
    config, ledger, _operator = _episode_budget(tmp_path)
    external = (tmp_path / "external").resolve(); external.mkdir(); entry = external / "main.py"
    entry.write_text("raise RuntimeError('must not execute')\n")
    channel_directory = (tmp_path / "channel").resolve(); channel_directory.mkdir()
    launcher = (tmp_path / "launcher.py").resolve()
    assert _build_launcher(config, ledger, external, entry, channel_directory, launcher, digest="0" * 64).returncode == 2
    assert not launcher.exists()
    launcher.write_text("keep")
    assert _build_launcher(config, ledger, external, entry, channel_directory, launcher).returncode == 2
    assert launcher.read_text() == "keep"

    source_root = (tmp_path / "source").resolve()
    helper = source_root / "skills/sage-eval-gauge/scripts/run_metered_python.py"
    helper.parent.mkdir(parents=True); helper.write_bytes(ENTRY.read_bytes())
    launcher2 = (tmp_path / "bound-launcher.py").resolve()
    marker = tmp_path / "must-not-exist"
    entry.write_text(f"open({str(marker)!r},'w').write('bad')\n")
    assert _build_launcher(
        config, ledger, external, entry, channel_directory, launcher2, source_root=source_root,
    ).returncode == 0
    helper.write_text(f"open({str(marker)!r},'w').write('bad')\n")
    result = subprocess.run(
        [str(launcher2), "--directory", str(external), "run", "main.py"], cwd=external,
        text=True, capture_output=True, timeout=5, check=False,
    )
    assert result.returncode == 2 and not marker.exists()


def test_launcher_selected_requests_family_installs_audit_before_entry(tmp_path):
    config, ledger, operator = _episode_budget(tmp_path)
    external = (tmp_path / "external-selected").resolve(); external.mkdir()
    output, entry = tmp_path / "selected.json", external / "main.py"
    entry.write_text(
        "import json,requests\nfrom ai import resource_clients\n"
        "import types\nresource_clients._ORIGINAL_REQUESTS_SEND=lambda self,request,**kwargs:types.SimpleNamespace(status_code=200,content=b'ok')\n"
        "request=requests.Request('POST','https://synthetic.invalid/',data=b'x').prepare()\n"
        "result=requests.adapters.HTTPAdapter(max_retries=0).send(request,stream=False)\n"
        f"open({str(output)!r},'w').write(json.dumps(dict(status=result.status_code,"
        "requests_active=requests.adapters.HTTPAdapter.send is resource_clients._requests_send,"
        "aiohttp_active=resource_clients._aiohttp is not None and "
        "resource_clients._aiohttp.ClientSession._request is resource_clients._aiohttp_request)))\n"
    )
    record = (tmp_path / "selected-observations.jsonl").resolve()
    with receiver_module.ObservationReceiver(record, lambda event: {}, temporary_parent=tmp_path.resolve()) as receiver:
        launcher = (tmp_path / "selected-launcher.py").resolve()
        result = _build_launcher(
            config, ledger, external, entry, receiver.directory, launcher,
            client_families=("requests",),
        )
        assert result.returncode == 0, result.stderr
        process = subprocess.Popen(
            [str(launcher), "--directory", str(external), "run", "main.py"], cwd=external,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        receipts = _serve_until_exit(receiver, process)
        stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert json.loads(output.read_text()) == {
        "status": 200, "requests_active": True, "aiohttp_active": False,
    }
    kinds = [item["kind"] for item in receipts]
    assert kinds[0] == "process_start" and kinds[-1] == "process_end"
    assert kinds.index("control_request") < kinds.index("control_response")
    assert "tool_event" in kinds
