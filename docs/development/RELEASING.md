# Releasing Sage

The maintainer's checklist for publishing a Sage version. Take the steps in order, and update this file whenever
the procedure changes. A step is done when its evidence exists, not when its command exits.

A release is three things that must agree: the version image `ghcr.io/mythicagents/sage:<version>` for
`linux/amd64` and `linux/arm64`, the signed annotated tag `v<version>` on `main`, and the GitHub release whose
notes come from `CHANGELOG.md`. `mythic-cli install` pulls the image that `config.json` names.

## How the pieces fit

| Piece | Where it lives | What checks it |
|---|---|---|
| Declared version | `agent_capabilities.json`, `agent_version` | `Payload_Type/sage/tests/test_version.py` on every CI run; `.github/workflows/version-check.yml` on the tag |
| Install image | `config.json`, `remote_images.sage` = `ghcr.io/mythicagents/sage:<agent_version>` | The same two checks |
| Release notes | `CHANGELOG.md`, section `## [<version>] - <date>` | Review |
| Version image | Published by `.github/workflows/container.yml` from `main` only | The tag's `verify-release` job (`.github/scripts/release_image.py verify`) |

- A push to `dev`, a pull request or a manual run builds the image without publishing it.
- A push to `main` publishes `:latest`, `:sha-<short commit>` and, until `v<agent_version>` is tagged,
  `:<agent_version>`. Every push to `main` before the tag republishes `:<agent_version>`.
- A `v*` tag rebuilds nothing. It checks that the tag equals `agent_version`, is annotated, points at a commit on
  `main`, and carries exactly one `Image-Digest: sha256:<64 hex>` line equal to the digest `:<version>` resolves to;
  that both platforms are present; and that each platform's OCI revision label is the tagged commit and its version
  label is `agent_version`. For a final release (no `-suffix`) that is the newest in its minor line, it then points
  `:<major>.<minor>` at the same manifest.
- `main` and `dev` accept only signed commits, and `main` moves only by fast-forward from `dev`.

**Why the version bump travels with the `main` push.** By default Mythic bind-mounts Sage's service directory over
the image's copy of the source (`/Mythic`), so a running Sage takes its code from the installed release and its Python packages from the image
`config.json` names. A `main` that carries new source with an old `remote_images` entry installs new code on old
packages.

## 1. Before the release

- Everything that belongs in the release is on `dev`, and CI on `dev` is green: **CI (offline boundary)**,
  **Container image** and **CodeQL**. The pip-audit and ruff jobs are advisory (`continue-on-error`), so they show red
  without failing the workflow: review pip-audit's findings and list what remains under the release's Known issues.
- `CHANGELOG.md` `## [Unreleased]` describes every operator-visible change, and the README's Limitations and Known
  issues match it.
- Dependency upgrades are finished or deferred. None land between steps 2 and 7.

## 2. Prepare the release commit on `dev`

Make one commit that changes exactly these:

- `agent_capabilities.json`: `"agent_version" : "<version>"`
- `config.json`: `"sage": "ghcr.io/mythicagents/sage:<version>"`
- `CHANGELOG.md`: rename `## [Unreleased]` to `## [<version>] - <YYYY-MM-DD>` and add an empty `## [Unreleased]`
  above it.

Run the full offline suite, then push:

```
.venv/bin/python skills/sage-focused-capability-tests/scripts/run_offline_suite.py
git push origin dev
```

Wait for CI on `dev` to pass. This commit is the release candidate, `C` (`git rev-parse HEAD`). Until `main` moves,
`dev`'s `config.json` names an image that does not exist yet; that is expected.

## 3. Qualify the candidate (when the release has a live run)

A live run uses `C`, or an image built from it, and its image, architecture, Mythic version, model route, objective
and result go in the GitHub release notes. A source or build-input change after the run makes a new candidate:
start again at step 2.

## 4. Move `main` to the candidate

```
git switch main && git pull --ff-only && git merge --ff-only <C> && git push origin main
```

No merge commit and no rebase: `main` must contain `C` itself.

## 5. Record the published image

Wait for the **Container image** workflow on that push to `main`. Then read the version image (this needs Docker
Buildx):

```
python3 .github/scripts/release_image.py inspect ghcr.io/mythicagents/sage:<version>
```

Check that `digest` is a `sha256:` value (call it `D`), that both `linux/amd64` and `linux/arm64` are listed, and that
each platform's revision label is `C` and its version label is `<version>`. From here until step 7 is done, push
nothing to `main`: a push republishes `:<version>` and the tag check would then fail.

## 6. Check the published install

On a test Mythic v4 host, with build-context mode off (`sudo ./mythic-cli config set sage_use_build_context false`):

```
sudo ./mythic-cli install github https://github.com/MythicAgents/sage
```

Confirm that the `sage` container runs `ghcr.io/mythicagents/sage:<version>`
(`docker inspect sage --format '{{.Config.Image}}'`), that its startup banner (`sudo ./mythic-cli logs sage`) shows
build commit `C`, and that a new chat offers the Sage model. This checks distribution only; run no target tasks. A
failure stops the release before the tag: fix it on `dev` and start again at step 2.

## 7. Tag the release

Create a signed annotated tag at `C` that carries the digest, and push it:

```
git tag -s v<version> -m "Sage <version>" -m "Image-Digest: <D>" <C>
git push origin v<version>
```

The **Release version check** and the **Container image** `verify-release` job must both pass. If either fails, leave
the tag where it is, find the cause, and decide deliberately whether to delete and recreate it; never move a
published tag automatically.

## 8. Publish the GitHub release

```
gh release create v<version> --verify-tag --title "Sage <version>" --notes-file <notes.md>
```

The notes are the CHANGELOG section for `<version>`, the live-run details from step 3 (or a sentence saying the
release had no live run) and the result of step 6. Add `--prerelease` for a version with a suffix such as `-beta`.
The README's Limitations section sends readers to these notes for live-run details, so every release says whether
it had one.

## 9. After the release

- The empty `## [Unreleased]` section from step 2 collects the next release's changes.
- Work deferred for the release, such as dependency upgrades, resumes on `dev`.
