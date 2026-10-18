# Off-target artifact inspection

The operator inspection CLI sends exact artifact bytes to an ephemeral Docker container.
It provides static inspection and a reviewed Certify help profile without using a payload
callback. It does not register a new Sage model tool or approve target actions. The separate
[executable contract path](EXECUTABLE_CONTRACTS.md) validates arguments sent through
supported Mythic wrappers.

Sage ships image definitions and harmless test-fixture sources. Operators supply their
own tool binaries; the images contain no Certify executable.

## Build the images

Run from the repository root using the configured Docker service:

```bash
docker build --target static -t sage-inspection-static:1 Payload_Type/sage/container/inspection
```

```bash
docker build --platform linux/amd64 --target wine -t sage-inspection-wine:1 Payload_Type/sage/container/inspection
```

The Dockerfile pins its base image and direct runtime dependencies. The Wine profile uses
Wine 8.0 and Wine Mono 7.4.0. Dependencies are installed when building the image; jobs
cannot download them. The static image follows the selected build platform. The Wine
image is amd64; an ARM64 Docker host requires working amd64 emulation for both builds
and execution. A successful cross-platform build alone does not establish execution
support. Configure emulation explicitly using the [Docker multi-platform instructions](https://docs.docker.com/build/building/multi-platform/).
The broker neither installs emulation nor pulls/builds an image automatically.

Pass the immutable image ID to inspection, rather than a mutable tag. Rebuilding an image
changes its identity and invalidates cached results.

## Inspect an artifact

Static inspection does not load the target assembly or invoke its entry point:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/inspect_artifact.py path/to/tool.exe --sha256 EXPECTED_SHA256 --profile static-v1 --recipe metadata --image-id "$(docker image inspect --format '{{.Id}}' sage-inspection-static:1)"
```

The result distinguishes partial metadata from a recovered command interface. A PE machine
field is not by itself a managed assembly's complete runtime architecture requirement.
Malformed files and unsupported formats do not become verified contracts.

The initial help profile accepts only the artifact enrolled in
[the Certify descriptor](../../Payload_Type/sage/ttps/contracts/certify.json):

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/inspect_artifact.py Payload_Type/sage/tools/Certify.exe --sha256 1383e8bca90e4be70ff2224b7e2af465a8f3d5bede1fde60f1ece71e3b02d346 --profile certify-help-v1 --recipe request --image-id "$(docker image inspect --format '{{.Id}}' sage-inspection-wine:1)"
```

Reviewed recipes are `top`, `enum-cas`, `enum-templates`, `request`, and
`request-download`. They deliberately request parser-error/usage output. They do not
accept caller-written command strings or operational arguments. This exact binary
initializes COM before parsing, so isolation also covers startup code. External
dependencies are rejected by this help profile: its reviewed dependencies are embedded.
A different binary with the same filename is not automatically enrolled.

`--output result.json` saves the JSON to a new file and refuses to overwrite an existing
file. Without it, JSON goes to stdout. `--timeout` changes the bounded wall timeout.
The CLI exits zero when it captures a completed workload, including a nonzero parser
exit; inspect the result's own `status`, `exit_code`, `stdout`, and `stderr`. A timeout,
output limit, startup failure, or failed cleanup is not a successful help observation.

## Isolation and result interpretation

Jobs use network mode `none`, a read-only root filesystem, UID/GID 10001, dropped
capabilities, and `no-new-privileges`. A fresh, bounded tmpfs holds input and the Wine
prefix. There are no host-file, credential, runtime-database, or Docker-socket mounts.
The trusted broker talks to Docker; the workload cannot. Artifact transfer uses a bounded
manifest and byte stream, with hash checks before dispatch and inside the workload.

CPU, memory, PID count, wall time, and each output stream are bounded. Output is limited
while draining, and Docker's per-container log storage is disabled. The broker forcibly
removes the container, including its children, on completion or failure. A cleanup error
is reported explicitly so the operator can resolve the exact remaining container.

Captured text is untrusted data, not instructions, an automatically accepted CLI schema,
or target evidence. Exit zero does not establish useful usage text; a nonzero parser exit
may contain useful usage. Static extraction, observed help, argument validation, Mythic
transport, and successful target behavior are separate claims.

## Cache and unavailable runners

The cache binds the artifact and dependency hashes, immutable image ID, profile, recipe,
resource limits, and result schema revision. Replacing any bound input invalidates reuse.
Entries are checked for structural consistency and integrity; the cache does not
authenticate a local writer. It never turns captured help into trusted instructions.

The CLI defaults to `$XDG_CACHE_HOME/sage/artifact-inspection`, or
`~/.cache/sage/artifact-inspection` when that variable is unset. Use `--cache-dir` to
choose a different directory or a fresh directory for an uncached observation. Treat
cached output and exported results as operator data.

An unavailable image/runtime is reported without a target-callback fallback. Arbitrary
Windows programs, BOFs, native ARM64 Wine, and automatic CLI enrollment are outside the
initial compatibility claim. The existing guarded `sandbox_exec` tool retains its own
interface and authority checks.

## Measured compatibility

The enrolled Certify bytes produced option usage for `enum-cas`, `enum-templates`,
`request`, and `request-download` on an ARM64 Linux Docker host using amd64 emulation,
Wine 8.0 and Wine Mono 7.4.0. Their option names matched the descriptor, including the
required request fields and the displayed 2048-bit key-size default. The `top` recipe
only reports that `help` is an unknown verb; it does not recover top-level usage.
Help output alone does not prove property types, option-set exclusion, or handler behavior.

Fresh-prefix Certify jobs initially took about 21–23 seconds on the measured host;
one overlapped emulated workload took 111 seconds. Run inspection jobs sequentially
on resource-constrained emulation hosts. Each uncached job initializes a new prefix;
a cache hit avoids that execution. Images occupy about
170 MB (native static) and 1.09 GB (amd64 Wine), as reported by Docker before layer sharing.
These are development measurements, not a performance guarantee. Native amd64 host
execution and other Windows binaries have not been validated by this experiment.
The workload deadline initiates termination; Docker control and cleanup add time,
and host or daemon stalls can delay the broker's return. Failure results remain explicit.

Wine emitted a missing `syswow64/rundll32.exe` initialization warning while the reviewed
help recipes completed. This limited help result does not establish operational Certify
compatibility. Harmless native and managed COM fixtures also ran inside the same isolation.
