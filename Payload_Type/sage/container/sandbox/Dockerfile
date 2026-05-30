# Sage sandbox image — minimal environment for isolated arbitrary code execution (Phase 1 T1.2).
#
# This image is intentionally bare: just shell + python3 and a non-root user. ALL of the
# isolation is applied by the CALLER (mythic_tools.sandbox_exec) at container-create time:
# no privileges, all caps dropped, no-new-privileges, read-only rootfs, a size-capped tmpfs
# at /sandbox/work, network disabled, mem/pids/cpu caps, and a hard timeout + force-remove.
# See Plans/SANDBOX_DESIGN.md.
#
# Built on first use by sandbox_exec (docker-py images.build) if absent, tagged sage-sandbox:latest.
FROM python:3.13-slim-bookworm

# Non-root sandbox user (uid 10001) and the tmpfs mountpoint.
RUN useradd -m -u 10001 sandbox && \
    mkdir -p /sandbox/work && \
    chown sandbox:sandbox /sandbox/work

USER sandbox
WORKDIR /sandbox/work

# Default command is irrelevant — sandbox_exec overrides it per exec (sh -c / python3 -c).
CMD ["python3", "-c", "print('sage-sandbox ready')"]
