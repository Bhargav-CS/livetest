# Project Overview

livetest is a black-box testing tool for **any** LiveKit agent

- **Input:** only a token, or a way to get one.
- **No agent changes:** it never touches the agent's code and doesn't depend on the agent's telemetry.
- **Any host:** works on LiveKit Cloud and on self-hosted LiveKit servers.
- **Distribution:** a standalone tool installed from the GitHub README (sh / iex / winget),

The code is in [livetest/](livetest/), layered platform / runtime / integrations (see "Code structure" in the plan). Read these two first:

- [docs/livetest_plan.md](docs/livetest_plan.md): what we're building and its status. Tick the status boxes as features land.
- [docs/livekit_tester_research.md](docs/livekit_tester_research.md): the technical findings.

# Workflow

1. Never work in the `main` branch. Always work in a `feature/` or `bugfix/` branch. If you are on `main`, create another branch using git.
2. Decisions of every branch live in `decisions_x.md`, where x is the branch name after the last `/` (`feature/tester-prototype` → `decisions_tester-prototype.md`). Create one if it does not exist.
3. Context of every branch lives in `context_x.md` (same x). Create one if it does not exist.
4. Always focus on the simplest solution that works with the tools you have. Never invent something that isn't in the instructions, the stdlib, or the packages already used in the repo. Do the work the user asked for, nothing more unless explicitly cited.
5. All temp files, scratchpad executions and temp programs go to the `scratchpad` dir in the current repository. If the dir does not exist, create one.
6. The git hooks in `.git/hooks` enforce the branch docs:
   - `pre-commit` blocks committing `context_*.md` / `decisions_*.md`. They are local-only and never reach `main`.
   - `post-checkout` parks them in `.git/branch-docs/` on a branch switch and restores the ones matching the new branch.
7. **Until v0.1 is released, nobody has installed livetest: change things in place.** No migration steps, compatibility shims, renamed-flag aliases or "was / now" comments. Edit the schema, models, CLI and tests so they read as if they had always been this way. After a schema change, delete `~/.livetest/livetest.db` and set it up again.

# Context

1. This is where the context of each branch lives, so it is preserved in every chat without having to load it again and again.
2. You must update this regularly with each request. Use a log format in descending order (latest first), with the date added.
3. When updating, focus on what you did and give a summary only. Don't include anything that is already clear from the diff or other references. This must be kept as the SSOT for context and be easy to understand from a third-person perspective.
4. You may also add your scratchpad here under a heading `scratchpad`, so that you, your subagents and other chats can record and use your thought process.

# Decisions

1. This is where the important decisions made along the way live.
2. You must update this regularly with each decision made. Use a log format in descending order (latest first), with the date added.
3. When updating, focus on decisions that matter for achieving a goal, major architectural changes, or something the user specifically asked for. This must be kept as the SSOT and be easy to understand from a third-person perspective.

# Skills

/ponytail - ultra mode, use eveytime you are writing any code 

# Subagents
