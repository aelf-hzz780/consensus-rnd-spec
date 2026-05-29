---
name: consensus-rnd-spec
description: Use when the user wants an unattended Consensus R&D loop that auto-detects Spec Kitty repositories, routes discovered work into Spec Kitty missions and WPs when available, and falls back to the native consensus-rnd/codex-refactor-loop backend for non-Spec-Kitty repositories.
---

# Consensus R&D Spec

This skill is the controller contract for `consensus-rnd-spec`: a backend-selecting Consensus R&D loop.

## Controller Rule

Always load host configuration from `.consensus-rnd-spec/host.env` when it exists. If absent, use safe defaults and fail closed for mutating actions. The controller may inspect repository state, count in-flight workers, and prepare dispatch plans. It must not perform GitHub lifecycle mutations or Spec Kitty lane transitions unless the selected backend and host config explicitly allow them.

## Backend Strategy

Use `scripts/detect_backend.py --repo <repo>` before every loop turn.

- `spec-kitty`: selected when the repository has Spec Kitty project signals and `spec-kitty` is callable. Discovery work is converted into Spec Kitty mission input. Spec Kitty owns mission state, worktrees, implementation, review, merge, and acceptance.
- `native`: selected for non-Spec-Kitty repositories. Native full-loop execution requires `NATIVE_FULL_LOOP_ENABLE=true` and a valid `NATIVE_CONSENSUS_SKILL_ROOT`; otherwise the controller reports a blocked plan.
- `auto`: default `BACKEND_MODE`; chooses `spec-kitty` when available, otherwise `native`.

This is a Strategy Pattern boundary. The controller owns scheduling, wakeups, floor checks, status, and backend choice; each backend owns its workflow semantics.

## Loop Contract

For `/loop <duration>` or an unattended run:

1. Source host env if present.
2. Run backend detection.
3. Check `CODEX_FLOOR` against this loop's in-flight workers.
4. Reload this skill contract by reading the installed `SKILL.md` before dispatch.
5. Process pending artifacts/events.
6. Dispatch backend work only through the selected backend adapter.
7. Write a concise status event to `.consensus-rnd-spec/state/loop-events.jsonl`.

`Human: ...` synthetic prompts are allowed only as `synthetic_human_intake`. They must never be treated as maintainer approval, GitHub comment authorship, or permission to bypass Spec Kitty gates.

Use `intake.py` for slash-style prompts such as `/loop 10min /codex-refactor-loop ...`. It parses the duration and compatibility surface, then records the remaining prompt as `synthetic_human_intake` for Spec Kitty repositories. Use `run_loop.py` as the lower-level executable loop surface. Default mode is dry-run planning; `--execute` is required before it writes intake seeds, starts Spec Kitty actions, native delegation, or Codex workers.

## Spec Kitty Backend

When `spec-kitty` is selected:

- Create new work through `spec-kitty specify --mission-type software-dev --json` or the current project-approved Spec Kitty creation flow.
- Advance through `spec-kitty next --agent <agent> --mission <slug> --json`.
- Use `spec-kitty orchestrator-api` for external automation state transitions when needed.
- If no mission has actionable WP work, promote the latest unpromoted `.consensus-rnd-spec/runs/discovery-*.json` into a Spec Kitty mission seed; if no discovery artifact exists, run the discovery producer first.
- Record source metadata in mission artifacts: `source=consensus-rnd-spec`, source issue number when present, audit artifact path, evidence hash, and synthetic intake marker when applicable. After successful `spec-kitty specify`, write the promoted seed to `<mission>/consensus-rnd/intake.md` and `<mission>/consensus-rnd/intake.json`, and attach `consensus_rnd_spec` metadata in `<mission>/meta.json`.
- Do not directly edit WP frontmatter or create worktrees outside Spec Kitty.

## Native Backend

When `native` is selected:

- Require `NATIVE_FULL_LOOP_ENABLE=true`.
- Require `NATIVE_CONSENSUS_SKILL_ROOT` to point at an installed `codex-refactor-loop` skill root.
- Delegate to the native controller through its `scripts/spawn-codex.sh`; do not duplicate its lifecycle logic.
- Preserve the host repository's push rules: never push to `main` or `master`; target remote and branch must be explicit.

## Commands

- Detect backend:
  `python3 <skill-root>/scripts/detect_backend.py --repo "$REPO_ROOT"`
- Plan one loop turn:
  `python3 <skill-root>/scripts/loop_check.py --repo "$REPO_ROOT"`
- Parse and run a slash-style intake:
  `python3 <skill-root>/scripts/intake.py --repo "$REPO_ROOT" --text "/loop 10min /codex-refactor-loop <goal>" --run`
- Run loop controller:
  `python3 <skill-root>/scripts/run_loop.py --repo "$REPO_ROOT" --duration 10min`
- Produce discovery artifact:
  `python3 <skill-root>/scripts/discovery.py --repo "$REPO_ROOT"`
- Promote discovery to Spec Kitty mission:
  `python3 <skill-root>/scripts/promote_discovery.py --repo "$REPO_ROOT" --execute`
- Plan Spec Kitty handoff:
  `python3 <skill-root>/scripts/spec_backend.py plan --repo "$REPO_ROOT" --title "<title>"`
- Check native fallback:
  `bash <skill-root>/scripts/native_backend.sh plan`
