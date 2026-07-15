import ast
from pathlib import Path


SAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = [
    SAGE_ROOT / "ai" / "langgraph" / "model.py",
    SAGE_ROOT / "ai" / "langgraph" / "mythic_tools.py",
    SAGE_ROOT / "ai" / "langgraph" / "engagement_state.py",
    SAGE_ROOT / "ai" / "langgraph" / "capabilities.py",
    SAGE_ROOT / "ai" / "langgraph" / "graph_reconciler.py",
    SAGE_ROOT / "ai" / "langgraph" / "state_reconcile.py",
    SAGE_ROOT / "ai" / "langgraph" / "task_reconciler.py",
    SAGE_ROOT / "ai" / "langgraph" / "adcs_certificate_materializer.py",
]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_runtime_modules_do_not_import_target_protocol_clients():
    forbidden = {
        "socket",
        "ldap3",
        "impacket",
        "smbprotocol",
        "smbclient",
        "winrm",
        "pypsrp",
    }
    violations = {}
    for path in RUNTIME_FILES:
        hits = sorted(name for name in _imports(path) if name.split(".", 1)[0] in forbidden)
        if hits:
            violations[path.name] = hits
    assert violations == {}


def test_runtime_modules_do_not_import_evaluator_or_referee_packages():
    forbidden_fragments = ("hillclimb", "evals", "referee", "ground_truth")
    violations = {}
    for path in RUNTIME_FILES:
        hits = sorted(
            name
            for name in _imports(path)
            if any(fragment in name.casefold() for fragment in forbidden_fragments)
        )
        if hits:
            violations[path.name] = hits
    assert violations == {}


def test_ca_materializer_has_no_local_reconstruction_or_forge_path():
    source = (SAGE_ROOT / "ai" / "langgraph" / "adcs_certificate_materializer.py").read_text(encoding="utf-8")
    assert "_embedded_pfx_candidate_paths" not in source
    assert "CertificateBuilder" not in source
    assert "serialize_key_and_certificates" not in source
