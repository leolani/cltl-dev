---
name: submodule-git
description: List what changed in each CLTL submodule and the parent, commit that work, and record the resulting submodule pointers, ignoring build-generated VERSION churn. Use when asked what changed or which submodules are dirty, for an overview or diff summary across the meta-repo, or to commit, stage, or check in changes anywhere in it. Commits only — never pushes.
---

Git skill for the CLTL meta-repo: commit inside submodules, then record the
resulting pointers in the parent.

Usage: `/submodule-git [changes [<module> ...] | status | branches | commit [<module> ...] | pointers | reset-version [<module>]]`

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

- 11 gitlinks in `.gitmodules`: `emissor`, `util`, `cltl-combot`,
  `cltl-emissor-data`, `cltl-chat-ui`, `cltl-eliza`, `cltl-backend`, `cltl-asr`,
  `cltl-vad`, `cltl-context`, `cltl-monitoring`.
- **`cltl-requirements/` and `integration/` are NOT submodules.** They are
  components in the root makefile's `project_components` but ordinary tracked
  directories in the parent, so changes there — including `integration/VERSION`
  churn — are parent-repo changes. `make git-reset-version` uses
  `git submodule foreach` and therefore never touches them.
- `util` is the `leolani/cltl-build` repo, vendored again as a nested `util`
  submodule inside every component. Only `make update-build` should move it.

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

### No argument, or `changes [<module> ...]` — what changed, and where

The overview to reach for first: one line per component saying whether it holds
real work, followed by the files under it. Read-only, commits nothing. With
module names (`cltl-asr`, `integration`, …) only those are reported.

```bash
cd /workspaces/cltl-dev
TARGETS="$*"                        # empty = every component
want() { [ -z "$TARGETS" ] && return 0; case " $TARGETS " in *" $1 "*) return 0;; esac; return 1; }

version_kind() {          # $1 = repo dir, $2 = VERSION path inside it
  local head work
  head=$(git -C "$1" show "HEAD:$2" 2>/dev/null | tr -d '[:space:]')
  work=$(cat "$1/$2" 2>/dev/null | tr -d '[:space:]')
  [ -z "$head$work" ] && { echo absent; return; }
  [ "$head" = "$work" ] && { echo clean; return; }
  [ "${head%%+*}" = "${work%%+*}" ] && { echo churn; return; }
  echo "BUMP $head -> $work"
}

ptr_flag() {              # $1 = exact submodule path; prints +/-/U only when the pointer moved
  git submodule status | awk -v p="$1" 'substr($0,2) ~ "^[0-9a-f]+ "p" " { f=substr($0,1,1); if (f != " ") print f }'
}

real=(); untr=(); churn=(); clean=(); moved=()
report() {                # $1 label, $2 repo dir, $3 VERSION path, $4 ignore-submodules mode, rest: pathspec
  local label=$1 dir=$2 vp=$3 ign=$4; shift 4
  want "$label" || return 0
  local v st tracked untracked flag note
  v=$(version_kind "$dir" "$vp")
  st=$(git -C "$dir" status --porcelain --ignore-submodules="$ign" -- "$@")
  tracked=$(grep -v '^??' <<<"$st"); untracked=$(grep '^??' <<<"$st")
  flag=$(ptr_flag "$label"); note=""
  [ -n "$flag" ] && { note=" [pointer $flag]"; moved+=("$label"); }
  case "$v" in BUMP*) note="$note [VERSION $v]";; esac

  if   [ -n "$tracked" ];       then echo "$label: REAL$note";               real+=("$label")
  elif [ -n "$untracked" ];     then echo "$label: untracked only$note";     untr+=("$label")
  elif [ "${v#BUMP}" != "$v" ]; then echo "$label: VERSION bump only$note";  real+=("$label")
  elif [ "$v" = churn ];        then echo "$label: version churn only$note"; churn+=("$label")
  else                               echo "$label: clean$note";              clean+=("$label")
  fi
  if [ -n "$st" ]; then
    git -C "$dir" -c advice.statusHints=false status --ignore-submodules="$ign" -- "$@" \
      | grep -Ev '^(On branch|Your branch|nothing (to commit|added to commit)|no changes added to commit)' \
      | sed -e '/^$/d' -e 's/^/    /'
  fi
  return 0
}

echo '=== submodules ==='
for sub in $(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}' | sort); do
  report "$sub" "$sub" VERSION dirty . ':(exclude)VERSION'
done

echo
echo '=== tracked directly in the parent (NOT submodules) ==='
for d in cltl-requirements integration; do
  report "$d" . "$d/VERSION" all "$d" ":(exclude)$d/VERSION"
done
report '(root)' . VERSION all . ':(exclude)cltl-requirements' ':(exclude)integration' ':(exclude)VERSION'

echo
echo "real work     : ${real[*]:-none}"
echo "untracked only: ${untr[*]:-none}"
echo "version churn : ${churn[*]:-none}"
echo "clean         : ${clean[*]:-none}"
echo "pointer moved : ${moved[*]:-none}"
```

| Line | Meaning | Consequence |
|---|---|---|
| `REAL` | tracked modifications other than `VERSION` | `commit` stages exactly these |
| `VERSION bump only` | `VERSION` changed in its base part — a hand-edited release | real work; confirm before committing |
| `untracked only` | nothing tracked changed | `add -u` stages **nothing** here; the user must name files |
| `version churn only` | `VERSION` restamped by a build | nothing to commit — see [The VERSION rule](#the-version-rule) |
| `clean` | no changes | — |
| `[pointer +]` | submodule HEAD ≠ the commit the parent records | needs a `pointers` commit even when otherwise clean |
| `[VERSION BUMP a -> b]` | annotation on any line whose `VERSION` is not churn | never let this be reverted |

Lead the report with the `real work` summary line — it is the answer to "what is
there to commit". Then name the files per component, and say explicitly that the
`version churn only` ones need nothing.

Why the snippet is shaped this way:

- `--ignore-submodules=dirty` **inside** a submodule: a moved nested `util`
  pointer is a real change and must show, while `util`'s own working-tree dirt is
  not this submodule's business.
- `--ignore-submodules=all` at **parent** level: each submodule already has its
  own line, and a `':(exclude)*/'` pathspec cannot do this job (see Notes).
- Untracked directories collapse to one entry with a trailing `/` (`tests/`
  under "Untracked files:"). Expand with `git -C <sub> status -uall` when the
  contents matter.
- The parent loop is a fixed list because `cltl-requirements` and `integration`
  are the root makefile's non-submodule components; `(root)` is everything else
  in the parent.

### `status` — full survey: branches, staged content, pointers

`changes` answers "what is modified"; this answers "is anything about to go
wrong" — declared vs. checked-out branch, pre-existing staged content, detached
HEADs.

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

1. Anything that would block or corrupt a commit: `DETACHED` HEAD, `branch`
   differing from `decl`, or `staged > 0` (someone staged deliberately).
2. Any `VERSION=INTENTIONAL` — show old → new and flag it.
3. Submodules whose pointer is already ahead (`+` in `git submodule status`) —
   these need a parent commit even with no new work.
4. Parent-repo dirt outside submodules, so it is clear a `pointers` commit will
   **not** include it.

Counts only. For the per-file breakdown run `changes`, which is the better
answer to "what did I change"; do not expand this loop to list files.

### `branches` — branch drift report

The same loop, branch columns only. Flag every submodule whose checked-out
branch differs from its `.gitmodules` `branch =` value, and every detached HEAD.
State the consequence: the parent will point at commits that do not exist on the
declared branch, so `git submodule update --remote` (i.e. `make git-update`)
would abandon them.

### `commit [<module> ...]` — commit real work inside submodules

Named modules only, or with no names, every submodule that `changes` classifies
`REAL` or `VERSION bump only`. Never all 12 blindly, and never one classified
`untracked only` — `add -u` would stage nothing and the commit would be empty.

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
unrelated dirt (`integration/VERSION`, `.devcontainer/` changes, `CLAUDE.md`, `.claude/`,
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

`integration/` is not a submodule, so `make git-reset-version` misses
`integration/VERSION`. Handle it explicitly when it is churn:

```bash
git -C /workspaces/cltl-dev checkout -- integration/VERSION
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
