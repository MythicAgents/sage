import os
from pathlib import Path

# MUST run before importing mythic_container, which builds its Dynaconf settings at import time and
# would not see anything loaded afterwards. Under Mythic this file lives in the bind-mounted service
# directory, so an operator edits it from the web UI container file browser — no shell, no sudo.
# Precedence and empty-value rules live in the module. See README "Configuration load order".
from dotenv_bootstrap import load_sage_dotenv
from ssl_bootstrap import configure_sage_ssl_cert_file

_dotenv_applied = load_sage_dotenv()

# Passive measurement must be active before control clients or telemetry start.
_observation_capture = bool(os.environ.get("SAGE_OBSERVATION_DIRECTORY"))
if _observation_capture:
    from ai.observation_process import install as _install_observation_process
    _install_observation_process()

# Measurement is opt-in and installed before any control client starts.  Ordinary
# startup neither imports nor patches the measurement client hooks.
if os.environ.get("SAGE_RESOURCE_BUDGET_CONFIG") or os.environ.get("SAGE_RESOURCE_BUDGET_LEDGER"):
    from ai.resource_clients import install as _install_resource_clients
    from ai.resource_rpc import install as _install_resource_rpc
    _install_resource_clients()
    _install_resource_rpc()

import faulthandler
import signal

import mythic_container
from mythic_container.logging import logger

# `kill -USR1 <pid>` (or `docker kill -s USR1 sage`) writes every thread's Python stack to stderr, so a
# hung Phoenix server or a stuck request can be located from the container logs without a debugger.
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1, all_threads=True)

# Print a version banner first thing so the container's logs always state exactly which Sage is running:
# the declared version, the commit the image was built from (SAGE_GIT_COMMIT, "unknown" for a plain
# mythic-cli build), and a fingerprint of the live source tree. This is the deployed-version signal
# operators otherwise lack — it survives Mythic copying the source without usable git metadata, and
# logging both the baked build commit and the live source fingerprint makes a stale-source / stale-image
# mismatch visible at boot. Best-effort: a banner failure must never stop the service from starting.
try:
    from _version import announce_startup as _sage_announce_startup
    _sage_announce_startup()
except Exception as _banner_exc:  # pragma: no cover - banner is best-effort, never fatal
    logger.warning(f"Sage version banner unavailable: {_banner_exc}")

import sage_chat # Registers SageChat (native v4.0.0 chat container) via Chat.__subclasses__().
                 # The PayloadType (`container`) has been removed — Sage is chat-only.
if _observation_capture:
    from ai.observation_channel import emit as _emit_observation
    from sage_chat import findings_slack as _findings_slack
    _effective_notification_webhook = os.environ.get(
        _findings_slack.SLACK_FINDINGS_WEBHOOK_ENV, ""
    ).strip()
    _effective_notification_channel = os.environ.get(
        _findings_slack.SLACK_FINDINGS_CHANNEL_ID_ENV, ""
    ).strip()
    _emit_observation("config_snapshot", {
        "event": "effective_notification_config",
        "webhook_present": bool(_effective_notification_webhook),
        "channel_present": bool(_effective_notification_channel),
    })
import phoenix as px
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

# Nothing else opens sage.db until the first chat turn, so an unusable location would otherwise surface
# only as a bare "unable to open database file" in the channel. Fail here instead, naming the path and
# SAGE_DATABASE_PATH. The probe also creates the parent directory of a configured path.
from runtime_db import probe_database

logger.info(f"Sage database: {probe_database()}")

# Phoenix's SQLite DB defaults to the service directory, which Mythic bind-mounts, so it persists
# outside the container across restarts. Let an operator override the location, though: a deployment
# whose service dir is on a filesystem SQLite cannot lock (e.g. a macOS Docker Desktop / OrbStack bind
# mount, where SQLite fails with "unable to open database file") sets PHOENIX_WORKING_DIR to a writable
# path instead. This mirrors how sage.db honors SAGE_DATABASE_PATH. Existing non-empty environment
# wins; an empty value sets nothing, matching the project's .env precedence rules.
if not os.environ.get("PHOENIX_WORKING_DIR", "").strip():
    os.environ["PHOENIX_WORKING_DIR"] = str(Path(__file__).resolve().parent / ".phoenix")
Path(os.environ["PHOENIX_WORKING_DIR"]).mkdir(parents=True, exist_ok=True)

# Cap span attribute value size BEFORE the tracer provider is built (register() reads
# OTel SpanLimits from env at SDK init). With register(batch=False) each span exports
# individually over OTLP gRPC, and the OpenInference LangChain instrumentor stores the FULL
# prompt/output in span attributes. On heavy turns a single prompt is 168K+ tokens (~1MB+),
# which exceeds gRPC's default 4MB receive limit → the exporter fails with RESOURCE_EXHAUSTED
# and the span is DROPPED (we lose visibility on exactly the heaviest steps). Truncating each
# attribute value to 128KB keeps every span well under the gRPC limit so traces keep exporting
# during a context blowup — at the cost of truncated (not dropped) large inputs/outputs.
os.environ.setdefault("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "131072")  # 128 KB per attribute value

if _observation_capture:
    from ai.phoenix_process_compat import launch_capture_phoenix as _launch_capture_phoenix
    _phoenix_session = _launch_capture_phoenix(px)
    if _phoenix_session is None or _phoenix_session.active is not True:
        raise RuntimeError("capture Phoenix process did not become active")
else:
    px.launch_app(use_temp_dir=False)

# Initialize global LangChain instrumentation for Phoenix tracing.  Captured
# measurements construct the same Phoenix-selected transport, but every exported
# span crosses the metadata-only boundary first.
if _observation_capture:
    from ai.observation_tracing import create_provider as _create_observation_provider
    from openinference.instrumentation import TraceConfig as _TraceConfig
    from opentelemetry import trace as _otel_trace
    from phoenix.otel import SimpleSpanProcessor as _PhoenixSimpleSpanProcessor

    _phoenix_transport = _PhoenixSimpleSpanProcessor()
    tracer_provider = _create_observation_provider(_phoenix_transport.span_exporter)
    _otel_trace.set_tracer_provider(tracer_provider)
    _trace_config = _TraceConfig(
        hide_inputs=True,
        hide_outputs=True,
        hide_llm_invocation_parameters=True,
        hide_llm_tools=True,
        hide_input_messages=True,
        hide_output_messages=True,
        hide_input_images=True,
        hide_input_text=True,
        hide_output_text=True,
        hide_embedding_vectors=True,
        hide_embeddings_vectors=True,
        hide_embeddings_text=True,
        hide_prompts=True,
        hide_choices=True,
    )
    LangChainInstrumentor().instrument(tracer_provider=tracer_provider, config=_trace_config)
else:
    # Synchronous per-span export, kept out of the process-global provider so in-process Phoenix's own
    # request spans cannot deadlock its server. See ai/phoenix_tracing.py.
    from ai.phoenix_tracing import register_sage_tracing

    tracer_provider = register_sage_tracing()

# Logged at WARNING so it shows at Mythic's default log level: which span processors export Sage's spans,
# and whether Sage's provider is the process-global one that Phoenix's own server spans also use.
try:
    from opentelemetry import trace as _otel_trace_api

    _span_processors = getattr(getattr(tracer_provider, "_active_span_processor", None), "_span_processors", ())
    logger.warning(
        f"Phoenix tracing: span processors={[type(p).__name__ for p in _span_processors]}, "
        f"global provider={_otel_trace_api.get_tracer_provider() is tracer_provider}"
    )
except Exception as _tracing_log_exc:  # a diagnostic must never stop startup
    logger.warning(f"Phoenix tracing configuration unavailable: {_tracing_log_exc}")

mythic_container.mythic_service.start_and_run_forever()
