---
title: Claude Code skills
description: Recommended skill set for an autonomous engineering fleet, install commands, per-agent matrix.
---

Skills are small bundles (markdown + optional scripts) that extend Claude Code's tool surface. Alfred-OS doesn't ship skills itself; consumer agents pick what they need.

Full guide at [`docs/SKILLS.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/SKILLS.md). Highlights:

## Where they live

```
~/.claude/skills/
├── code-review/SKILL.md
├── code-review-and-quality/SKILL.md
├── debugging-and-error-recovery/SKILL.md
├── frontend-ui-engineering/SKILL.md
├── security-and-hardening/SKILL.md
├── spec-driven-development/SKILL.md
├── autofix/SKILL.md
└── gstack/                  # gstack tap installs as a directory of subskills
    ├── browse/
    ├── investigate/
    ├── qa/
    ├── review/
    └── ship/
```

## Recommended set for an autonomous engineering fleet

| Skill | Source | Used by | Why |
|---|---|---|---|
| `spec-driven-development` | Anthropic | feature-dev | Forces the model to derive code from a written spec |
| `code-review-and-quality` | Anthropic | feature-dev (self-check), reviewer | Multi-axis review |
| `security-and-hardening` | Anthropic | feature-dev (auth), reviewer | Security-specific lens |
| `debugging-and-error-recovery` | Anthropic | bug-triage, monitoring | Systematic root-cause path |
| `frontend-ui-engineering` | Anthropic | feature-dev (frontend) | Component patterns |
| `code-review` | CodeRabbit | reviewer | Backbone for structured review |
| `autofix` | CodeRabbit | review-fix | Apply CodeRabbit P0/P1 fixes with per-change approval |
| `/review`, `/ship`, `/qa`, `/browse`, `/investigate` | gstack | various | gstack's CLI-first review/ship/QA flow |

## Install

```sh
# Anthropic official
git clone --depth 1 https://github.com/anthropics/claude-code.git /tmp/cc
cp -R /tmp/cc/skills/* ~/.claude/skills/
rm -rf /tmp/cc

# gstack
git clone https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
(cd ~/.claude/skills/gstack && ./setup)

# CodeRabbit
npx -y skills add coderabbitai/skills --global --yes \
    --agent claude-code --skill '*'
```

For a single fresh-install script, see [`docs/SKILLS.md#skill-install-automation`](https://github.com/luminik-io/alfred-os/blob/main/docs/SKILLS.md#skill-install-automation).

## Security note

Skills run with the same permissions as `claude`. They can read/write files in the agent's worktree, run shell commands, invoke tools. Treat any new skill the way you'd treat any other dependency:

1. Read the `SKILL.md`.
2. Skim the scripts the skill might invoke.
3. Run a Snyk / CodeQL scan on unfamiliar sources.
4. Pin to a specific commit when installing from a third-party tap.

The fleet's IAM-per-agent + per-firing-worktree-isolation patterns limit blast radius (a malicious skill in the Lucius worktree can't reach the operator's home or the secondary Claude account). Mitigations, not prevention.

## Anti-recommendations

- **Anything that auto-publishes** (auto-tweet, auto-deploy, auto-merge). Use as draft-then-review only.
- **Skills that fork to the network without explicit allowlists.** Network egress from a worktree is a known agent attack vector.
- **Skills the operator hasn't read.** Skills are markdown. Read them.

## Where skills live in the framework's mental model

Skills are operator-installed, not framework-bundled. Alfred-OS ships zero skills by default. Consumer fleets pick. Keeps the framework pluralist (different fleets, different stacks) and small (no skill maintenance burden on us).
