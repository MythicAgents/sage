import mythic_container
from mythic_container.logging import logger
import container # DO NOT REMOVE THIS IMPORT - This is needed to register the Payload Type with Mythic
import os
import phoenix as px
from phoenix.otel import register
from openinference.instrumentation.langchain import LangChainInstrumentor

# Configure SSL certificate bundle if present
cert_bundle_path = os.path.join(os.path.dirname(__file__), "certs", "bundle.pem")
if os.path.exists(cert_bundle_path):
    os.environ["SSL_CERT_FILE"] = cert_bundle_path
    logger.info(f"Using custom SSL certificate bundle: {cert_bundle_path}")
else:
    logger.info(f"No custom SSL certificate bundle found at {cert_bundle_path}, using system defaults")

os.environ["PHOENIX_WORKING_DIR"] = "./.phoenix"

px.launch_app(use_temp_dir=False)

# Initialize global LangChain instrumentation for Phoenix tracing
tracer_provider = register(
    project_name="Sage",
    auto_instrument=False,  # We manually instrument LangChain below
    batch=False,
)
LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

mythic_container.mythic_service.start_and_run_forever()