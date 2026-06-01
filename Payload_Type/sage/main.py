import mythic_container
from mythic_container.logging import logger
import container # DO NOT REMOVE THIS IMPORT - This is needed to register the Payload Type with Mythic
import os
import phoenix as px
from phoenix.otel import register
from openinference.instrumentation.langchain import LangChainInstrumentor

# Configure SSL certificate bundle if present
# Combines system CA certs with any custom certs so both public APIs and internal services work
import ssl

cert_bundle_path = os.path.join(os.path.dirname(__file__), "certs", "bundle.pem")
if os.path.exists(cert_bundle_path):
    system_ca_path = ssl.get_default_verify_paths().openssl_cafile
    combined = ""
    if system_ca_path and os.path.exists(system_ca_path):
        with open(system_ca_path, "r") as f:
            combined = f.read()
        if not combined.endswith("\n"):
            combined += "\n"
    with open(cert_bundle_path, "r") as f:
        combined += f.read()
    combined_path = os.path.join(os.path.dirname(__file__), "certs", "combined-bundle.pem")
    with open(combined_path, "w") as f:
        f.write(combined)
    os.environ["SSL_CERT_FILE"] = combined_path
    logger.info(f"Using combined SSL certificate bundle (system CAs + custom certs): {combined_path}")
else:
    logger.info(f"No custom SSL certificate bundle found at {cert_bundle_path}, using system defaults")

os.environ["PHOENIX_WORKING_DIR"] = "./.phoenix"

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