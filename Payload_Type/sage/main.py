import os
from pathlib import Path

# MUST run before importing mythic_container, which builds its Dynaconf settings at import time and
# would not see anything loaded afterwards. Under Mythic this file lives in the bind-mounted service
# directory, so an operator edits it from the web UI container file browser — no shell, no sudo.
# Precedence and empty-value rules live in the module. See README "Configuration load order".
from dotenv_bootstrap import load_sage_dotenv
from ssl_bootstrap import configure_sage_ssl_cert_file

_dotenv_applied = load_sage_dotenv()

import mythic_container
from mythic_container.logging import logger
import sage_chat # Registers SageChat (native v4.0.0 chat container) via Chat.__subclasses__().
                 # Phase 4 complete: the PayloadType (`container`) has been removed — Sage is chat-only.
import phoenix as px
from phoenix.otel import register
from openinference.instrumentation.langchain import LangChainInstrumentor

# Names only — never values. Silence here means either no .env, or every setting already came from
# the container environment, which is the normal Mythic case.
if _dotenv_applied:
    logger.info(f"Loaded {len(_dotenv_applied)} setting(s) from .env: {sorted(_dotenv_applied)}")

certs_dir = Path(__file__).resolve().parent / "certs"
ssl_cert_file = configure_sage_ssl_cert_file(certs_dir=certs_dir)
if ssl_cert_file:
    logger.info(f"Using configured SSL certificate bundle: {ssl_cert_file}")
else:
    logger.info(f"No custom SSL certificate bundle found at {certs_dir / 'bundle.pem'}, using system defaults")

_phoenix_working_dir = Path(__file__).resolve().parent / ".phoenix"
_phoenix_working_dir.mkdir(parents=True, exist_ok=True)
os.environ["PHOENIX_WORKING_DIR"] = str(_phoenix_working_dir)

# Cap span attribute value size BEFORE the tracer provider is built (register() reads
# OTel SpanLimits from env at SDK init). With register(batch=False) each span exports
# individually over OTLP gRPC, and the OpenInference LangChain instrumentor stores the FULL
# prompt/output in span attributes. On heavy turns a single prompt is 168K+ tokens (~1MB+),
# which exceeds gRPC's default 4MB receive limit → the exporter fails with RESOURCE_EXHAUSTED
# and the span is DROPPED (we lose visibility on exactly the heaviest steps). Truncating each
# attribute value to 128KB keeps every span well under the gRPC limit so traces keep exporting
# during a context blowup — at the cost of truncated (not dropped) large inputs/outputs.
os.environ.setdefault("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "131072")  # 128 KB per attribute value

px.launch_app(use_temp_dir=False)

# Initialize global LangChain instrumentation for Phoenix tracing
tracer_provider = register(
    project_name="Sage",
    auto_instrument=False,  # We manually instrument LangChain below
    batch=False,
)
LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

mythic_container.mythic_service.start_and_run_forever()
