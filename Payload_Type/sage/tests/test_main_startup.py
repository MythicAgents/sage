from __future__ import annotations

import logging
from pathlib import Path
import runpy
import shutil
import sys
from types import ModuleType, SimpleNamespace

import pytest


SAGE_ROOT = Path(__file__).resolve().parents[1]


class _PhoenixReached(RuntimeError):
    pass


def test_main_creates_absolute_phoenix_working_dir_before_launch(monkeypatch, tmp_path):
    runtime_root = tmp_path / "sage-runtime"
    runtime_root.mkdir()
    runtime_main = runtime_root / "main.py"
    shutil.copy2(SAGE_ROOT / "main.py", runtime_main)
    expected = runtime_root / ".phoenix"

    dotenv = ModuleType("dotenv_bootstrap")
    dotenv.load_sage_dotenv = lambda: ()
    monkeypatch.setitem(sys.modules, "dotenv_bootstrap", dotenv)

    mythic = ModuleType("mythic_container")
    mythic.mythic_service = SimpleNamespace(start_and_run_forever=lambda: None)
    monkeypatch.setitem(sys.modules, "mythic_container", mythic)
    logging_module = ModuleType("mythic_container.logging")
    logging_module.logger = logging.getLogger("test-main-startup")
    monkeypatch.setitem(sys.modules, "mythic_container.logging", logging_module)
    monkeypatch.setitem(sys.modules, "sage_chat", ModuleType("sage_chat"))

    phoenix = ModuleType("phoenix")

    def launch_app(*, use_temp_dir):
        assert use_temp_dir is False
        configured = Path(__import__("os").environ["PHOENIX_WORKING_DIR"])
        assert configured == expected
        assert configured.is_absolute()
        assert configured.is_dir()
        raise _PhoenixReached

    phoenix.launch_app = launch_app
    monkeypatch.setitem(sys.modules, "phoenix", phoenix)
    phoenix_otel = ModuleType("phoenix.otel")
    phoenix_otel.register = lambda **_kwargs: object()
    monkeypatch.setitem(sys.modules, "phoenix.otel", phoenix_otel)
    instrumentation = ModuleType("openinference.instrumentation.langchain")
    instrumentation.LangChainInstrumentor = lambda: SimpleNamespace(
        instrument=lambda **_kwargs: None
    )
    monkeypatch.setitem(sys.modules, "openinference", ModuleType("openinference"))
    monkeypatch.setitem(
        sys.modules, "openinference.instrumentation", ModuleType("openinference.instrumentation")
    )
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.langchain", instrumentation)
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    try:
        with pytest.raises(_PhoenixReached):
            runpy.run_path(str(runtime_main), run_name="__main__")
    finally:
        if expected.exists():
            expected.rmdir()
