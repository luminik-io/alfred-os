## Summary

<!-- One paragraph: what changes, why. Link the issue this resolves. -->

## Type of change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to change)
- [ ] Documentation only

## Design alignment

<!-- Alfred is deliberately small. Confirm your change fits the constraints in CONTRIBUTING.md. -->

- [ ] My change does NOT broaden scope (no multi-tenant, no web UI, no hosted-service patterns).
- [ ] My change does NOT add a runtime dependency that's not already in `pyproject.toml` (or I justify it in the PR body).
- [ ] My change is testable. I added tests under `tests/` for new behaviour.
- [ ] If this change affects an operator-facing flow (`install.sh`, `bin/`, `examples/bin/`), I updated the relevant doc.

## Verification

<!-- Reviewer needs to know what you ran. -->

- [ ] `uv run --with pytest pytest tests/` — all green
- [ ] `bash bin/doctor.sh` — passes locally
- [ ] `ruff check .` — no new violations
- [ ] `bash bin/scrub-check.sh` — clean
- [ ] (If shell change) `shellcheck <file>` — clean

## Screenshots / output

<!-- For UI / CLI / Slack-message changes, paste before/after. -->

## Checklist

- [ ] I read [`CONTRIBUTING.md`](../CONTRIBUTING.md).
- [ ] My commits are signed-off (`git commit -s`) if you're contributing under DCO.
- [ ] I updated `CHANGELOG.md` under `[Unreleased]`.
- [ ] I added a doc update if the change is operator-visible.

🤖 If this PR was generated with assistance, mention which tool in the body.
