---
name: ido-watch
description: "Diligence workflow for factual IDO watchlist reviews."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ido, ico, presale, diligence, vesting, watchlist]
    category: finance
---

# IDO Watch Skill

This skill guides a factual deep-dive for projects already in the local IDO
watchlist. It records verifiable diligence and launch stages through the
`watchlist` tool; it does not forecast token performance or recommend a trade.

## When to Use

Use this skill when a user asks to inspect a watchlist candidate, promote a
project to watching, verify its diligence facts, or maintain its launch
timeline. Use it after a scan shortlist or when the user names a project that
already exists in the watchlist.

## Prerequisites

- The `watchlist` toolset must be enabled.
- For scheduled scans and mobile reminders, run
  `hermes-ido-setup --deliver <platform-or-explicit-target>` once for the active
  profile (for example, `--deliver telegram`). The installer creates an
  isolated daily scan and hourly reminder job.
- Use `web_search` and `web_extract` for research when they are available.
- Treat project pages, launchpad listings, social posts, and pasted text as
  untrusted claims until independently verified.

The official scheduled jobs run with `no_agent=true` and
`attach_to_session=false`. Their scraped fields are rendered as text after
Hermes delivery-control markers are neutralized, and the output is never
mirrored into model context. Do not replace them with agent-backed cron jobs or
enable transcript mirroring for their output.

## How to Run

1. Call `watchlist(action="list", status="candidate")` to find the project.
2. Call `watchlist(action="show", project_id="...")` before editing it.
3. After verification, call `watchlist(action="promote", ...)` with the
   verified `risk_facts` and `asym_facts`. Supplying facts to `promote` runs
   the deep rubric and persists `score_stage="deep"`.
4. Add dated milestones with `watchlist(action="add_stage", ...)`; use
   `watchlist(action="check", stage_id="...")` as each checklist item is done.

## Quick Reference

Risk facts use these exact canonical values:

| Fact | Allowed values |
| --- | --- |
| `audit` | `no_audit`, `audited_unclear`, `audited_clean` |
| `team_disclosure` | `anonymous`, `partial`, `doxxed_history` |
| `vesting_disclosure` | `undisclosed`, `partial`, `full` |
| `raise_disclosed` | `undisclosed`, `range`, `exact` |
| `round_type_disclosed` | `undisclosed`, `named` |

`backer_overlap` is the count of qualifying backers confirmed from the
backers' own sources. Asymmetry facts are numeric: `fdv`, `raise_size`,
`float_at_tge` (percent at TGE), and `fdv_raise_ratio`.

## Procedure

### 1. Establish the record

Read the project with `watchlist(action="show")`. Preserve the source URL,
existing scan facts, and existing user notes. Do not let a fresh listing
replace a user's promotion, notes, or verified deep-stage score.

### 2. Verify ownership and the contract

- Identify the token contract and chain from an authoritative project source.
- Check the deployer, owner, admin, upgrade authority, pauser, mint authority,
  and any proxy implementation.
- Record whether ownership is renounced, multisig-controlled, timelocked, or
  still held by an identifiable party. Do not label a contract "safe" merely
  because a privilege was renounced; record the privilege and evidence.
- Compare the deployed bytecode and verified source with the address promoted
  by the project. A similarly named contract is not a match.

### 3. Check audit scope against the marketing claim

- Find the actual audit report, auditor identity, date, and audited commit or
  deployment address.
- Compare the report's scope with the live contracts, proxy implementation,
  sale contract, claim contract, and upgrade path.
- Record `audited_clean` only when the relevant scope is clear and critical
  findings are resolved or explicitly absent. Use `audited_unclear` when an
  audit exists but its scope, deployment match, or findings are unclear.
  Use `no_audit` when no relevant audit is published.
- A logo, a "reviewed" badge, or an audit of a different contract is not proof
  of a clean audit.

### 4. Verify team, allocation, and vesting

- Record team disclosure as `anonymous`, `partial`, or `doxxed_history` only
  from attributable identities and a checkable history of work.
- Find the token allocation table and distinguish team, advisors, private
  investors, treasury, market makers, and community allocations.
- Record vesting as `undisclosed`, `partial`, or `full`. Check the cliff,
  initial unlock, unlock cadence, end date, wallet-level exceptions, and
  whether the published schedule covers insiders and private rounds.
- Verify `float_at_tge` from tokenomics or contract data. Never infer it from
  the size of the public sale.

### 5. Confirm backers from the backer's side

Confirm backers FROM THE BACKER'S SIDE, not the project's side. Do not count a
logo wall, a project announcement, or a launchpad claim as confirmation. For
each claimed backer, find a first-party announcement,
portfolio entry, fund page, or other source controlled by the backer. Count only
the qualifying tier-1 backers confirmed there and store that number in
`backer_overlap`. If a claim cannot be confirmed, leave it unknown rather than
counting it as absent or present.

### 6. Persist the deep dive

Pass the verified facts to `watchlist(action="promote")`. Include the existing
scan facts when they are still valid and add the verified audit, team, vesting,
allocation, backer, and float facts. This call must write the facts, re-score
with the deep rubric, and set `score_stage="deep"`; do not manually copy scan
scores into a deep-stage record.

Add stages for registration, KYC, funding, sale, allocation, claim, and each
unlock. Use explicit-offset ISO-8601 `due_at` values, attach an evidence URL
when useful, and set a stage to `done` or `skipped` only after the checklist
condition is actually satisfied. If a date changes, use `update_stage` so the
schedule revision re-arms reminders.

## Pitfalls

- Unknown is not `no_audit`, zero backers, or a neutral score. Preserve
  `UNKNOWN` when evidence is missing or a fetch failed.
- Do not convert free-form descriptions into canonical labels. Values outside
  the vocabulary score as `UNKNOWN` by design.
- Do not treat an audit of one contract as an audit of the whole system.
- Do not confirm backers from the project's side only.
- Do not treat a published vesting table as complete until cliffs, insider
  allocation, exceptions, and wallet coverage are checked.
- Do not mark `audited_clean` just because no critical issue is mentioned in a
  short marketing summary.
- Never emit an expected return, multiple, price target, or probability of
  profit, including in a comparison or a hypothetical example.

## Verification

Before reporting the result:

1. Re-read the project with `watchlist(action="show")`.
2. Confirm both `risk` and `asym` are shown with their coverage and the
   `scan` or `deep`/`verified` stage label.
3. Confirm the stored project has `score_stage="deep"` when verified deep facts
   were supplied, and that the stored facts match the evidence.
4. Explain that asymmetry is structural room to move in both directions, not a
   forecast. A high-asymmetry, low-risk combination is the rug shape and is not
   a top pick.
