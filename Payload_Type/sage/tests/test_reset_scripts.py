import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MYTHIC_RESET = ROOT / "skills" / "sage-goad-reset" / "scripts" / "mythic_reset.sh"


def test_mythic_reset_drops_container_only_host_overrides(tmp_path):
    log = tmp_path / "calls.log"
    cli = tmp_path / "mythic-cli"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s|%s|%s|%s\\n" "$*" "${RABBITMQ_HOST-unset}" '
        '"${MYTHIC_SERVER_HOST-unset}" "${NGINX_HOST-unset}" >> "$CALL_LOG"\n'
    )
    cli.chmod(0o755)
    env = {
        **os.environ,
        "MYTHIC_CLI": str(cli),
        "CALL_LOG": str(log),
        "RABBITMQ_HOST": "127.0.0.1",
        "MYTHIC_SERVER_HOST": "127.0.0.1",
        "NGINX_HOST": "127.0.0.1",
    }

    subprocess.run(
        ["/bin/bash", str(MYTHIC_RESET), "--yes"],
        cwd=ROOT,
        env=env,
        check=True,
    )

    calls = log.read_text().splitlines()
    assert calls == [
        "stop|unset|unset|unset",
        "database reset -f|unset|unset|unset",
        "start|unset|unset|unset",
    ]
