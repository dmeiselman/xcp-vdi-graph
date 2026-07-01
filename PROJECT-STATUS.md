# XCP-ng VDI Snapshot-Metadata Corruption ("VDI Super-Parents") — Master Status & Remediation Plan

**Last updated:** 2026-07-01 (Phase 1 executed against all affected pools — verdict: METADATA-ONLY, no physical chain entanglement — see §6)
**Scope of this document:** merges upstream/community tracking (Vates, forum, GitHub) with this environment's internal diagnostic tooling and remediation plan.

---

## 0. Executive Summary

A design flaw in how XAPI/SMAPI maintain the `snapshot_of` VDI database field lets it become stale or contradictory on live, non-snapshot disks. Xen Orchestra trusts that field to decide what's a snapshot vs. a real disk, so affected VDIs vanish from the per-VM **Disks** tab even though the disk is intact, attached, and the VM runs normally. The community term for the pattern is **"VDI super-parents"** — a VDI whose `snapshots:` field lists UUIDs that are actually unrelated VMs' primary disks, not real children.

- **Upstream status:** XAPI-side portion of the real fix is implemented; the SMAPI-side portion is in progress (no ETA). Vates has published an official one-off repair script (`snapshot-fixer.py`), which has a known minor bug and doesn't fully address root cause or recurrence.
- **This environment's status:** Several XCP-ng 8.3.x pools (LVM/iSCSI SRs) are confirmed affected. A read-only unified audit tool (20 checks) and a ground-truth cross-checker (`xcp-vhd-chain-crosscheck.py`, comparing physical VHD trees against xapi metadata) were built, validated against a clean reference host, and **have now been run against all affected pools**. **Verdict: METADATA-ONLY across the board — no genuine VHD chain entanglement found. The central open question is resolved: this is purely xapi database corruption, not physical disk-layer damage.** Remediation now proceeds down the metadata-only path (Phase 2, §6) with no migration required.

---

## 1. Problem Statement

### Symptom summary (general / upstream-confirmed)

| Where you look | What you see |
|---|---|
| XO5 / XO6 → VM → Disks tab | "No item found" — disk appears missing |
| XO REST API (`/rest/v0/vms/<uuid>/vdis`) | Empty array, or `"no such VDI"` for a UUID XCP-ng knows about |
| `xe vdi-param-list` on the host | VDI exists; `is-a-snapshot: false` but `snapshot-of:` populated pointing at another VDI (sometimes itself) |
| XCP-ng Center (Windows client) | Disk shows up fine — confirms this is a XO/XAPI-consumption issue, not real data loss |
| XOA Dashboard → Health | Spurious "orphaned base copy" entries — **do not delete these**, several confirmed false positives were live disks |
| VM itself | Runs fine; snapshots, backups, and guest file access unaffected |
| ISO SRs | Also affected — ISOs get a bogus `snapshot-of` and disappear from the upload picker |

### This environment's specifics

- Several XCP-ng **8.3.x** pools with **LVM-based SRs** (`lvmoiscsi`/iSCSI) are confirmed affected.
- Two visible symptoms locally: (1) XO5 hiding live VM disks due to snapshot-flavored metadata, and (2) a "super-parent" VDI named as `snapshot-of` by VDIs spanning unrelated VMs and even ISO SRs.
- **Central open question — RESOLVED (2026-07-01):** are the actual VHD chains on disk tangled, or is corruption confined to the xapi database? Audit tooling run against all affected pools found **metadata corruption only — no genuine physical chain entanglement**. Every pool classifies as `METADATA-ONLY`; no VM requires storage migration.
- **Suspected trigger:** XO rolling-snapshot backup jobs and/or snapshot-revert cycles — consistent with the upstream root-cause explanation (§2) and with multiple community reports of corruption appearing right after 8.3.0 patch updates and nightly backup cycles. Per-VM correlation in this environment has **not yet been done** (see §8).

### Constraints & environment

| Item | Status |
|---|---|
| XCP-ng version | 8.3.x on affected pools |
| SR types | LVM-based (lvmoiscsi / iSCSI) |
| VM provenance | Mixed (clones, fresh installs, imports — varies by pool) |
| Downtime windows | Available |
| Spare SR capacity | Available (enables migration-based remediation) |
| XO | Kept up to date; pool-side fix still expected to be needed (XML state) |
| Access from this workstation | Clean reference host only: `root@10.88.88.124` (no corruption; used for `xe` CLI shape). Affected pools are not reachable directly — data is relayed. |

---

## 2. Root Cause (per Vates/XCP-ng team)

XAPI's data model has two ways of representing a VDI's snapshot relationship:

1. `is_a_snapshot` (boolean flag)
2. `snapshot_of` (parent UUID reference)

These fields are **redundant and can contradict each other**. `snapshot_of` can be written by either XAPI or the SM (`smapi`) storage layer, and under certain snapshot **revert** operations, one side writes it while the other doesn't clean it up. Xen Orchestra's classification logic then reads a live, non-snapshot VDI as if it were a snapshot and filters it out of the UI.

- **XAPI-side fix:** implemented.
- **SMAPI-side fix:** in progress as of the last upstream update, described by Vates as requiring "deep changes in both stacks" because a proper `VDI.revert` operation doesn't cleanly exist yet.
- First community report traced back to **late August/September 2025**; visibility increased sharply after a December 2025 XO code change altered how it interprets these fields.

**Layered model (useful for disambiguating metadata-only vs. physical corruption):**

| Layer | What it is | Corruption here means |
|---|---|---|
| 1. xapi DB fields (`is-a-snapshot`, `snapshot-of`, `snapshots`) | Pure bookkeeping. What XO reads, what audits check, what `snapshot-fixer.py` edits. | Display/management breakage only, never data loss |
| 2. `sm-config:vhd-parent` | The storage manager's cached view (partially audited) | Stale cache vs. physical reality |
| 3. Actual VHD chains in the LVM VG | Ground truth | Genuine chain tangles — requires migration, not DB edits |

**Corollary:** a shared VHD parent across "unrelated" VMs is **not inherently corruption** — fast clones legitimately share a hidden read-only base copy until coalesce separates them. A chain-crossover finding can be normal clone topology; only the data layer (§5, `xcp-vhd-chain-crosscheck.py`) can disambiguate.

---

## 3. Timeline

| Date | Event |
|---|---|
| ~Aug/Sep 2025 | Earliest known report of the underlying corruption (per Vates) |
| Dec 10, 2025 | XO code change alters snapshot-detection logic (`is_a_snapshot \|\| $snapshot_of !== undefined`), making the issue far more visible in XO5/XO6 |
| Dec 24, 2025 | Forum thread [#11715](https://xcp-ng.org/forum/topic/11715/vdi-not-showing-in-xo-5-from-source) opened — "VDI not showing in XO 5 from Source" |
| ~Jan 2026 | Community coins the term **"VDI super-parents"**; `snapshot-of` field mapping identified as the mechanism |
| Feb 2026 | GitHub PR [#9231](https://github.com/vatesfr/xen-orchestra/commit/85596da79217070bf4431135bbb5b0d2cf04e45b) merged — improves xo-server's handling of `is_a_snapshot`/`snapshotOf` mismatch |
| Feb 26, 2026 | Related regression via REST API routing, tracked in PR [#9381](https://github.com/vatesfr/xen-orchestra/pull/9381) (closed) |
| Mar 2026 | Vates confirms two-track remediation: proper fix + one-off repair script |
| Apr–May 2026 | Independent reports confirm the same signature after XCP-ng 8.3.0 patch updates (package delta: `xapi-core`, `sm`, `sm-fairlock`, `xenopsd`, etc.) |
| May 2026 | GitHub Issue [#9578](https://github.com/vatesfr/xen-orchestra/issues/9578) opened — "VM disk with status => no item found on XOA" |
| Late May/early Jun 2026 | Vates publishes official `snapshot-fixer.py` workaround script in `xcp-ng/xcp` |
| Jun 8, 2026 | Recurrence reported the day after running the fixer, suspected nightly-backup re-trigger; a bug in the script's `elif` branch acknowledged by Vates but left as low-priority |
| **Jun 10, 2026** | **(This environment)** `xcp-vhd-chain-crosscheck.py` built and validated on reference host; critical parser bug found and fixed in `xcp-vdi-unified-audit-v2.py` — see §5 |

---

## 4. Upstream / Community Tracking

### GitHub issues & PRs

| # | Repo | Title | Status | Relevance |
|---|---|---|---|---|
| [#9578](https://github.com/vatesfr/xen-orchestra/issues/9578) | `vatesfr/xen-orchestra` | VM disk with status => no item found on XOA | Open | Closest thing to a canonical tracking issue |
| [#9231](https://github.com/vatesfr/xen-orchestra/pull/9231) | `vatesfr/xen-orchestra` | fix(xo-server): improve handling of xapi snapshots | Merged | First partial fix — handles `is_a_snapshot=false` + populated `snapshotOf` better |
| [#9381](https://github.com/vatesfr/xen-orchestra/pull/9381) | `vatesfr/xen-orchestra` | fix(rest-api): fix getVmVdis and enhance the type | Closed | Fixed a related REST API 404 routing regression surfaced by the same data issue |
| `snapshot-fixer.py` | [`xcp-ng/xcp`](https://github.com/xcp-ng/xcp/blob/master/scripts/snapshot-fixer.py) | Rewrite erroneous VM snapshot links | Published | Official remediation script — the actual `xcp-ng` org artifact (see §5 for a technical critique) |

There is no single dedicated tracking issue in the `xcp-ng` org itself (e.g. in `xcp-ng/xen-api` or `xcp-ng/sm`) for the underlying XAPI/SMAPI design flaw — public bug tracking lives almost entirely on the `vatesfr/xen-orchestra` side, since that's where the symptom surfaces. The XAPI-side fix and in-progress SMAPI-side fix don't have a public PR reference as of the last update.

### Forum threads

- [#11715 — VDI not showing in XO 5 from Source](https://xcp-ng.org/forum/topic/11715/vdi-not-showing-in-xo-5-from-source) — 56 posts; primary community thread; origin of the "VDI super-parents" term, root-cause discussion, and the official fixer script release. Symptoms there matched exactly: `is-a-snapshot: false` + `snapshot-of: <uuid>` contradiction, appearing after snapshot/revert operations.
- [#12152 — XenOrchestra not showing VM Disks on Pool](https://xcp-ng.org/forum/topic/12152/xenorchestra-not-showing-vm-disks-on-pool-on-single-server-working-xcp-ng-center-is-showing-them) — independent confirmation thread with detailed REST API failure diagnostics and package-delta correlation.

Reported community workarounds (predate or supplement the official script): halt → snapshot → revert → delete-snapshots cycle, or migrating the VM to another SR and back (both force fresh metadata). Vates staff (`anthoineb`, `MathieuRA`, `olivierlambert`, `stormi`, `florent`) were actively engaged throughout.

| Workaround | Notes |
|---|---|
| Snapshot → revert (with "take snapshot" option) → delete snapshots | Reliable per-VM; doesn't scale past a handful of VMs |
| Migrate VDI/VM to a different SR and back | Forces metadata rebuild; requires downtime/storage headroom — **this is the same principle behind the Phase 2 migration path in §7** |
| `xe vdi-copy` to a new SR, reattach | Collapses the snapshot chain and regenerates clean metadata |
| For ISO SRs: forget the VDI, then rescan the SR | Simplest fix for ISO-only cases; ISOs uploaded via XOA lose their original name (renamed to `.IMG`) |
| "Rescan All Disks" on the SR | Anecdotally fixed it for some hosts, not others — non-destructive, worth trying first |

---

## 5. Tooling Inventory (this environment)

### `snapshot-fixer.py` — official Vates fixer, the only **write** tool

Published at [`xcp-ng/xcp`](https://github.com/xcp-ng/xcp/blob/master/scripts/snapshot-fixer.py). Disables HA, stops xapi, backs up `/var/lib/xcp/state.db`, edits the XML directly, restarts. Subcommands: `dry-run`, `rewrite`, `restore-backup`.

- **Why XML editing is necessary:** `is-a-snapshot` and `snapshot-of` are read-only through xapi — there is no `xe vdi-param-set` path. Pool-side correction requires the stop-xapi/rewrite approach (or VDI migration, which creates fresh records).
- **Coverage:** fixes only two patterns — non-snapshot records with `snapshot_of` set (nulled), and self-referential VDI snapshots (nulled + unflagged).
- **Known bug (confirmed independently in this environment and acknowledged by Vates on the forum):** the self-reference branch checks `record.get('is-a-snapshot')` (hyphens), but `state.db` XML attributes use underscores (`is_a_snapshot`), so that branch never fires (`snapshot-fixer.py:113`). This matches the dead-`elif` bug Vates confirmed upstream — same root defect, independently rediscovered here.
- **Known gap:** it never cleans the *parent* side — stale entries in parents' `snapshots` lists and bogus `snapshot_time` values survive the fix, so reciprocal-link findings persist after a run. Confirmed observation: it works but doesn't address root cause, and matches the community report of recurrence the day after running it.
- **Compatibility:** not supported on XCP-ng 8.2.1 (EOL).

### `xcp-vdi-graph-audit.sh` — read-only bash audit (superseded)

Pool-master bash 4.2-compatible audit, 17 finding categories, severity-ranked report with ghost-UUID clustering. Detection logic has been folded into the unified auditor below.

### `xcp-vdi-unified-audit-v2.py` — read-only consolidated auditor (current)

Supersedes the original single-file `xcp-vdi-unified-audit.py` (the one with the `A1-SELF-REF` self-referential-VDI check). V2 expands to **20 checks (A1–G1)**: self-refs, contradictions, circular chains, orphans, broken reciprocal links, duplicate parent claims, snapshots-on-live-VMs, vhd-parent issues, cross-SR refs, corrupt snapshot roots, super-parents, dangling VBDs, VM-level issues, missing VDIs. Classifies VMs (REAL-VM / VM-SNAPSHOT / XO-BACKUP / TEMPLATE / CONTROL-DOMAIN); outputs text/JSON/CSV; exits non-zero on CRITICAL/HIGH.

Consolidates two scripts referenced in its header not present in this directory: `super-parent-finder.sh` and `vdi-snapshot-metadata-audit-v2.sh`.

> **PARSER BUG (fixed 2026-06-10):** the xe-output field regex (`^(\S.*?)\s*\(...`) rejected leading whitespace, but `xe` indents every field line except `uuid`. Every field was silently swallowed as a "continuation" of the uuid line, so this copy of the auditor parsed all records as effectively empty. Fixed by allowing leading whitespace (`^\s*(\S.*?)...`); verified on the reference host (now reports real findings: 6 HIGH `B1-ORPHANED-SNAPSHOT`, 4 `G1-VDI-MISSING`, 1 `A2-CONTRADICTION` — note `B1-ORPHANED-SNAPSHOT` can be benign: a kept snapshot whose source disk was deliberately deleted).
>
> A fix for this same bug was independently made in a personal fork of the tool — the copy in this directory was the stale pre-fix version. Audit results from the *fixed fork* remain valid; only results from this stale copy were garbage. **TODO: sync the latest fork into this directory so it stays the single source of truth.**

### `xcp-vhd-chain-crosscheck.py` — data-layer ground truth (new, built 2026-06-10)

The Phase 1 tool (§6). Joins `vhd-util scan` physical VHD trees with xapi metadata and classifies **every `snapshot-of` claim** into:

- **`DB-GARBAGE`** — no physical basis (target absent from VHD tree, child/target in unrelated trees, cross-SR claim, raw-LV involvement, self-reference). Safe to null in `state.db`.
- **`PHYSICAL-RELATED`** — child and target share a common physical ancestor (normal snapshot/clone sibling-under-hidden-base layout). Chain is fine; only flags may need fixing.
- **`UNVERIFIABLE`** — not visible to `vhd-util`; manual review.

Independently detects physical anomalies: active (`hidden=0`) VHDs with children (never normal — strong tangle indicator), parent-link cycles, dangling physical parents, trees whose active leaves span multiple real VMs (fast-clone vs. tangle, using the hidden/active share-point as the discriminator), stale `sm-config:vhd-parent` vs. physical parent, orphan active VHDs, and VDIs with no backing VHD/LV.

Emits an overall verdict: `CLEAN` / `METADATA-ONLY` (state.db cleanup sufficient) / `PHYSICAL-ENTANGLEMENT` (migration needed; exit code 2). This directly answers the tangled-vs-never-separated question from §1.

Designed around the access constraint (affected pools unreachable from the analysis machine):

```sh
# on the affected pool master (read-only: xe list/vhd-util scan/lvs only):
python3 xcp-vhd-chain-crosscheck.py collect --out /root/crosscheck-data
# copy the directory back, then anywhere:
python3 xcp-vhd-chain-crosscheck.py analyze --from ./crosscheck-data [--json out.json]
# or both at once on the master:
python3 xcp-vhd-chain-crosscheck.py run [--keep DIR] [--json FILE]
```

Python 3.6.8 stdlib only (verified against the host's interpreter). Validated end-to-end on the reference host 2026-06-10: 44 VHDs across 2 LVM SRs, 11 snapshot-of claims all correctly classified `PHYSICAL-RELATED`, verdict `CLEAN`, exit 0. Offline `analyze` verified on the workstation against `./sample-crosscheck-data/` (kept as a test fixture); collect/relay workflow proven end-to-end.

---

## 6. Recommended Remediation Plan

### Phase 1 — Data-layer ground truth (read-only, no downtime) — **EXECUTED ✅ — verdict: METADATA-ONLY, all pools**

`xcp-vhd-chain-crosscheck.py` (§5) implemented this phase; it has now been run against all affected pools with a consistent result: no physical chain entanglement anywhere. Steps taken:

1. Copied the script to each pool master; ran `collect --out DIR` (read-only).
2. Copied `DIR` back; ran `analyze --from DIR --json pool-<name>.json`.
3. Every pool read `METADATA-ONLY` — proceeding to the Phase 2 metadata path below for all of them. No pool triggered `PHYSICAL-ENTANGLEMENT`.
4. `xe sr-scan` / `SMlog` coalesce-error review from step 4 of the original plan: not yet done — still worth a pass to rule out a slow-brewing coalesce backlog even though the snapshot-metadata itself is clean (not yet automated).

| Bucket | Evidence | Disposition |
|---|---|---|
| DB-GARBAGE | Claim has no physical basis in the VHD tree | Safe to null in `state.db`; record deletable if no backing LV |
| PHYSICAL-RELATED | Child and target share a physical ancestor (clone/snapshot layout) | Fix flags only; **never** touch the VHD |
| Genuinely tangled (physical anomalies) | Active-VHD-with-children, cycles, active shared roots | **Not observed in this environment — bucket unused** |

### Phase 2 — Remediate per bucket — **NOW ACTIVE (metadata-only path, all pools)**

**Metadata-only (confirmed 100% of affected pools), per pool in a maintenance window:**

1. **Pause XO backup schedules** for the pool first — don't let the suspected trigger race the rewrite.
2. `xe pool-dump-database file-name=...` as a second backup beyond the `state.db` copy.
3. Run the **extended fixer** (to be built on top of Vates' `snapshot-fixer.py`):
   - fix the `is-a-snapshot`/`is_a_snapshot` hyphen bug (§5, and matches Vates' own acknowledged bug upstream);
   - drive fixes from audit findings (from `xcp-vdi-unified-audit-v2.py`), not just the two hardcoded patterns;
   - null bogus `snapshot_of`, clear wrong `is_a_snapshot`, scrub stale `snapshot_time`, and clean parents' `snapshots` lists (reciprocal integrity) — closing the gap identified in §5;
   - keep dry-run / rewrite / restore-backup semantics.
4. Restart, re-run the unified audit, require zero CRITICAL/HIGH before re-enabling backups.

**This is now the only path required** — every pool classified `METADATA-ONLY` in Phase 1, so the migration path below is retained for reference/future recurrence but isn't on the critical path right now.

**Genuinely tangled chains (not currently applicable — no pool triggered this):** do **not** DB-edit. Storage-migrate (or offline `vdi-copy`) the affected VMs' disks to the spare SR — this writes a brand-new single-VM chain *and* fresh xapi records in one operation (this is the mechanism behind why "migrate to another SR and back" fixed it for multiple forum reporters — see §4). Let GC clean the source SR afterwards.

**The super-parent VDI itself:** only after its claimed children are cleaned, and only if `vhd-util scan` shows nothing chained to it, use `xe vdi-forget` (drops the record, leaves the LV for `sr-scan` to reconcile) rather than `vdi-destroy`.

### Phase 3 — Root cause & recurrence prevention

Fixing the DB doesn't stop re-corruption — consistent with the community-reported recurrence in §3/§4.

- **Forensic lead:** bogus `snapshot-time` values on `A2-CONTRADICTION` VDIs timestamp when the corruption happened. Correlate them with XO backup-job logs and `xensource.log` to identify the exact operation (interrupted rolling snapshot vs. revert).
- **Early detection:** cron the unified audit (`--skip-info --json`) on each pool master and alert on non-zero exit, catching recurrence before disks vanish in XO.
- **Upstream:** post correlation results to forum thread [#11715](https://xcp-ng.org/forum/topic/11715/vdi-not-showing-in-xo-5-from-source) — Vates was actively investigating, and per-VM trigger data would help land a real upstream fix.

### Rollout order

1. ~~Build the vhd-util-vs-xapi cross-check script~~ ✅ done, validated on `10.88.88.124`.
2. ~~Run audit + ground-truth scan on all affected pools; classify~~ ✅ done — all pools `METADATA-ONLY`.
3. Build/harden the extended metadata fixer (§5 gaps), pilot on the **least critical** affected pool in a maintenance window; verify clean re-audit. **← current step**
4. Roll out to remaining pools (no migration needed — all confirmed metadata-only).
5. Stand up the cron audit + log correlation.

---

## 7. Immediate Next Actions

- [x] Write the `vhd-util` ⇄ xapi cross-check/classification script (`xcp-vhd-chain-crosscheck.py`, validated on reference host 2026-06-10)
- [x] Fix the xe-parser whitespace bug in `xcp-vdi-unified-audit-v2.py` (previous runs of that script produced invalid/empty results)
- [x] **Re-run the unified audit on all affected pools** — done; verdict METADATA-ONLY across the board, no physical entanglement
- [x] Run `xcp-vhd-chain-crosscheck.py collect` on affected pool masters and classify — complete for all pools
- [ ] **Harden `snapshot-fixer.py` into the extended fixer (§6 Phase 2)** — hyphen bug at line 113, parent-side `snapshots` list cleanup, `snapshot_time` scrub, audit-driven fixes — **now the top priority, gates Phase 2**
- [ ] Pilot the extended fixer on the least-critical affected pool in a maintenance window; require zero CRITICAL/HIGH on re-audit before rollout
- [ ] Correlate A2 `snapshot-time` values with XO backup/revert history on at least one pool (Phase 3 forensics)
- [ ] Sync the latest fixed fork of `xcp-vdi-unified-audit-v2.py` into this directory (currently stale)
- [ ] Cross-check findings against `snapshot-fixer.py --dry-run` output before touching anything in the Health Dashboard — several confirmed false-positive "orphans" exist upstream (§1)
- [ ] Stand up cron audit (`--skip-info --json`) on each pool master once Phase 2 clears, to catch recurrence early (Phase 3)

---

## 8. Validation Log

| Date | What | Result |
|---|---|---|
| 2026-06-10 | xe output format, Python 3.6.8, vhd-util presence on reference host | Confirmed; parsers built against real output |
| 2026-06-10 | First cross-check run on reference host | Exposed the leading-whitespace parser bug (0 records parsed) |
| 2026-06-10 | Regex fix applied to cross-check + unified auditor | Confirmed via direct regex test against real xe lines |
| 2026-06-10 | Cross-check `run` on reference host (lvm + lvmoiscsi SRs) | 44 VHDs, 11 claims all PHYSICAL-RELATED, verdict CLEAN, exit 0 |
| 2026-06-10 | Cross-check `analyze --from` offline on workstation | Identical report from `sample-crosscheck-data/`; collect/relay workflow proven |
| 2026-06-10 | Repaired unified audit on reference host | Now parses real records; 11 findings (6 B1-ORPHANED-SNAPSHOT HIGH, 4 G1, 1 A2) on a physically clean host |
| 2026-07-01 | `xcp-vhd-chain-crosscheck.py` + unified audit run against **all affected pools** | Every pool classified `METADATA-ONLY`; no `PHYSICAL-ENTANGLEMENT` verdict anywhere — Phase 1 complete, tangled-chain remediation path not needed |

---

## 9. Current Status Snapshot

- ✅ XAPI-side portion of the upstream fix: implemented
- 🔄 SMAPI-side portion of the upstream fix: in progress, no ETA, architecturally non-trivial
- ✅ Official one-off repair script (`snapshot-fixer.py`): published, with a known bug **independently confirmed in this environment**
- ⚠️ Community-reported recurrence post-fix, cause unconfirmed but suspected tied to backup/snapshot cycles — still an open risk for this environment's Phase 3 work
- ❌ No upstream fix path for XCP-ng 8.2.1 (EOL)
- ✅ This environment: read-only diagnostic tooling (audit + ground-truth cross-checker) built and validated on a clean reference host
- ✅ **This environment: Phase 1 complete — all affected pools classified `METADATA-ONLY`, zero physical chain entanglement. No storage migration required anywhere.**
- ⏳ This environment: Phase 2 (extended fixer) is now the critical path — `snapshot-fixer.py`'s known gaps (parent-side cleanup, `snapshot_time` scrub, hyphen bug) must be closed before running it at scale

---

## 10. References

- Forum: [VDI not showing in XO 5 from Source (#11715)](https://xcp-ng.org/forum/topic/11715/vdi-not-showing-in-xo-5-from-source) — 56 posts, primary community thread
- Forum: [XenOrchestra not showing VM Disks on Pool (#12152)](https://xcp-ng.org/forum/topic/12152/xenorchestra-not-showing-vm-disks-on-pool-on-single-server-working-xcp-ng-center-is-showing-them)
- GitHub: [vatesfr/xen-orchestra#9578](https://github.com/vatesfr/xen-orchestra/issues/9578)
- GitHub: [vatesfr/xen-orchestra PR #9231 (commit 85596da)](https://github.com/vatesfr/xen-orchestra/commit/85596da79217070bf4431135bbb5b0d2cf04e45b)
- GitHub: [vatesfr/xen-orchestra PR #9381](https://github.com/vatesfr/xen-orchestra/pull/9381)
- GitHub: [xcp-ng/xcp — snapshot-fixer.py](https://github.com/xcp-ng/xcp/blob/master/scripts/snapshot-fixer.py)
- Internal: `xcp-vdi-unified-audit.py` (original, `A1-SELF-REF` check) → superseded by `xcp-vdi-unified-audit-v2.py` (20 checks, A1–G1)
- Internal: `xcp-vdi-graph-audit.sh` (superseded bash audit)
- Internal: `xcp-vhd-chain-crosscheck.py` (new, ground-truth data-layer tool)
- Internal: `sample-crosscheck-data/` (offline test fixture)
