# PR #1 Gmail draft remediation plan

## Goal
Ship a minimal, production-ready remediation for PR #1 (`codex/gmail-draft-lifecycle-tools`) that fixes the verified `update_gmail_draft` preservation defects and removes bundled-skill contract drift without widening scope beyond the Gmail draft lifecycle slice.

## Source of truth
- `AGENTS.md`
- `README.md`
- `gmail/gmail_tools.py`
- `tests/gmail/test_draft_gmail_message.py`
- `skills/managing-google-workspace/SKILL.md`
- `skills/managing-google-workspace/references/gmail.md`
- Live PR head: `4e03fb78c4f86e317ed3e2e072cae45aa404e740`
- Checkout path: `/home/prop_/projects/fork_google_workspace_mcp_pr1_review`
- Trusted base: `main` at `5cf43d1803ce4f8dd78e40dbce26b73dd937b8c9`
- Conflict rule: runtime/tests beat review-bot wording; repo auth/doc-contract rules beat convenience

## Scope in
- `update_gmail_draft` preservation semantics for omitted `from_name`
- `update_gmail_draft` preservation semantics for omitted existing inline/attachment MIME parts
- Regression tests proving both behaviors
- Bundled Gmail skill/reference docs for the new draft lifecycle tools

## Scope out
- New Gmail features or API surface changes beyond the existing PR
- Packaging/manifest/Helm/Docker changes
- Auth-mode, permission-scope, or transport behavior changes
- Unrelated Gmail cleanup or refactors outside the draft remediation path

## Non-goals
- Re-architecting all Gmail MIME construction
- Adding new evaluation suites unless the existing evaluation bundle already represents this contract
- Changing the README example again unless required by the runtime fix (currently not required)

## Delivery map
- PR count: 1 (existing PR #1 only)
- PR order: current PR head only
- Branch: `pr-1` worktree attached to the live PR head
- Active stack depth: 0
- Owner slice: Gmail draft update preservation + bundled skill/reference sync
- Regroup rule: if the smallest safe runtime fix requires broader MIME-system redesign, stop and refresh this plan before continuing

## Commit structure
1. Regression tests that fail against current PR head
2. Minimal runtime fix in `gmail/gmail_tools.py`
3. Bundled Gmail skill/reference updates in the same pass if the public contract remains as implemented

## Verification
- RED: targeted failing Gmail regression tests for omitted `from_name` preservation and omitted inline attachment preservation
- GREEN: rerun the targeted tests after the fix
- Repo gates:
  - `uv sync --group dev`
  - `uv run ruff check .`
  - `uv run pytest`
- Final affected-surface check via GitNexus impact/context plus live-worktree `git diff` because `detect_changes` cannot target the unindexed PR review worktree

## Affected surface
- Changed behavior boundary: `update_gmail_draft` reconstructs an existing draft while promising to preserve omitted fields and omitted attachments
- Direct runtime surface: `gmail/gmail_tools.py` draft update flow and shared message-preparation helper behavior
- Adjacent no-change surfaces:
  - `draft_gmail_message` behavior must remain unchanged
  - `send_gmail_message` attachment handling must remain unchanged
  - tool registration/schema expectations in Gmail tests must remain correct
- Coupled contract surface:
  - `skills/managing-google-workspace/SKILL.md`
  - `skills/managing-google-workspace/references/gmail.md`

## Authoritative contract
- Omitting `from_name` on `update_gmail_draft` preserves the existing draft display name rather than dropping it
- Omitting `attachments` on `update_gmail_draft` preserves existing attachment/inline MIME parts rather than silently discarding them
- Bundled skill/reference docs must list the draft lifecycle tools that the runtime exposes

## Invariants
- `draft_gmail_message` still creates drafts with current attachment/signature/reply behavior
- `send_gmail_message` still uses the existing attachment path unchanged
- `update_gmail_draft` still requires `draft_id` and compose scope only
- Protected runtime/secret paths remain untouched

## Proof plan
- Focused regression tests for the two verified defects
- Full repo lint and test gates after implementation
- GitNexus affected-surface confirmation before completion

## Mutable vs protected paths
Mutable this pass:
- `gmail/gmail_tools.py`
- `tests/gmail/test_draft_gmail_message.py`
- `skills/managing-google-workspace/SKILL.md`
- `skills/managing-google-workspace/references/gmail.md`
- `docs/plans/2026-04-16-pr1-gmail-draft-remediation.md`

Protected / must remain untouched:
- repo-root `.env`
- `client_secret.json`
- `~/.google_workspace_mcp/credentials`
- any live `GOOGLE_MCP_CREDENTIALS_DIR`
- any live `WORKSPACE_ATTACHMENT_DIR`
- packaging/deployment metadata surfaces

## Checklist
- [x] Verify live PR checkout path, branch attachment, and head SHA
- [x] Confirm affected runtime surface and shared helper blast radius
- [x] Write failing regression tests for omitted `from_name` preservation
- [x] Write failing regression tests for omitted inline attachment preservation
- [x] Implement the smallest runtime fix that satisfies the verified contract
- [x] Update bundled Gmail skill/reference docs for the draft lifecycle tools
- [x] Run `uv sync --group dev`
- [x] Run `uv run ruff check .`
- [x] Run `uv run pytest`
- [x] Final affected-surface check via GitNexus impact/context plus live-worktree `git diff`
- [x] Summarize outcomes and remaining blockers, if any

## Risks / blockers
- Preserving inline MIME parts can regress `send_gmail_message` if the shared attachment path is widened carelessly
- If the existing message parser cannot distinguish body parts from preserved inline assets safely, fail closed and refresh plan scope before broadening implementation

## Execution handoff
Use this governing artifact: `docs/plans/2026-04-16-pr1-gmail-draft-remediation.md`.
Do not create a new plan. Do not re-plan this pass.
Use `production-preflight` before tracked edits and `production-code` before calling the pass complete.
Re-walk the affected Gmail draft preservation surface before edits and again before completion.
Keep the checklist current.
Do not touch protected secret/runtime paths or packaging/deployment metadata in this pass.
