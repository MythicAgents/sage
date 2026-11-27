"""Child process for ``test_phoenix_auth.py``: Sage's real Phoenix startup, then probes of what an operator sees.

Runs ``main.py`` as Sage does, with only the parts outside this boundary replaced: the Mythic service loop (it would
connect to RabbitMQ) and the native-chat registration. Phoenix, its secrets, the exporter and the default-password
check are the installed code. Ports, host and working directory come from the environment the test sets. Prints one
JSON line of observations and never prints a secret.
"""

from __future__ import annotations

import asyncio
import json
import logging
import runpy
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace


def main() -> None:
    sage_root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(sage_root))

    dotenv = ModuleType("dotenv_bootstrap")
    dotenv.load_sage_dotenv = lambda: ()
    sys.modules["dotenv_bootstrap"] = dotenv
    mythic = ModuleType("mythic_container")
    mythic.mythic_service = SimpleNamespace(start_and_run_forever=lambda: None)
    sys.modules["mythic_container"] = mythic
    mythic_logging = ModuleType("mythic_container.logging")
    mythic_logging.logger = logging.getLogger("phoenix-auth-child")
    sys.modules["mythic_container.logging"] = mythic_logging
    sys.modules["sage_chat"] = ModuleType("sage_chat")

    import os

    import httpx

    main_globals = runpy.run_path(str(sage_root / "main.py"), run_name="__main__")
    tracer_provider = main_globals["tracer_provider"]
    base = f"http://127.0.0.1:{os.environ['PHOENIX_PORT']}"
    projects_query = {"query": "{ projects { edges { node { name } } } }"}
    observations: dict[str, object] = {}

    with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
        for _ in range(100):
            try:
                if client.get("/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        observations["healthz"] = client.get("/healthz").status_code
        observations["graphql_unauthenticated"] = client.post("/graphql", json=projects_query).status_code

        with tracer_provider.get_tracer("phoenix-auth-child").start_as_current_span("sage-auth-probe-span"):
            pass
        tracer_provider.force_flush()

        admin = {"Authorization": f"Bearer {os.environ['PHOENIX_ADMIN_SECRET']}"}
        observations["graphql_admin_secret"] = client.post("/graphql", json=projects_query, headers=admin).status_code
        span_names: list[str] = []
        for _ in range(50):
            response = client.get("/v1/projects/Sage/spans", headers=admin)
            if response.status_code == 200:
                span_names = [span.get("name") for span in response.json().get("data", [])]
                if "sage-auth-probe-span" in span_names:
                    break
            time.sleep(0.2)
        observations["sage_span_arrived"] = "sage-auth-probe-span" in span_names

    from ai import phoenix_auth

    observations["notice_first"] = asyncio.run(phoenix_auth.default_password_notice())
    observations["notice_second"] = asyncio.run(phoenix_auth.default_password_notice())

    with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
        login = client.post(
            "/auth/login",
            json={"email": phoenix_auth.DEFAULT_ADMIN_EMAIL, "password": phoenix_auth.SHIPPED_DEFAULT_ADMIN_PASSWORD},
        )
        observations["operator_login"] = login.status_code
        change = client.post("/graphql", json={
            "query": "mutation($p: String!, $c: String!) { patchViewer(input: {newPassword: $p, currentPassword: $c})"
                     " { user { email } } }",
            "variables": {"p": "operator-chosen-password-42", "c": phoenix_auth.SHIPPED_DEFAULT_ADMIN_PASSWORD},
        })
        observations["password_change"] = change.status_code
        observations["password_change_errors"] = bool(change.json().get("errors"))

    observations["accepted_after_change"] = asyncio.run(phoenix_auth.default_password_accepted())
    print("PHOENIX_AUTH_OBSERVATIONS " + json.dumps(observations), flush=True)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
