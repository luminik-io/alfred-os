# AI-Assisted Install

This guide is for installing Alfred with Claude Code, Codex, or another local
coding assistant driving the terminal.

Alfred is friendly to AI-assisted setup, but the assistant needs clear
boundaries. Pick one setup lane first:

| Lane | Use it when | Config shape |
|---|---|---|
| One repo | You have one app or one library. | `--agents all --repos owner/repo` |
| Multi-repo product | You have backend/frontend/mobile/packages. | `--agents all --repos owner/api,owner/web,owner/mobile` |
| Specs-led workspace | You keep specs/roadmap separate from code repos. | Put specs under the workspace for context; assign implementers to code repos. |
| Batman planning | You want cross-repo bundle plans. | Included in the full fleet; use the same multi-repo `--repos` list. |

The safe first path is explicit:

1. Run the dry-run example.
2. Install prerequisites.
3. Let the human complete interactive auth flows.
4. Configure explicit repos with the full fleet.
5. Run doctor and show the final state.

Do not ask an assistant to guess your GitHub org, Slack webhook, AWS profile, or
which repos should receive scheduled agents.

For the local checkout shape, read [`WORKSPACE_PATTERNS.md`](WORKSPACE_PATTERNS.md).

## Before You Paste a Prompt

Know these values:

| Value | Example | Notes |
|---|---|---|
| GitHub org/user | `my-org` | Must match the owner of the repo Alfred will operate on. |
| Repos | `my-api,my-web` or `my-org/my-api,my-org/my-web` | Use one repo for a one-repo product, or a comma-separated list for a multi-repo product. Start with the smallest honest scope you want agents to operate on. |
| Specs repo | `my-specs` | Optional. Keep it checked out for context; do not assign write-capable agents to it unless you want them editing specs. |
| Operator name | `Jane Builder` | Used in generated prompts and config. |
| Operator email | `jane@example.com` | Used in generated prompts and config. |
| Slack webhook | `skip` or a real webhook URL | `skip` is safe; Alfred logs locally without Slack. |

The local assistant may need you to complete browser-based auth for:

- `gh auth login`
- `claude` first-run auth
- optional `codex` first-run auth if you want Codex as an Alfred engine

## Copy-Paste Prompt

Paste this into Claude Code, Codex, or your local coding assistant. Replace the
values in the first block.

```text
Please install Alfred for a local agent fleet.

Values:
- GH_ORG=<your-github-org-or-user>
- REPOS=<comma-separated-repos-owned-by-GH_ORG>
- SPECS_REPO=<optional-specs-repo-or-blank>
- OPERATOR_NAME=<your-name>
- OPERATOR_EMAIL=<your-email>
- SLACK_WEBHOOK=skip
- INSTALL_DIR=~/code/alfred-os
- WORKSPACE_ROOT=~/code

Rules:
- Do not invent secrets, tokens, webhooks, AWS profiles, or repo names.
- Do not enable every discovered repo. Configure only the repos listed in REPOS.
- Keep Slack skipped unless I paste a webhook.
- Keep AWS optional; do not create IAM users or profiles during this install.
- Keep ANTHROPIC_API_KEY and OPENAI_API_KEY unset unless I explicitly ask for API billing.
- Use the full engineering fleet: Drake, Batman, Lucius, Ra's al Ghul, Bane,
  Nightwing, Robin, Huntress, Gordon, automerge, cleanup, code-map refresh,
  briefs, recaps, shipped summaries, and fleet doctor where available.
- Keep Batman configured even for a one-repo install. It will only act when
  cross-repo or parent-plan work exists, but users should not have to discover
  and add the architect later.
- If SPECS_REPO is set, clone it under the workspace for context, but do not assign Lucius/Nightwing write loops to it unless I explicitly ask.
- Before running any command that loads scheduled agents, show me the command and ask for confirmation.
- If an interactive browser auth step is needed, stop and tell me exactly what to run.
- At the end, show the Alfred repo path, ~/.alfredrc Alfred block, `alfred agents`, `alfred auth status`, and doctor output.

Steps:
0. Set shell variables from the values block:
   export GH_ORG="<your-github-org-or-user>"
   export REPOS="<comma-separated-repos-owned-by-GH_ORG>"
   export SPECS_REPO=""
   export OPERATOR_NAME="<your-name>"
   export OPERATOR_EMAIL="<your-email>"
   export SLACK_WEBHOOK="skip"
   export INSTALL_DIR=~/code/alfred-os
   export WORKSPACE_ROOT=~/code

1. Clone or update Alfred:
   if [ ! -d "$INSTALL_DIR/.git" ]; then git clone https://github.com/luminik-io/alfred-os.git "$INSTALL_DIR"; fi
   cd "$INSTALL_DIR"
   git fetch --all --prune
   git checkout main
   git pull --ff-only

2. If SPECS_REPO is set, clone or update it for read-only planning context:
   mkdir -p "$WORKSPACE_ROOT/product"
   if [ -n "$SPECS_REPO" ]; then
     SPECS_NAME="${SPECS_REPO##*/}"
     SPECS_NAME="${SPECS_NAME%.git}"
     if [ ! -d "$WORKSPACE_ROOT/product/$SPECS_NAME/.git" ]; then
       git clone "https://github.com/$SPECS_REPO.git" "$WORKSPACE_ROOT/product/$SPECS_NAME"
     else
       git -C "$WORKSPACE_ROOT/product/$SPECS_NAME" fetch --all --prune
       git -C "$WORKSPACE_ROOT/product/$SPECS_NAME" checkout main || true
       git -C "$WORKSPACE_ROOT/product/$SPECS_NAME" pull --ff-only || true
     fi
   fi

3. Show the dry-run lifecycle:
   PYTHONPATH=lib python3 examples/bin/echo_summarise.py --dry-run

4. Install prerequisites:
   ALFRED_NONINTERACTIVE=1 GH_ORG="$GH_ORG" OPERATOR_NAME="$OPERATOR_NAME" OPERATOR_EMAIL="$OPERATOR_EMAIL" bash install.sh
   . ~/.alfredrc

5. Check auth and pause for me if login is needed:
   gh auth status || { echo "GitHub auth needed. Run: gh auth login --hostname github.com --git-protocol https --web"; exit 1; }
   claude --version || true
   echo "If Claude Code is not authenticated yet, run: claude"

6. After GitHub and Claude Code auth are working, configure the full fleet:
   ./bin/alfred-init.py --non-interactive --agents all --repos "$REPOS" --slack-webhook "$SLACK_WEBHOOK"

7. If SPECS_REPO is set, show me the specs checkout path and remind me to add it to `~/.alfred/prompts/drake.md` as planning context. Do not add it to `--repos` unless I ask.

8. Verify:
   ~/.local/bin/alfred agents
   ~/.local/bin/alfred auth status
   bash bin/doctor.sh
```

## Claude Code vs Codex as the Installer

Using Claude Code or Codex to install Alfred is different from using Claude
Code or Codex as Alfred's agent engine.

| Surface | What it means |
|---|---|
| Claude Code as installer | Claude Code drives your terminal to clone, install, configure, and verify Alfred. |
| Codex as installer | Codex drives your terminal to run the same install commands. |
| Claude Code as Alfred engine | Scheduled Alfred agents call `claude -p` for work. This is the default. |
| Codex as Alfred engine | Scheduled Alfred agents call `codex exec` for a codename set to `codex` or `hybrid`. Optional. |

After install, check engine readiness with:

```sh
alfred auth status
alfred codex status
alfred codex probe
```

If Codex is not installed or authenticated, Alfred can still run Claude-backed
agents. Codex is optional.

## OAuth Token Setup Needs a Real Terminal

`alfred setup-token` wraps `claude setup-token`, which uses Ink (the
React-for-CLI library) and requires a real TTY. AI assistants and other
non-TTY contexts (CI, automation, Claude Code subprocess sessions) cannot
spawn it directly: Ink crashes with `Raw mode is not supported on the
current process.stdin` and the process hangs until killed.

Two supported paths from an AI-assisted install:

1. Ask the operator to open their own terminal and run `alfred setup-token`
   once. The script writes `CLAUDE_CODE_OAUTH_TOKEN` to `~/.alfredrc` and
   exits; the assistant can resume from there.
2. Use the paste-back flow: the operator runs `claude setup-token` in
   their shell, copies the printed `sk-ant-oat...` token, and pastes it
   back to the assistant. The assistant then runs:

   ```sh
   alfred setup-token --token sk-ant-oat01-<...>
   ```

   `--token <value>` skips the Ink spawn entirely and writes the token
   straight to `~/.alfredrc` with the same 0600 perms as the interactive
   path. Re-run with `--force` to rotate later.

When the script is invoked without a TTY and without `--token`, it
detects the non-TTY context, refuses to spawn, and prints the two paths
above. No Ink stack trace.

## Safer First Run

For the first real install, prefer explicit repos:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos "$REPOS" \
  --slack-webhook skip
```

That command:

- seeds fleet prompts into `~/.alfred/prompts/`
- creates standard GitHub labels on the selected repo
- writes `launchd/agents.conf`, the shared scheduler manifest
- updates `~/.alfredrc`
- deploys scheduler units for the selected fleet
- runs doctor

It does not create AWS profiles, create Slack apps, or configure every repo on
the machine. `--repos` may be one repo or a comma-separated list. All selected
repo-operating agents receive that same repo list, and `alfred-init.py`
creates the standard GitHub labels on every selected repo.

Batman is part of the full fleet. It coordinates `agent:large-feature` and
`agent:bundle:<slug>` issues across repos and remains bounded by its normal
approval gates.

## Multi-Repo Setup

Use the same command with a comma-separated repo list:

```sh
export REPOS="my-org/api,my-org/web,my-org/mobile"
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos "$REPOS" \
  --slack-webhook skip
```

This writes per-agent repo env vars such as:

```sh
ALFRED_DRAKE_REPOS=api,web,mobile
ALFRED_LUCIUS_REPOS=api,web,mobile
ALFRED_RASALGHUL_REPOS=api,web,mobile
```

Alfred expects those local checkouts under `$WORKSPACE_ROOT/product/<repo>`.
For example, with `WORKSPACE_ROOT=~/code`, clone code repos into
`~/code/product/api`, `~/code/product/web`, and `~/code/product/mobile`.

## Specs-Led Setup

If you keep specs or roadmap in a separate repo, clone it into the workspace but
keep the first scheduled write loop focused on code repos:

```text
~/code/
  alfred-os/
  product/
    api/
    web/
    mobile/
    specs/
```

Use `--repos my-org/api,my-org/web,my-org/mobile` for the full fleet. Then
edit prompts in `~/.alfred/prompts/` to point Drake and reviewers at the specs
checkout for context. Only add the specs repo to `--repos` if you want the
fleet to create labels and potentially operate on specs issues too.

## Batman Planning

Batman is the OSS architect agent for cross-repo work. It is included in Alfred
and supports two public paths:

- `BATMAN_PARENT_REPO` parent issues can go through plan, approval, child-issue
  filing, and status reporting.
- `BATMAN_SCAN_REPOS` legacy scans pick open `agent:large-feature` issues,
  group siblings with the same `agent:bundle:<slug>` label, post a rollout plan,
  and stop before child issue filing.

Batman owns the feature shape above the repo-local work. It plans the rollout
and files scoped child issues for the normal fleet queue when the gate allows
it.

For an explicit multi-repo setup:

```sh
export REPOS="my-org/api,my-org/web,my-org/mobile"
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos "$REPOS" \
  --slack-webhook skip
```

Batman is configured by that full-fleet install. It remains protected by the
runner gate until you arm it:

```sh
alfred enable batman
# Ensure ~/.alfredrc has BATMAN_SCAN_REPOS=api,web,mobile, then redeploy.
bash deploy.sh
bash bin/doctor.sh
```

## Expected Success Shape

After a clean install:

```sh
alfred agents
alfred auth status
bash bin/doctor.sh
```

Expected:

- `alfred agents` lists the full engineering fleet, including gated agents.
- `alfred auth status` shows Claude Code account routing and Codex status if
  Codex is installed.
- `doctor.sh` reports the configured agents as pass, or names the exact missing
  binary/auth/env var before any agent burns model turns.

## Common Assistant Mistakes

- **Assigning every repo on the machine.** Use an explicit `--repos` list.
- **Putting specs into the write loop by accident.** Clone specs for context;
  only pass it in `--repos` when you want agents operating there.
- **Skipping prompt setup.** Current `alfred-init.py` copies prompt templates;
  do not manually copy old prompt snippets unless you are customizing them.
- **Treating Slack as required.** It is optional. Use `--slack-webhook skip`.
- **Confusing assistant auth with Alfred engine auth.** The assistant can
  install Alfred even if Codex is not configured as an Alfred engine.
- **Continuing after failed auth.** Run `alfred auth status` and fix auth before
  scheduled agents start firing.

## Adding Slack, AWS, or More Repos Later

Start with the smallest honest repo set. Once the full fleet passes doctor:

- Add Slack: [`SLACK_SETUP.md`](SLACK_SETUP.md)
- Add AWS IAM-per-agent: [`AWS_SETUP.md`](AWS_SETUP.md)
- Add Codex engine routing: [`CODEX_PROVIDER.md`](CODEX_PROVIDER.md)
- Add more repos by rerunning `alfred-init.py` with explicit `--repos`, or by
  editing `~/.alfredrc` and redeploying.
- Add workspace structure: [`WORKSPACE_PATTERNS.md`](WORKSPACE_PATTERNS.md)
