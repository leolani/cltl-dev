---
name: submodule-git
description: Commit work across the CLTL submodules and record the resulting submodule pointers in the parent repo, ignoring build-generated VERSION churn. Use when asked to commit, stage, or check in changes anywhere in this meta-repo, or to survey what is dirty across submodules. Commits only — never pushes.
---

Git skill for the CLTL meta-repo: commit inside submodules, then record the
resulting pointers in the parent.

Usage: `/submodule-git [status | branches | commit [<module> ...] | pointers | reset-version [<module>]]`

## This skill never pushes

It runs `git add` and `git commit` only. It must never run `git push`,
`git tag`, `git commit --amend`, `git reset --hard`, `git rebase`, or
`git submodule update --remote`.

Pushing is a separate step the user performs themselves. After committing, say
so explicitly and stop — for example: *"Committed in 2 submodules and recorded
the pointers in the parent. Nothing has been pushed."* The parent repo has **no
remote configured at all**, so a parent push would fail regardless; only the
submodules have an `origin` on github.com/leolani.

## Repository shape

- 12 gitlinks in `.gitmodules`: `emissor`, `util`, `cltl-combot`,
  `cltl-emissor-data`, `cltl-chat-ui`, `app/util`, `cltl-eliza`, `cltl-backend`,
  `cltl-asr`, `cltl-vad`, `cltl-context`, `cltl-monitoring`.
- **`app/` and `cltl-requirements/` are NOT submodules.** They are ordinary
  tracked directories in the parent, so changes there — including `app/VERSION`
  churn — are parent-repo changes. `make git-reset-version` uses
  `git submodule foreach` and therefore never touches `app/VERSION`.
- `util` and `app/util` are both the `leolani/cltl-build` repo. Only
  `make update-build` should move them.

## The VERSION rule

Every component's `VERSION` is tracked, and `util/make/makefile.base.mk`
restamps it to `<base>+<unix-epoch>` on every build (only when it contains
`dev`). No `.gitignore` covers it, so it shows up as a modification constantly.
That churn must never be committed on its own.

But a `VERSION` change is **not always** churn — a hand-edited release bump looks
identical in `git status`. Classify before acting:

```bash
version_change_kind() {   # $1 = submodule path -> clean | churn | intentional | absent
  local head work
  head=$(git -C "$1" show HEAD:VERSION 2>/dev/null | tr -d '[:space:]')
  work=$(cat "$1/VERSION" 2>/dev/null | tr -d '[:space:]')
  [ -z "$head$work" ] && { echo absent; return; }
  [ "$head" = "$work" ] && { echo clean; return; }
  [ "${head%%+*}" = "${work%%+*}" ] && { echo churn; return; }
  echo intentional
}
```

Churn iff the part before the first `+` is unchanged:
`0.0.dev1+1643925433 → 0.0.dev1+1784890147` is churn;
`0.0.dev7 → 1.0.dev0` is a real bump and must never be discarded.

**Mechanism: exclude VERSION from the add, do not revert it.**

```bash
git -C <sub> add -u -- . ':(exclude)VERSION'
```

- `-u` stages tracked modifications only, deliberately leaving untracked files alone.
- Do **not** `git checkout -- VERSION` first — that silently destroys an
  intentional bump.
- Never `git commit -a` or `git add -A`: both stage `VERSION` regardless of any
  pathspec.

---

## Dispatch on `$ARGUMENTS`

### No argument, or `status` — survey, report, propose. No writes.

```bash
cd /workspaces/cltl-dev
for sub in $(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}'); do
  head=$(git -C "$sub" show HEAD:VERSION 2>/dev/null | tr -d '[:space:]')
  work=$(cat "$sub/VERSION" 2>/dev/null | tr -d '[:space:]')
  if   [ -z "$head$work" ];               then v=absent
  elif [ "$head" = "$work" ];             then v=clean
  elif [ "${head%%+*}" = "${work%%+*}" ]; then v=churn
  else v=INTENTIONAL; fi
  br=$(git -C "$sub" symbolic-ref --quiet --short HEAD || echo DETACHED)
  decl=$(git config -f .gitmodules --get "submodule.$sub.branch" || echo -)
  mod=$(git -C "$sub" diff  --name-only -- . ':(exclude)VERSION' | wc -l)
  unt=$(git -C "$sub" ls-files -o --exclude-standard -- . ':(exclude)VERSION' | wc -l)
  stg=$(git -C "$sub" diff --cached --name-only | wc -l)
  printf '%-18s VERSION=%-11s branch=%-14s (decl %s) modified=%s untracked=%s staged=%s\n' \
         "$sub" "$v" "$br" "$decl" "$mod" "$unt" "$stg"
done
echo '--- pointers ---'
git submodule status                    # '+' = worktree HEAD differs from the parent's record
echo '--- parent, outside submodules ---'
git status --porcelain --ignore-submodules=all
```

Report in this order, then **stop** — this branch commits nothing:

1. Submodules with real work (`modified > 0`), naming the files.
2. Untracked files — list every one. These are often agent debris (stray
   `CLAUDE.md`, scratch scripts) and are never committed by default.
3. Any `VERSION=INTENTIONAL` — show old → new and flag it.
4. Any `DETACHED` HEAD, or `branch` differing from `decl`.
5. Submodules whose pointer is already ahead (`+` in `git submodule status`) —
   these need a parent commit even with no new work.
6. Parent-repo dirt outside submodules, so it is clear a `pointers` commit will
   **not** include it.

### `branches` — branch drift report

The same loop, branch columns only. Flag every submodule whose checked-out
branch differs from its `.gitmodules` `branch =` value, and every detached HEAD.
State the consequence: the parent will point at commits that do not exist on the
declared branch, so `git submodule update --remote` (i.e. `make git-update`)
would abandon them.

### `commit [<module> ...]` — commit real work inside submodules

Named modules only, or with no names, every submodule with `modified > 0`.
Never all 12 blindly.

Per submodule, in order:

1. **Guard: branch.** Skip and report if detached. If the checked-out branch
   differs from `.gitmodules`, ask once for the whole batch, naming both
   branches and the consequence.
2. **Guard: pre-existing staged content.**
   ```bash
   git -C "$sub" diff --cached --name-only
   ```
   If non-empty, stop and show it — someone staged deliberately; do not extend
   or discard it without asking.
3. **Guard: VERSION.** If `version_change_kind` is `intentional`, show old → new
   and ask whether to include `VERSION` in this commit. Default is to include it
   (it is real work) and to say so in the message.
4. **Show what will be staged.**
   ```bash
   git -C "$sub" status --porcelain -- . ':(exclude)VERSION'
   ```
5. **Stage tracked modifications only.**
   ```bash
   git -C "$sub" add -u -- . ':(exclude)VERSION'
   ```
6. **Untracked files — only ones the user named, one path at a time.**
   ```bash
   git -C "$sub" add -- <explicit/path>
   ```
   Never `git add .` — it sweeps stray files into history.
7. **Verify the index before committing.** `git add --dry-run` produces no
   output in this environment and must not be used as a preview; inspect the
   index instead:
   ```bash
   git -C "$sub" diff --cached --name-only
   ```
   If `VERSION` appears and was classified `churn`, unstage it and stop:
   ```bash
   git -C "$sub" restore --staged VERSION
   ```
8. **Commit.** Short imperative subject matching repo history ("Add event log
   writer", "Update integration tests"), derived from the actual diff. **No
   trailers** — no `Co-Authored-By`, no session link; existing history has none.
   ```bash
   git -C "$sub" commit -m "<subject>"
   ```
9. Report the new SHA.

After the loop, offer to run `pointers`. Do not run it automatically — the user
may want to inspect the submodule commits first.

### `pointers` — record submodule pointers in the parent

```bash
cd /workspaces/cltl-dev
git submodule status              # '+' marks a pointer differing from the parent's record
```

Collect the differing paths, show them, then stage **exactly** those:

```bash
git -C /workspaces/cltl-dev add -- <sub1> <sub2> ...
git -C /workspaces/cltl-dev diff --cached --name-only    # must list gitlinks only
git -C /workspaces/cltl-dev commit -m "Update submodules"
```

Never `git add -A`, `-u`, or `.` at root. The parent working tree carries
unrelated dirt (`app/VERSION`, `.devcontainer/` changes, `CLAUDE.md`, `.claude/`,
`docs/`, stray files under `cltl-requirements/`) that must not enter a pointer
commit. If `git diff --cached --name-only` shows anything that is not a
submodule path, unstage everything and stop:

```bash
git -C /workspaces/cltl-dev restore --staged .
```

Include submodules showing `+` even without new work in this session, but say
which ones were already ahead beforehand.

Then state plainly that nothing has been pushed.

### `reset-version [<module>]` — discard pure VERSION churn

A safer `make git-reset-version`. Classify first; never revert a VERSION
classified `intentional`, and never use `git submodule foreach` blindly.

```bash
# only when version_change_kind is 'churn':
git -C /workspaces/cltl-dev/<sub> checkout -- VERSION
```

`app/` is not a submodule, so `make git-reset-version` misses `app/VERSION`.
Handle it explicitly when it is churn:

```bash
git -C /workspaces/cltl-dev checkout -- app/VERSION
```

This is cosmetic — the next build restamps everything. Usually the right answer
is to leave the churn alone.

---

## Ask before running

- **Committing while a submodule is off its declared branch** — confirm once per
  session, naming the checked-out and declared branches.
- **Staging any untracked file** — list them and require the user to name the
  ones to include.
- **A `VERSION` classified `intentional`** — show old → new and confirm.
- **A submodule with pre-existing staged content** — stop and show it.
- **`make git-update`** — out of scope and destructive here: it runs
  `git submodule update --remote`, moving submodules onto the branch declared in
  `.gitmodules`. If asked, explain and decline.

## Notes

- Order is always: commit inside each submodule first, then commit the pointers
  in the parent. A parent pointer commit referencing an uncommitted submodule
  state is meaningless.
- `git status --porcelain -- . ':(exclude)VERSION'` works here;
  `git add --dry-run` prints nothing and is useless as a preview.
- To list parent dirt excluding submodules, use
  `git status --porcelain --ignore-submodules=all`. A `':(exclude)*/'` pathspec
  does **not** work: gitlinks are recorded as bare path entries (`cltl-asr`), not
  directories, so they survive the exclusion while genuine untracked directories
  get hidden.
- The `make update-build` target does something superficially similar, but only
  for the nested `util` submodule; its `git diff --cached --quiet` guard is
  inverted and it misreports every failure as "Stash is not empty". Do not reuse
  it as a model.
- Commit subjects follow existing history: short, imperative, no scope prefix,
  no body unless the change genuinely needs one.
- The `build` skill explains where the VERSION churn comes from and how to
  rebuild the environment that generates it.
