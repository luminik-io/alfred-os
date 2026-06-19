---
title: Plain mode
description: A non-technical front door that lets anyone direct the fleet in plain language and approve outcomes, not code.
---

Plain mode lets someone who does not write code direct the Alfred fleet in plain
language. A teammate can describe a change the way they would say it out loud,
for example "make the signup button on the welcome screen green", and the
planning assistant turns that into the same structured work item the fleet
always runs on. The person never sees a spec, a repository name, a readiness score, or a pull
request. They describe an outcome, answer at most a question or two, and approve
a preview.

Source: [`lib/intake_profiles.py`](https://github.com/luminik-io/alfred-os/blob/main/lib/intake_profiles.py) and the full note in [`docs/PLAIN_MODE.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/PLAIN_MODE.md).

## The idea

**The agent does spec-driven development for you. You approve outcomes, not
code.**

Alfred's quality bar is unchanged: every piece of work still becomes a
structured draft with a problem statement, desired behavior, scope, checks, and
a verification plan, and it still passes every existing gate before anything
ships. Plain mode only changes *who has to think in those terms*. In the default
(technical) mode, you do. In plain mode, the assistant does that work
invisibly and talks to the person entirely in plain language.

## Turn it on

Set one environment variable on whatever surface is talking to the person:

```sh
export ALFRED_INTAKE_PROFILE=plain
```

- **Unset** (or any unrecognized value) keeps the original technical behavior,
  unchanged. A typo never silently downgrades the default technical mode into plain mode.
- **`plain`** switches the planning assistant's conversational surface to the
  non-technical front door.

The variable only affects two things:

1. **The clarifying-question persona.** When an LLM refiner is enabled, plain
   mode gives it a friendly, no-jargon persona. It asks at most one or two short,
   plain questions ("Which screen is this on?", "What color did you have in
   mind?") and never uses words like spec, acceptance criteria, repository,
   readiness, pull request, or diff.
2. **The user-facing summary.** Instead of an amendment count and a readiness
   verdict, the person sees a short plan:

   > Here's what I'll do:
   > - Make the signup button on the welcome screen green so it stands out.
   >
   > I'll put this together and show you a preview to look over before anything
   > goes live.
   >
   > OK to go ahead?

Everything else, the structured draft, readiness scoring, the GitHub-ready issue
body, the development spec, and the whole downstream bridge and fleet, is
identical in both modes. Plain mode is a thin strategy seam, not a separate code
path.

## Where it fits

Plain mode is the friendly entrance; the existing pieces do the rest.

- **[Slack listener](/concepts/slack-native-planning/).** A teammate DMs Alfred
  describing what they want. With plain mode on, Alfred replies with plain
  questions and a plain plan instead of internal commands and readiness scores.
- **[Alfred Desktop](/concepts/desktop-client/).** The Compose box becomes a
  plain-language intake.
- **The fleet.** Receives the exact same structured work it always has, and keeps
  every gate (claim-lock, spend caps, review, approval) intact.

The non-technical user approves an *outcome* and later reviews a *preview*. They
never touch code, specs, or GitHub.
