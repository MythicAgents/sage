# Sage Test Tiers

Sage keeps product, development, and historical evaluation tests in one tree while the repository boundaries are
being established. The default operator command must therefore say exactly which lifecycle it validates.

Run the maintained offline suite from the repository root:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py supported
```

This tier runs every maintained offline test while explicitly excluding four append-only, rejected successor
portfolio suites whose frozen source hashes intentionally no longer match the product. Those exclusions are
named in the runner; they are not inferred from failures and must not grow silently.

Inspect those historical suites separately when working on their lifecycle:

```bash
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py retired
```

The retired tier is diagnostic and is not expected to be green. Its result does not alter an evaluation,
promotion, or product disposition.

For a small change, run the directly affected modules first, then the supported tier. Live range checks are a
separate lifecycle and never substitute for offline tests.
