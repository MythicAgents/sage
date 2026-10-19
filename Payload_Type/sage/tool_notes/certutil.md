# certutil

## Hangs — interactive prompts

- `certutil -config -` prompts for CA selection interactively. A non-interactive shell
  (an Apollo task) waits forever — the task never returns and the wait only ends at the
  timeout. Observed 2026-10-15.
  - Remedy: name the CA explicitly (`certutil -config <CA-Name> ...`), or enumerate CAs
    without the interactive form first (`certutil -dump`, `certutil -catemplates`).
  - A task already hanging on this: end the spawned process on the target, or re-issue
    with the CA named. Deciding between those is an operator call.
