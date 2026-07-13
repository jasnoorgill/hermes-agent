#!/usr/bin/env bash
# release-overlay.sh
#
# Build or refresh a prod overlay branch on top of a stable Hermes release tag.
#
# What it does:
#   1. Verifies clean working tree on a base branch you specify.
#   2. Fetches origin and confirms the requested release tag exists upstream.
#   3. Creates (or fast-refreshes) `release/<TAG>` from the upstream tag.
#      - If `release/<TAG>` already exists locally, it is left as-is (frozen).
#   4. Creates (or refreshes) `jasnoor/<TAG>-custom` from `release/<TAG>`,
#      then cherry-picks each commit listed in the "custom commits" manifest
#      onto it. Duplicates already on the tag base are skipped automatically.
#   5. Reports the resulting branch tip, list of custom commits applied,
#      and the count of skipped / failed cherry-picks.
#
# Usage:
#   scripts/release-overlay.sh <TAG>                      # initial bootstrap
#   scripts/release-overlay.sh <TAG> --refresh           # wipe & rebuild custom branch
#   scripts/release-overlay.sh <TAG> --manifest FILE     # use a custom commit list
#   scripts/release-overlay.sh --list-tags               # show available upstream tags
#
# Re-running for a NEW release (e.g. v2026.8.15):
#   1. Add new commits to scripts/release-overlay.commits
#   2. Run: scripts/release-overlay.sh v2026.8.15
#   3. Inspect the result, push when ready.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

FORK_REMOTE="${FORK_REMOTE:-fork}"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-origin}"
MANIFEST="${MANIFEST:-scripts/release-overlay.commits}"
LOG_PREFIX="[release-overlay]"

log() { printf '%s %s\n' "$LOG_PREFIX" "$*"; }
die() { printf '%s ERROR: %s\n' "$LOG_PREFIX" "$*" >&2; exit 1; }

# ---- subcommand: list upstream tags ---------------------------------------
if [[ "${1:-}" == "--list-tags" ]]; then
    git ls-remote --tags --sort=-v:refname "$UPSTREAM_REMOTE" \
        | awk '{print $2}' \
        | sed -e 's|refs/tags/||' -e 's|\^{}$||' \
        | awk '!seen[$0]++' \
        | head -30
    exit 0
fi

# ---- argument parsing -----------------------------------------------------
TAG=""
REFRESH=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --refresh) REFRESH=1; shift ;;
        --manifest) MANIFEST="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0 ;;
        -*) die "unknown flag: $1" ;;
        *)
            [[ -z "$TAG" ]] || die "only one TAG argument allowed (got '$TAG' and '$1')"
            TAG="$1"; shift ;;
    esac
done

[[ -n "$TAG" ]] || die "missing TAG argument. Try '$0 --list-tags' to see options."

# ---- preconditions --------------------------------------------------------
command -v git >/dev/null || die "git not on PATH"

if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
    die "upstream remote '$UPSTREAM_REMOTE' not configured (expected $(git remote -v | awk '/nousresearch/ {print $1; exit}'))"
fi
if ! git remote get-url "$FORK_REMOTE" >/dev/null 2>&1; then
    die "fork remote '$FORK_REMOTE' not configured (expected $(git remote -v | awk '/jasnoorgill/ {print $1; exit}'))"
fi

[[ -f "$MANIFEST" ]] || die "manifest not found: $MANIFEST"
[[ -s "$MANIFEST" ]] || die "manifest is empty: $MANIFEST"

# Working tree must be clean (refuse to clobber uncommitted work).
if ! git diff --quiet --ignore-submodules HEAD 2>/dev/null; then
    die "working tree has uncommitted changes. Commit or stash before running."
fi
if ! git diff --quiet --ignore-submodules --cached 2>/dev/null; then
    die "index has staged changes. Commit or stash before running."
fi

# ---- fetch & verify tag ---------------------------------------------------
log "fetching $UPSTREAM_REMOTE ..."
git fetch --tags --prune "$UPSTREAM_REMOTE" >/dev/null 2>&1 \
    || die "git fetch $UPSTREAM_REMOTE failed"

if ! git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null; then
    die "tag '$TAG' not found upstream. Try '$0 --list-tags'."
fi
TAG_SHA="$(git rev-parse --verify "refs/tags/$TAG^{commit}")"
log "tag $TAG resolved to $TAG_SHA"

# ---- read manifest --------------------------------------------------------
# Lines in the manifest are either:
#   - blank / whitespace-only  -> ignored
#   - starting with '#'        -> comment
#   - any other text           -> commit SHA (or branch/ref) to cherry-pick
mapfile -t CUSTOM_COMMITS < <(
    grep -vE '^\s*(#|$)' "$MANIFEST" \
        | awk '{$1=$1; print}' \
        | awk '!seen[$0]++'
)
[[ ${#CUSTOM_COMMITS[@]} -gt 0 ]] || die "no commits listed in $MANIFEST"

log "manifest has ${#CUSTOM_COMMITS[@]} unique commit(s) to apply"

# Verify each commit is resolvable. Also note any already on the tag base so we can skip them.
declare -a TO_APPLY=()
declare -a ALREADY_PRESENT=()
declare -a UNRESOLVABLE=()
for ref in "${CUSTOM_COMMITS[@]}"; do
    if ! git rev-parse --verify --quiet "$ref^{commit}" >/dev/null; then
        UNRESOLVABLE+=("$ref")
        continue
    fi
    if git merge-base --is-ancestor "$ref" "$TAG"; then
        ALREADY_PRESENT+=("$ref")
    else
        TO_APPLY+=("$ref")
    fi
done

if [[ ${#UNRESOLVABLE[@]} -gt 0 ]]; then
    for r in "${UNRESOLVABLE[@]}"; do
        log "  ! unresolvable: $r"
    done
    die "${#UNRESOLVABLE[@]} manifest entries could not be resolved"
fi

if [[ ${#ALREADY_PRESENT[@]} -gt 0 ]]; then
    log "skipping ${#ALREADY_PRESENT[@]} commit(s) already present on $TAG:"
    for r in "${ALREADY_PRESENT[@]}"; do
        log "  - $r  $(git log -1 --pretty=format:'%s' "$r")"
    done
fi

log "will cherry-pick ${#TO_APPLY[@]} commit(s)"

# ---- create / refresh release/<TAG> ---------------------------------------
RELEASE_BRANCH="release/$TAG"
CUSTOM_BRANCH="jasnoor/$TAG-custom"

if git show-ref --verify --quiet "refs/heads/$RELEASE_BRANCH"; then
    log "local $RELEASE_BRANCH already exists; leaving frozen as-is"
else
    log "creating $RELEASE_BRANCH from tag $TAG"
    git branch "$RELEASE_BRANCH" "$TAG"
fi

if [[ ! "$REFRESH" -eq 1 ]] && git show-ref --verify --quiet "refs/heads/$CUSTOM_BRANCH"; then
    die "$CUSTOM_BRANCH already exists. Re-run with --refresh to wipe & rebuild it."
fi

if git show-ref --verify --quiet "refs/heads/$CUSTOM_BRANCH"; then
    log "wiping existing $CUSTOM_BRANCH (--refresh)"
    git branch -D "$CUSTOM_BRANCH" >/dev/null
fi

log "creating $CUSTOM_BRANCH from $RELEASE_BRANCH"
git checkout -b "$CUSTOM_BRANCH" "$RELEASE_BRANCH" >/dev/null

# ---- cherry-pick ----------------------------------------------------------
APPLIED=()
FAILED=()
for ref in "${TO_APPLY[@]}"; do
    SUBJECT="$(git log -1 --pretty=format:'%s' "$ref")"
    log "  -> cherry-pick $ref  ($SUBJECT)"
    if git cherry-pick -x "$ref" >/dev/null 2>&1; then
        APPLIED+=("$ref")
    else
        FAILED+=("$ref")
        log "     STOPPED with conflicts. Resolve and run:"
        log "       git cherry-pick --continue   # to apply this commit"
        log "       git cherry-pick --abort      # to skip it"
        log "     Then re-run scripts/release-overlay.sh $TAG (without --refresh)."
        exit 2
    fi
done

# ---- report ---------------------------------------------------------------
TIP_SHA="$(git rev-parse HEAD)"
log ""
log "==============================================================="
log "DONE"
log "==============================================================="
log "release base:   $RELEASE_BRANCH @ $TAG_SHA"
log "custom branch:  $CUSTOM_BRANCH @ $TIP_SHA"
log "applied:        ${#APPLIED[@]} / ${#TO_APPLY[@]} manifest commits"
log "skipped:        ${#ALREADY_PRESENT[@]} already on $TAG"
log "failed:         ${#FAILED[@]}"
log ""
log "Next steps:"
log "  1. Inspect the diff vs $TAG:    git log $TAG..$CUSTOM_BRANCH --oneline"
log "  2. Run tests:                   bash scripts/run_tests.sh"
log "  3. Push to fork:                git push $FORK_REMOTE $RELEASE_BRANCH $CUSTOM_BRANCH"
log "  4. Deploy:                      git checkout $CUSTOM_BRANCH && restart gateway"
