# Gmail draft update simplicity redesign

Date: 2026-04-18
Repo: `~/projects/fork_google_workspace_mcp`
Status: proposed
Governing artifact type: implementation plan

## Goal

Redesign `update_gmail_draft` so draft updates stay production-safe without the current preserve-extract-rebuild cascade.

The target is a simplicity-first update path that edits the existing draft MIME tree directly, preserves untouched attachment structure, and keeps the public Gmail tool surface stable.

## Source of truth

Primary authorities for this plan, in priority order:
- `AGENTS.md`
- `gmail/gmail_tools.py`
- `tests/gmail/test_draft_gmail_message.py`
- `tests/gmail/test_modify_gmail_message_labels_schema.py`
- `skills/managing-google-workspace/references/gmail.md`
- PR 9 live review surface on branch `gmail-draft-lifecycle-clean-pr9`

Trusted base:
- `main`

Current PR context:
- PR: `#9`
- branch: `gmail-draft-lifecycle-clean-pr9`
- checkout: `/home/prop_/projects/fork_google_workspace_mcp`
- current live head when this plan was written: `58f93cb0ae7f8effaa0516ead0c4ae7c5e33c7a4`
- checkout state: branch-attached local branch `pr-9`

Conflict rule:
- runtime code + tests beat bot review wording
- this plan beats incremental review-comment patching once approved
- do not widen scope to satisfy non-contract nits

## Scope in

- redesign internal behavior of `update_gmail_draft`
- replace the current omitted-attachments preservation strategy
- keep `update_gmail_draft` public tool name, scope, and broad purpose unchanged
- update only directly coupled tests and user-facing Gmail reference docs if the contract wording changes

## Scope out

- no redesign of `draft_gmail_message`
- no redesign of `send_gmail_message`
- no auth, packaging, manifest, Docker, Helm, or transport changes
- no new dependency or MIME framework
- no broad cleanup of unrelated Gmail helpers
- no attempt to satisfy every bot nit outside the chosen contract

## Non-goals

- byte-for-byte preservation of untouched raw MIME serialization
- general-purpose MIME editing utilities for the whole repo
- adding new Gmail draft features beyond the existing tool contract
- splitting the work into stacked PRs

## Delivery map

PR count: 1

Owner slice:
- one focused PR for `update_gmail_draft` runtime behavior, directly coupled tests, and directly coupled Gmail reference docs

Classification:
- runtime: `gmail/gmail_tools.py`
- proof/tests/docs: `tests/gmail/test_draft_gmail_message.py`, `tests/gmail/test_modify_gmail_message_labels_schema.py`, `skills/managing-google-workspace/references/gmail.md`

Stack depth:
- `0`

Branch strategy:
- do not continue comment-by-comment patching on top of a noisy branch state
- implement the redesign in one clean pass on the active PR branch or a clearly superseding branch, but keep review ownership in one place

Active branch order:
1. `gmail-draft-lifecycle-clean-pr9` redesign pass

Consolidation trigger:
- if the redesign starts requiring shared create/send helper rewrites or broad MIME abstractions, stop and split the work into a new governing plan instead of smuggling scope expansion into this pass

Stop condition:
- if the work can no longer stay inside `update_gmail_draft` + coupled proof/docs, consolidate and re-plan instead of stacking micro-fixes

## Commit structure

Preferred commit shape:
1. runtime redesign in `gmail/gmail_tools.py`
2. coupled tests + docs updates if needed

If the diff remains small enough, squash into one commit before merge.

## Recommended design

### Core decision

Replace the current update flow's omitted-attachment preserve/rebuild behavior with a direct MIME-tree mutation model.

Do not extract preserved attachments into ad-hoc dict payloads and then reattach them through `_prepare_gmail_message`.

### Public contract to preserve

Keep these tool-level expectations:
- `draft_id`, `subject`, and `body` remain required
- `to`, `cc`, `bcc`, `from_name`, `from_email`, `thread_id`, `in_reply_to`, `references`, and `attachments` continue to support preserve-on-omit semantics
- empty string continues to mean clear for supported string fields
- empty list continues to mean clear for attachments
- `include_signature` and `quote_original` remain available
- `GMAIL_COMPOSE_SCOPE` remains unchanged

### Internal contract to simplify

1. Always fetch and parse the existing draft raw MIME at update time.
2. Omitted fields preserve current values from the parsed draft.
3. Header mutations are applied directly to the parsed message rather than rebuilding the full message from semantic attachment snapshots.
4. Untouched attachment and related-part MIME subtrees remain in the parsed message unless the caller explicitly clears or replaces attachments.
5. The redesign preserves semantic MIME structure for untouched parts, not exact original byte serialization.

### Exact behavior decisions

#### Subject and body
- `subject` and `body` stay required to avoid public API churn.
- The update pass always replaces the message body content.
- If `body_format` is explicitly provided, use it.
- If `body_format` is omitted, infer the replacement format from the existing draft body:
  - if the existing draft has an HTML body, replace as HTML
  - otherwise replace as plain text
- When replacing an HTML body, regenerate the plain-text fallback using the existing HTML-to-text helper.
- Keep the body-replacement helper narrow and subtree-aware: it should update the HTML body and the plain-text fallback without turning into a general MIME rewrite framework.
- If body replacement starts needing broad multipart normalization or reusable tree-rebuild abstractions, stop and re-scope instead of growing the helper further.

#### Headers
- Apply direct header mutation for `To`, `Cc`, `Bcc`, `From`, `In-Reply-To`, and `References`.
- Omitted values preserve current header values.
- Empty strings clear the corresponding header or request-body thread binding where applicable.
- Preserve the current `Re:` behavior only if it is still required by the documented contract; if not required, do not add new subject-munging logic beyond current behavior.

#### Thread binding
- Preserve existing Gmail `threadId` when `thread_id` is omitted.
- Remove `threadId` from the update request body when `thread_id` is explicitly cleared.
- Do not silently keep a cleared thread binding in the request body.

#### Attachments
Treat `attachments` as a strict tri-state input:
- `attachments is None`:
  - preserve existing non-body MIME parts in place
  - do not run preservation extraction logic
- `attachments == []`:
  - remove all existing non-body attachment and related-part MIME subtrees
- `attachments` supplied with entries:
  - remove existing non-body attachment and related-part MIME subtrees
  - resolve and attach the supplied set using the existing URL/path/base64 attachment resolution path

For this redesign, inline related parts count as part of the attachment surface for explicit clear/replace. This avoids fuzzy hybrid states.

#### Signature and quoted replies
- `include_signature` and `quote_original` apply only during body regeneration.
- They must not force extraction/rebuild of untouched attachments.
- Keep using the existing body-composition helpers where safe, but do not let shared create/send concerns drag update back into full-message reconstruction.

#### Failure handling
- missing raw MIME or malformed raw MIME fails closed with `UserInputError`
- no partial update when parse or required mutation steps fail
- attachment-resolution failures retain the existing fail-closed/user-facing behavior expected by the current tool contract

## Affected surface

Changed boundary:
- Gmail draft update mutation path in `update_gmail_draft`

Adjacent consumers/dependents that must remain correct:
- tool schema generation for `update_gmail_draft`
- Gmail reference docs for the update contract
- update-time attachment resolution path for supplied URL/path/base64 attachments
- current reply/thread/signature behavior relied on by the update path

No-change surfaces that still need proof:
- `draft_gmail_message`
- `send_gmail_message`
- `delete_gmail_draft`
- Gmail tool registration and scope metadata

## Authoritative contract

After the redesign:
- omitted update fields preserve current draft state
- omitted attachments preserve existing MIME attachment structure semantically
- explicit attachment clear/replace is deterministic
- HTML updates preserve HTML body behavior without flattening to plain text when `body_format` is omitted
- multipart attachments remain multipart MIME trees rather than flattened raw-byte payloads
- malformed raw MIME fails closed instead of mutating a partially understood draft

## Invariants

- untouched multipart attachments remain real multipart MIME entities after update
- untouched `message/rfc822` attachments remain valid forwarded-message attachments
- preserved CID-related parts remain inline when still referenced
- HTML body replacement regenerates a plain-text fallback
- explicit `thread_id=""` removes request-body thread binding
- create/send paths behave the same before and after if their code is untouched
- no secret/runtime paths outside source-controlled implementation files are mutated

## Proof plan

### Focused tests required

Update or add focused tests for these cases:
- plain draft update preserving omitted headers
- HTML draft update with omitted `body_format` preserving HTML-mode replacement
- HTML draft update preserving inline CID-related parts when attachments are omitted
- invalid-charset HTML draft still preserving CID-related inline parts when body stays HTML
- omitted multipart attachment preservation
- omitted `message/rfc822` attachment preservation
- explicit `attachments=[]` clearing all attachment/related parts
- explicit supplied attachments replacing previous attachment/related parts
- explicit `thread_id=""` removing request-body thread binding
- malformed raw MIME failure
- blank `draft_id` failure

### Coupled proof

If shared helpers move or change, also run focused no-change checks for:
- `draft_gmail_message`
- `send_gmail_message`

### Full gates

Run:
- `uv sync --group dev`
- `uv run ruff check .`
- `uv run pytest`

## Mutable vs protected paths

Mutable for this plan:
- `gmail/gmail_tools.py`
- `tests/gmail/test_draft_gmail_message.py`
- `tests/gmail/test_modify_gmail_message_labels_schema.py`
- `skills/managing-google-workspace/references/gmail.md`

Reference-only / should remain untouched unless the contract wording truly changes:
- unrelated Gmail docs and skill surfaces
- package and deployment metadata
- auth/runtime config surfaces

Protected runtime/secret paths that must remain untouched:
- `.env`
- `client_secret.json`
- `~/.google_workspace_mcp/credentials`
- live attachment storage outside owned test fixtures

## Risks / blockers

- The body-subtree replacement helper is the hardest part; keep it narrow and avoid turning this into a repo-wide MIME utility framework.
- The redesign is only still “simple” if it stays inside update semantics; if shared create/send abstractions need heavy rewrites, stop and re-scope.
- Do not claim literal raw-byte preservation once the message is parsed and reserialized.
- Be explicit that inline related parts are part of the attachment clear/replace surface; otherwise the contract becomes ambiguous again.

## Checklist

- [x] Governing plan written to disk
- [x] Confirm execution will stay inside `update_gmail_draft` + coupled proof/docs
- [x] Realign branch to the intended live PR head before tracked edits
- [x] Implement direct MIME-tree update path
- [x] Remove preserve-extract-rebuild logic from the update flow
- [x] Keep create/send paths unchanged unless a tiny shared helper extraction is strictly required
- [x] Update focused tests for the new precise contract
- [x] Update Gmail reference docs only where the contract wording changes
- [x] Run `uv sync --group dev`
- [x] Run `uv run ruff check .`
- [x] Run `uv run pytest`
- [ ] Re-audit live PR review surface after the last push on the final head SHA

## Execution handoff

Use this artifact as the governing source:
- `docs/plans/2026-04-18-gmail-draft-update-simplicity-redesign.md`

Execution rules:
- do not create a new plan
- do not re-plan this pass
- stay inside `update_gmail_draft` runtime behavior, coupled tests, and coupled Gmail reference docs
- do not touch auth, packaging, deployment, or unrelated Gmail tools
- re-walk the real affected surface before edits and again before calling the pass complete
- keep the checklist current

Use only these execution skills:
- `production-preflight`
- `production-code`

Final merge gate:
- do not treat the work as complete until the last pushed head SHA has been re-audited against the live review surface, not just local green tests
