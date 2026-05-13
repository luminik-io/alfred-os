# Claude Code skills

Skills are small bundles (markdown + optional scripts) that extend Claude Code's tool surface for a specific purpose: code review, refactoring, browser testing, security checks. Alfred does not ship skills itself. Consumer agents pick the ones they want.

This doc lists a recommended skill set for an autonomous engineering fleet, what each skill is for, and the install commands.

## Where skills live

```
~/.claude/skills/
├── code-review/SKILL.md
├── code-review-and-quality/SKILL.md
├── debugging-and-error-recovery/SKILL.md
├── frontend-ui-engineering/SKILL.md
├── security-and-hardening/SKILL.md
├── spec-driven-development/SKILL.md
├── vercel-react-best-practices/SKILL.md
├── autofix/SKILL.md
└── gstack/                  # the gstack tap installs as a directory of subskills
    ├── browse/
    ├── investigate/
    ├── qa/
    ├── review/
    └── ship/
```

`claude` resolves skill names against `~/.claude/skills/` at run-time. Once installed, any `claude -p` invocation can invoke them as tools (the agent's prompt should explicitly name the skill it wants to use, e.g. "Use the `code-review-and-quality` skill on the changed files before committing").

## Recommended set for an autonomous engineering fleet

| Skill | Source | Used by | Why |
|---|---|---|---|
| `spec-driven-development` | Anthropic official | feature-dev agents | Forces the model to derive code from a written spec instead of inventing requirements |
| `code-review-and-quality` | Anthropic official | feature-dev (self-check), reviewer | Multi-axis review: correctness, edge cases, type safety, test coverage |
| `security-and-hardening` | Anthropic official | feature-dev (auth/IAM/session paths), reviewer | Security-specific lens; complements code-review-and-quality |
| `debugging-and-error-recovery` | Anthropic official | bug-triage, monitoring agents | Systematic root-cause path |
| `frontend-ui-engineering` | Anthropic official | feature-dev (frontend repos) | Component patterns, state, layouts |
| `vercel-react-best-practices` | community | feature-dev (React/Next.js) | RSC patterns, perf optimisation |
| `code-review` | CodeRabbit | reviewer agents | Backbone for structured review, pairs with `/review` |
| `autofix` | CodeRabbit | review-fix agents | Apply CodeRabbit-flagged P0/P1 fixes with per-change approval |
| `/review`, `/ship`, `/qa`, `/browse`, `/investigate` | gstack | various | gstack's CLI-first review/ship/QA flow |

## Install commands

### Anthropic official skills

These ship in the [`anthropics/claude-code`](https://github.com/anthropics/claude-code) repo under `skills/`. One-time copy:

```sh
mkdir -p ~/.claude/skills
git clone --depth 1 https://github.com/anthropics/claude-code.git /tmp/cc-skills-src
cp -R /tmp/cc-skills-src/skills/* ~/.claude/skills/
rm -rf /tmp/cc-skills-src
```

To update later:

```sh
git clone --depth 1 https://github.com/anthropics/claude-code.git /tmp/cc-skills-src
rsync -a --delete /tmp/cc-skills-src/skills/ ~/.claude/skills/
rm -rf /tmp/cc-skills-src
```

### gstack

```sh
git clone https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
cd ~/.claude/skills/gstack && ./setup
```

The setup script symlinks each subdirectory back up to `~/.claude/skills/<name>` so they resolve as top-level skills (`/review`, `/ship`, etc.).

### CodeRabbit

```sh
npx -y skills add coderabbitai/skills --global --yes \
    --agent claude-code --skill '*'
```

Installs all CodeRabbit skills into `~/.claude/skills/`. Note: the security audit on `skills.sh` flags `code-review` as Snyk Med Risk and `autofix` as High Risk. Both are reviewable; treat as "review-before-use". `autofix` itself enforces per-change approval at runtime.

### Vercel React best practices

Vercel's React guidance moves over time. Install this only after verifying the current published source for your Claude Code setup, or omit it and rely on your own frontend prompt conventions.

## Skill-install automation

If you want fresh-install reproducibility, a script that installs a known set of skills:

```sh
# scripts/install-skills.sh in your fleet repo
#!/usr/bin/env bash
set -euo pipefail
mkdir -p ~/.claude/skills
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Anthropic official
git clone --depth 1 https://github.com/anthropics/claude-code.git "$TMPDIR/cc"
rsync -a "$TMPDIR/cc/skills/" ~/.claude/skills/

# gstack
if [[ ! -d ~/.claude/skills/gstack ]]; then
  git clone https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
  (cd ~/.claude/skills/gstack && ./setup)
fi

# CodeRabbit (interactive prompt: confirm yes/no for each)
npx -y skills add coderabbitai/skills --global --yes \
    --agent claude-code --skill '*'

echo "skills installed:"
ls ~/.claude/skills/
```

Run on every fresh host. Idempotent: `git clone` fails loud if the dir exists, the rsync silently overwrites stale Anthropic skills, the npx install is a no-op when versions match.

## Per-agent skill matrix

Document which skills each codename invokes in your fleet's own agent prompts or runbooks. A typical engineering-fleet matrix:

| Codename | Skills it invokes |
|---|---|
| Lucius (feature dev) | `spec-driven-development`, `code-review-and-quality` (self-check), `security-and-hardening` (auth paths), `frontend-ui-engineering` + `vercel-react-best-practices` (FE repo only), `/investigate` (vague issues), `/review` (final pre-push) |
| Bane (test coverage) | `code-review-and-quality`, `/qa` (integration scenarios) |
| Ra's al Ghul (PR review) | `code-review`, `code-review-and-quality`, `security-and-hardening`, `/review` |
| Nightwing (review-fix) | `autofix` (CodeRabbit thread auto-closure), `code-review-and-quality`, `/review` |
| Robin (bug triage) | `debugging-and-error-recovery`, `/investigate` |
| Gordon or another deploy monitor | `debugging-and-error-recovery`, `/investigate` |
| Huntress (E2E smoke) | `/browse`, `/qa` |

Skills are opt-in per-agent. No "skill bus" the framework wires up. The agent's prompt tells `claude -p` to invoke a skill, e.g.:

```
After implementing the change, invoke the `code-review-and-quality` skill
on every file you edited. Apply any P0 or P1 finding before you commit.
```

## Security note

Skills run with the same permissions as `claude`. They can read/write files in the agent's worktree, run shell commands, invoke other tools. Treat any new skill the way you'd treat any other dependency:

1. Read the `SKILL.md` before installing.
2. Skim the scripts the skill might invoke.
3. Run a Snyk / CodeQL scan if the source is unfamiliar.
4. Pin to a specific version/commit when installing from a third-party tap.

The fleet's IAM-per-agent and per-firing-worktree-isolation patterns limit blast radius (a malicious skill in the Lucius worktree can't reach the operator's home or the secondary Claude account). Mitigations, not prevention.

## Skills NOT recommended for an autonomous fleet

- **Anything that auto-publishes** (auto-tweet, auto-deploy, auto-merge). Use these as draft-then-review only.
- **Skills that fork to the network without explicit allowlists.** Network egress from a worktree is a known agent attack vector (data exfiltration, prompt injection from fetched content). Default to disabling.
- **Skills the operator hasn't read.** Skills are markdown. Read them. 100-300 lines apiece.

## Where skills live in the framework's mental model

Skills are operator-installed, not framework-bundled. Alfred ships zero skills by default. The consumer fleet picks. Keeps the framework pluralist (different fleets, different skill stacks) and small (no skill maintenance burden on the framework).

If a future skill becomes universally needed (e.g. a state-machine-aware skill that reads the agent claim labels), it lands in `examples/skills/` as a documented option, never as a default.
