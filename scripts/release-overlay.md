# Release Overlay Workflow

Stable-prod-with-custom-changes on top of upstream Hermes Agent releases.

## What this is

A two-branch prod overlay pattern:

```
origin/main (rolling)            ── ignored
       │
upstream tag v2026.7.7.2         ── frozen, signed
       │
release/v2026.7.7.2              ── local frozen base (mirrors the tag)
       │
jasnoor/v2026.7.7.2-custom       ── prod: 7 cherry-picked custom commits
       │
       (next release: v2026.8.15)
       │
release/v2026.8.15              ── new local frozen base
       │
jasnoor/v2026.8.15-custom       ── new prod with same custom commits
```

**Why two branches per release instead of one:**

- `release/<TAG>` is the local frozen base — `git checkout release/<TAG>` gives you exactly what upstream tagged, no custom code, no surprises. Useful for rollback and for "what changed in this release?" diffs.
- `jasnoor/<TAG>-custom` is what you actually deploy. The name encodes which tag it's based on, so old deployments stay identifiable as new tags come out.

## Files

| File | Purpose |
|---|---|
| `scripts/release-overlay.sh` | The automation. Run this. |
| `scripts/release-overlay.commits` | Your custom commits, listed top-to-bottom. Edit this when adding new fixes. |
| `scripts/release-overlay.md` | This doc. |

## Initial bootstrap (you're here)

Already on `jasnoor/v0.15.2-jasnoor-working-code` with 7 custom commits ready. The script will:

1. Create `release/v2026.7.7.2` from the upstream tag (frozen, never moves).
2. Create `jasnoor/v2026.7.7.2-custom` from `release/v2026.7.7.2`.
3. Cherry-pick each commit in `scripts/release-overlay.commits` onto it.

### Run it

```bash
cd /home/pi/.hermes/hermes-agent
chmod +x scripts/release-overlay.sh
bash scripts/release-overlay.sh v2026.7.7.2
```

If any cherry-pick conflicts, the script stops with instructions. Resolve, then re-run.

### After success

```bash
# Verify the diff is what you expect
git log v2026.7.7.2..jasnoor/v2026.7.7.2-custom --oneline

# Run tests (optional but recommended)
bash scripts/run_tests.sh

# Push to your fork
git push fork release/v2026.7.7.2 jasnoor/v2026.7.7.2-custom

# Deploy
git checkout jasnoor/v2026.7.7.2-custom
# restart the gateway however you normally do it
```

## When a new release comes out (e.g. v2026.8.15)

```bash
# 1. Fetch new tags
git fetch origin --tags

# 2. (Optional) Add any new custom commits to the manifest
$EDITOR scripts/release-overlay.commits

# 3. Bootstrap the new overlay
bash scripts/release-overlay.sh v2026.8.15

# 4. Resolve conflicts if any, then re-run

# 5. Test, push, deploy
bash scripts/run_tests.sh
git push fork release/v2026.8.15 jasnoor/v2026.8.15-custom
git checkout jasnoor/v2026.8.15-custom
# restart gateway
```

The script **automatically skips commits already present on the new tag's base** — so if upstream merges one of your custom commits (or a close equivalent) in a later release, it won't be double-applied.

### Detect-if-merged workflow (use this if upstream might have caught up)

If you're not sure whether one of your historical fixes is already on a new release tag, do a quick check before adding to the manifest:

```bash
# Is this commit already reachable from the new tag?
git merge-base --is-ancestor <commit-sha> v2026.8.15 && echo "yes — skip it" || echo "no — keep it"

# What does upstream's current version look like?
git show v2026.8.15:path/to/file.py | grep -c 'feature_marker'
```

**Lesson learned (from v2026.7.7.2 overlay, July 2026):** all 4 signal attachment fixes from this profile (`fix(signal): detect ADTS AAC voice notes`, `fix(signal): route non-media attachments like Telegram`, `fix(signal): rescue text attachments from .bin`, `fix(signal): full Telegram-pattern parity`) landed in upstream `v2026.7.7.2` as equivalent implementations. The cherry-pick auto-resolved by taking HEAD (upstream) entirely, but doing the grep check up front saves a few minutes of conflict resolution. The behavior of the script's "already-on-tag-base" check would have worked for true ancestor commits, but it did NOT catch the duplicates because upstream merged *equivalent-but-different* commits — different SHAs, same effect.

## Refreshing an existing overlay

If you've added more commits to `scripts/release-overlay.commits` and want to rebuild an existing `-custom` branch from scratch:

```bash
bash scripts/release-overlay.sh v2026.7.7.2 --refresh
```

This wipes the local `jasnoor/v2026.7.7.2-custom` branch and re-applies the manifest from `release/v2026.7.7.2`. **It does not push or delete the remote branch** — that's a separate explicit step.

## Rollback

To go back to pure upstream (no custom code) for any release:

```bash
git checkout release/v2026.7.7.2
# restart gateway
```

That's it. The release branch never moves, so it's always a clean rollback target.

## Inspecting "what's in my prod"

```bash
# All commits between this release tag and the deployed custom branch
git log v2026.7.7.2..jasnoor/v2026.7.7.2-custom --oneline

# Files changed by the overlay (vs the release tag)
git diff --stat v2026.7.7.2 jasnoor/v2026.7.7.2-custom

# What's actually running right now (gateway's loaded code)
git -C /proc/$(pgrep -f 'hermes_cli.main gateway' | head -1)/cwd rev-parse HEAD
```

## Subcommands

| Command | Purpose |
|---|---|
| `scripts/release-overlay.sh --list-tags` | Show the 30 most recent upstream tags (after dedup of `^{}` peeled tags). |
| `scripts/release-overlay.sh --help` | Show usage. |
| `scripts/release-overlay.sh <TAG>` | Bootstrap or fail if custom branch exists. |
| `scripts/release-overlay.sh <TAG> --refresh` | Wipe & rebuild the local custom branch. |
| `scripts/release-overlay.sh <TAG> --manifest FILE` | Use an alternate commit list. |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `FORK_REMOTE` | `fork` | Where to push your prod branches. |
| `UPSTREAM_REMOTE` | `origin` | Where to fetch tags from. |
| `MANIFEST` | `scripts/release-overlay.commits` | Path to the commit list. |

## Gotchas

- **Working tree must be clean.** Stash or commit before running.
- **Custom commits in the manifest must still exist** in your repo's object DB. If you squashed or rebased them away, the script will fail with an "unresolvable" error.
- **Cherry-pick conflicts stop the script.** It exits with code 2 and instructions. Resolve, then re-run **without** `--refresh` — the partial cherry-picks that DID land are preserved in `jasnoor/<TAG>-custom`, and the failed one just needs to be continued.
- **Don't push `--force` to `release/<TAG>`.** That branch should never move after creation.
- **If you want zero drift from the upstream tag**, diff `release/<TAG>` against `refs/tags/<TAG>` periodically. They should be identical unless you accidentally modified the release branch.
- **Conflict pattern when upstream refactored a function you patched.** If your old commit added a string to a hardcoded set (e.g. `"tinyfish"`) but upstream replaced the set with a constant or function call, the typical resolution is: take HEAD entirely, then add your string to the *upstream* location (constant / new helper). Don't try to reinstate the hardcoded set — you'll be fighting the refactor on every release.
- **Conflicts where upstream already implemented an equivalent fix.** When the cherry-pick conflict shows your hunk deleting code that already exists upstream verbatim, the right answer is "take HEAD" and skip the cherry-pick entirely. The commit's *intent* is preserved by upstream's version; your SHA is now stale. Run `git cherry-pick --abort`, comment out (don't delete) the SHA in the manifest as documentation, and move on.

## Naming convention

- `release/<TAG>` mirrors the tag exactly.
- `jasnoor/<TAG>-custom` carries your layered commits.
- Old overlays stay around as historical artifacts — don't delete `jasnoor/v2026.7.7.2-custom` when you adopt `v2026.8.15`. Keep both for audit + rollback.
