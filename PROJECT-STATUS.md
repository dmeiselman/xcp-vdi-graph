# XCP-ng VDI Snapshot-Metadata Corruption ("VDI Super-Parents") — Master Status & Remediation Plan

**Last updated:** 2026-07-01 (added cross-cluster correlation evidence, confirmed non-durable per-VDI fixes, corrected tool naming, and inserted new Phase 1.5 — controlled reproduction — after reviewing full `xcp-vdi-unified-audit.py` source; see §1, §2, §5, §6)
**Scope of this document:** merges upstream/community tracking (Vates, forum, GitHub) with this environment's internal diagnostic tooling and remediation plan.

---

## 0. Executive Summary

A design flaw in how XAPI/SMAPI maintain the `snapshot_of` VDI database field lets it become stale or contradictory on live, non-snapshot disks. Xen Orchestra trusts that field to decide what's a snapshot vs. a real disk, so affected VDIs vanish from the per-VM **Disks** tab even though the disk is intact, attached, and the VM runs normally. The community term for the pattern is **"VDI super-parents"** — a VDI whose `snapshots:` field lists UUIDs that are actually unrelated VMs' primary disks, not real children.

- **Upstream status:** XAPI-side portion of the real fix is implemented; the SMAPI-side portion is in progress (no ETA). Vates has published an official one-off repair script (`snapshot-fixer.py`), which has a known minor bug and doesn't fully address root cause or recurrence.
- **This environment's status:** Several XCP-ng 8.3.x pools (LVM/iSCSI SRs) are confirmed affected. A read-only unified audit tool (20 checks) and a ground-truth cross-checker (`xcp-vhd-chain-crosscheck.py`, comparing physical VHD trees against xapi metadata) were built, validated against a clean reference host, and **have now been run against all affected pools**. **Verdict: METADATA-ONLY across the board — no genuine VHD chain entanglement found.** Remediation proceeds down the metadata-only path (§6, Phase 2) with no migration required.
- **New (this update):** a natural experiment across five clusters strongly implicates automated backups as the trigger — all 3 production clusters (which run scheduled backups) are affected, both dev clusters (no scheduled backups) are clean, despite matching XCP-ng versions and identical `lvmoiscsi` storage. Separately, first-hand testing confirmed that fixing individual VDIs (via SR migration) is **not durable** — the `snapshot-of` field re-corrupts at some later point. **This shifts the immediate priority: before rolling the extended fixer out pool-wide, the next step is controlled, intentional reproduction on a dev cluster to isolate the exact trigger** (§6, new Phase 1.5) — otherwise remediation risks being repeatedly undone.

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
- **Suspected trigger — now strongly correlated (not yet proven by controlled reproduction):** XO rolling-snapshot backup jobs and/or snapshot-revert cycles.

#### Cross-cluster correlation evidence (new)

| Cluster | Role | Automated backups | XCP-ng version | SR type | Corruption observed? |
|---|---|---|---|---|---|
| Prod 1 | Production | Yes | ~8.3.x (matches others) | lvmoiscsi | ✅ Yes |
| Prod 2 | Production | Yes | ~8.3.x (matches others) | lvmoiscsi | ✅ Yes |
| Prod 3 | Production | Yes | ~8.3.x (matches others) | lvmoiscsi | ✅ Yes |
| Dev 1 | Development | No | ~8.3.x (matches others) | lvmoiscsi | ❌ No |
| Dev 2 | Development | No | ~8.3.x (matches others) | lvmoiscsi | ❌ No |

3/3 production clusters affected, 0/2 dev clusters affected, with version and storage backend held constant — the only material variable identified so far is presence of a scheduled backup job. This is a clean natural experiment but not yet a controlled one; see Phase 1.5 (§6) for the proposed reproduction methodology.

#### Confirmed: individual-VDI fixes are not durable

Migrating an individual affected VDI to a new SR does clear the corruption for that VDI — consistent with the community-reported workaround (§4) — **but it has now been directly confirmed in this environment that the `snapshot-of` field re-corrupts on that same VDI at some later point.** This rules out "one-time DB corruption, fix once and done" as a model. The corruption is being **actively regenerated** by something in the environment, most plausibly the backup pipeline (see correlation table above).

**Open hypothesis (unconfirmed): whether remediation must be applied to all VDIs simultaneously ("all-or-nothing"), or whether individually-fixed VDIs simply have equal ongoing exposure to the same recurring trigger** (i.e. not literally an all-or-nothing dependency between VDIs, but each fixed VDI independently sitting in the blast radius of the next backup cycle until the trigger itself is understood and either fixed or worked around). Distinguishing these two explanations is the main goal of the Phase 1.5 reproduction work below, since they imply different remediation strategies — a genuine all-or-nothing dependency would mean partial fixes are actively harmful/pointless, while independent-exposure would mean partial fixes are safe but need to be repeated (e.g. via a cron'd auto-repair) until the trigger is closed.

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
| 1a. xapi native VDI fields (`is-a-snapshot`, `snapshot-of`, `snapshots`) | Pure bookkeeping on the `VDI` object itself. What XO reads, what `snapshot-fixer.py` edits. This is the field pair Vates describes upstream. | Display/management breakage only, never data loss |
| 1b. `sm-config` cached fields (`vhd-parent`, `snapshots`) | A **second, separate** set of bookkeeping values cached inside the `sm-config` map by the SM/`smapi` layer — distinct key-value pairs from 1a, not just a mirror of it | Can drift independently from 1a *and* from physical reality; this is the layer the `D1-SUPER-PARENT` audit check targets specifically |
| 2. Actual VHD chains in the LVM VG | Ground truth | Genuine chain tangles — requires migration, not DB edits |

**Important nuance surfaced by reviewing the unified audit script directly:** the community term "VDI super-parents" turns out to map to **two structurally different findings** depending on which layer is corrupted:
- A VDI's *native* `snapshots` field listing UUIDs of unrelated VMs' real disks → caught by `B2-CROSS-VDI` / `B3-DUPLICATE-CLAIM` / `C5-CORRUPT-SNAPSHOT-ROOT` (layer 1a). This matches the original forum description of the symptom.
- A VDI's `sm-config:snapshots` cache making the same claim → caught by `D1-SUPER-PARENT` specifically (layer 1b).

Both produce the same user-visible symptom (disks vanishing from XO), but they're different fields with potentially different write paths — worth keeping distinct during the Phase 1.5 reproduction work, since the corrupted layer may be a clue to which XAPI/SM code path is misbehaving.

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
| **Jun 10, 2026** | **(This environment)** `xcp-vhd-chain-crosscheck.py` built and validated on reference host; critical parser bug found and fixed in `xcp-vdi-unified-audit.py` — see §5 |
| **Jul 1, 2026** | **(This environment)** Phase 1 executed against all affected pools (METADATA-ONLY, no entanglement); cross-cluster correlation (3 prod affected w/ backups vs. 2 dev clean w/o backups) and confirmed non-durability of per-VDI fixes established — see §1, §6 |

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
| Migrate VDI/VM to a different SR and back | Forces metadata rebuild; requires downtime/storage headroom — **this is the same principle behind the Phase 2 migration path in §6** — note however that this environment has now confirmed the fix from this workaround does not hold; see §1 |
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

### `xcp-vdi-unified-audit.py` — read-only consolidated auditor (current)

**Naming correction:** this is the same file from the original single-check version (`A1-SELF-REF` only, from the earlier dataclasses-removal work) — it has **grown in place** to 20 checks, not been replaced by a separate `-v2` file. (Earlier drafts of this document referred to it as `xcp-vdi-unified-audit-v2.py`; that name doesn't match the actual file and should be treated as an error — corrected here.)

Per its own header, it combines and extends detection logic from four source scripts: `snapshot-fixer_ffc40afd.py` (→ `A1-SELF-REF`, `A2-CONTRADICTION`), `super-parent-finder_0bd59673.sh` (→ `D1-SUPER-PARENT`, sm-config-based), `xcp-vdi-graph-audit_e87532c3.sh` (→ graph-level checks: VHD chains, cross-SR, etc.), and `vdi-snapshot-metadata-audit-v2_f00702bf.sh` (→ VM-correlated reciprocal checks). Of these, `xcp-vdi-graph-audit.sh` and (the official) `snapshot-fixer.py` are separately present in this directory (§5); `super-parent-finder.sh` and `vdi-snapshot-metadata-audit-v2.sh` are not.

Classifies VMs into `REAL-VM` / `VM-SNAPSHOT` / `XO-BACKUP` (detected via `other_config` keys prefixed `xo:backup`) / `TEMPLATE` / `CONTROL-DOMAIN` — the `XO-BACKUP` classification in particular is useful groundwork for the Phase 1.5 reproduction/correlation work, since it lets findings be automatically filtered to whether they're touching real disks vs. expected backup-job snapshot artifacts. Outputs text/JSON/CSV; exits non-zero on CRITICAL/HIGH.

**Full check catalog (from the script's own `--help` epilog):**

| ID | Check | Severity range |
|---|---|---|
| A1 | Self-referential `snapshot-of` | CRITICAL |
| A2 | Contradictory flags (`is-a-snapshot` vs `snapshot-of`/`snapshot-time`) | CRITICAL/MEDIUM |
| A3 | Circular `snapshot-of` chains (depth > 1) | CRITICAL |
| B1 | Orphaned/missing parents, broken reciprocal links | HIGH/MEDIUM |
| B2 | `snapshots`-field corruption (ghost refs, broken back-links, cross-VDI contamination) | MEDIUM/HIGH/CRITICAL |
| B3 | Duplicate parent claims (a VDI claimed by >1 parent) | CRITICAL |
| B4 | Snapshot-flagged VDI attached to a live/production VM | CRITICAL/HIGH/INFO |
| C1 | Self-referential `vhd-parent` (sm-config) | HIGH |
| C2 | Dangling `vhd-parent` (sm-config) | MEDIUM |
| C3 | VHD chain crossover (one parent, children across unrelated VMs) | HIGH |
| C4 | Cross-SR references (`snapshot-of` or `vhd-parent` crossing SR boundary) | HIGH |
| C5 | Corrupt snapshot root (children spanning unrelated VMs/mixed SR types) | HIGH |
| C6 | ISO/udev VDI flagged as snapshot | HIGH |
| C7 | `snapshot-of` points to another snapshot | MEDIUM |
| D1 | Super-parent via `sm-config:snapshots` (the SM-cache variant — see §2) | HIGH/INFO |
| E1 | Dangling VBD (references non-existent VDI) | MEDIUM |
| E2 | VBD back-link mismatch | LOW |
| F1 | VM-level snapshot issues (missing/dangling `snapshot-of`) | MEDIUM |
| F2 | VM-level contradictions (mirrors A2 for VM objects) | CRITICAL |
| G1 | VDI flagged `missing` (backing store gone) | MEDIUM |

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

### Phase 1.5 — Controlled Reproduction & Root-Cause Isolation — **NEW, CURRENT PRIORITY**

Promoted ahead of pool-wide fixer rollout given two new findings from §1: (a) strong cross-cluster correlation between scheduled backups and corruption, and (b) confirmation that per-VDI fixes don't hold. Rolling out the extended fixer pool-wide before understanding the trigger risks fixing everything and having it silently re-corrupt on the next backup cycle — this phase exists to avoid that.

**Why the dev clusters are the right testbed:** both are currently clean, run the same XCP-ng version and `lvmoiscsi` storage as production, and have no scheduled backups — i.e. they isolate the one variable that differs from production.

**Proposed methodology:**

1. **Mirror the trigger, not just the schedule.** Export the exact backup job configuration from an affected production cluster (job type — full vs. delta/CBT, retention/rotation settings, concurrent-job count, target) and replicate it as closely as possible on one dev cluster, rather than approximating with a generic snapshot schedule. If multiple job *types* exist in production (e.g. both rolling snapshot and continuous replication), test them separately if possible — the upstream root cause discussion (§2) implicates snapshot **revert** specifically, so CR/mirroring jobs vs. plain rolling-snapshot jobs may not be equally implicated.
2. **Build a representative VM set.** Include at least one VM shaped like the ones showing findings in production — real disk with a delta/CBT-style snapshot lineage — since the corruption trigger may be lineage-shape-dependent, not just "any backup."
3. **Instrument tightly around each cycle.** Before and after every individual step of a backup run (snapshot creation → export/replication → snapshot deletion/coalesce), snapshot the state with `xcp-vdi-unified-audit.py --json` and diff against the prior run. This should pinpoint which *specific* XAPI/SM operation flips `is-a-snapshot`/`snapshot-of` (or the `sm-config` equivalent — see §2) rather than just "corruption appeared sometime during the job."
4. **Cross-reference `SMlog`/`xensource.log`** for the exact timestamp window identified in step 3, to get the actual XAPI/SM call sequence rather than inferring it.
5. **Once reproduced reliably, directly test the all-or-nothing hypothesis (§1):** fix one VDI on the dev cluster with the extended fixer (once built, see Phase 2), leave a sibling VDI unfixed, run another backup cycle, and audit both. If only the previously-corrupted-and-now-fixed VDI re-corrupts, that supports "independent ongoing exposure" (§1) rather than a genuine cross-VDI all-or-nothing dependency — directly resolving the open question from §1.
6. **Document the exact trigger** once isolated, and feed it into both the extended fixer design (Phase 2 — e.g. the fixer may need to run *after* every backup job rather than as a one-off) and the upstream report (Phase 3, forum #11715).

This phase can run in parallel with hardening the extended fixer (Phase 2 below), but **pool-wide rollout of the fixer against production should wait for at least a preliminary answer from this phase** — otherwise there's a real risk of spending a maintenance window fixing every VDI in a pool only to have the next night's backup job re-corrupt some or all of them.

### Phase 2 — Remediate per bucket — **fixer development active; pool-wide rollout gated on Phase 1.5**

**Metadata-only (confirmed 100% of affected pools), per pool in a maintenance window — hold pool-wide rollout until Phase 1.5 has at least a preliminary trigger finding:**

1. **Pause XO backup schedules** for the pool first — don't let the suspected trigger race the rewrite.
2. `xe pool-dump-database file-name=...` as a second backup beyond the `state.db` copy.
3. Run the **extended fixer** (to be built on top of Vates' `snapshot-fixer.py`):
   - fix the `is-a-snapshot`/`is_a_snapshot` hyphen bug (§5, and matches Vates' own acknowledged bug upstream);
   - drive fixes from audit findings (from `xcp-vdi-unified-audit.py`), not just the two hardcoded patterns — including the sm-config-layer `D1-SUPER-PARENT` variant identified in §2, which the official script doesn't touch at all;
   - null bogus `snapshot_of`, clear wrong `is_a_snapshot`, scrub stale `snapshot_time`, and clean parents' `snapshots` lists (reciprocal integrity) — closing the gap identified in §5;
   - keep dry-run / rewrite / restore-backup semantics.
4. Restart, re-run the unified audit, require zero CRITICAL/HIGH before re-enabling backups.
5. **New requirement given §1's findings:** don't treat a clean post-fix audit as the finish line — schedule a follow-up audit after the pool's next scheduled backup cycle completes, specifically to check whether the fix held. This is now a known risk, not a hypothetical one.

**This is the only path required for the metadata itself** — every pool classified `METADATA-ONLY` in Phase 1, so the migration path below is retained for reference/future recurrence but isn't on the critical path right now. **What's gating the timeline is Phase 1.5, not the metadata bucket determination.**

**Genuinely tangled chains (not currently applicable — no pool triggered this):** do **not** DB-edit. Storage-migrate (or offline `vdi-copy`) the affected VMs' disks to the spare SR — this writes a brand-new single-VM chain *and* fresh xapi records in one operation (this is the mechanism behind why "migrate to another SR and back" fixed it for multiple forum reporters — see §4). Let GC clean the source SR afterwards.

**The super-parent VDI itself:** only after its claimed children are cleaned, and only if `vhd-util scan` shows nothing chained to it, use `xe vdi-forget` (drops the record, leaves the LV for `sr-scan` to reconcile) rather than `vdi-destroy`.

### Phase 3 — Recurrence prevention (post-root-cause)

Fixing the DB doesn't stop re-corruption — now confirmed directly in this environment (§1), not just via community reports. Once Phase 1.5 identifies the trigger:

- **Early detection (do this regardless of Phase 1.5 outcome):** cron the unified audit (`--skip-info --json`) on each pool master and alert on non-zero exit, catching recurrence before disks vanish in XO. This is cheap insurance even before the trigger is fully understood.
- **Upstream:** post the cross-cluster correlation data and reproduction findings to forum thread [#11715](https://xcp-ng.org/forum/topic/11715/vdi-not-showing-in-xo-5-from-source) — this is exactly the kind of per-environment trigger data Vates said would help land a real upstream fix, and the 3-affected/2-clean split with matched versions is unusually clean evidence.
- **Depending on Phase 1.5 result:** either close the trigger directly (e.g. if it's a specific backup job type or CBT interaction that can be avoided/reconfigured), or accept it as a standing risk and make the extended fixer a recurring/cron'd operation rather than a one-off remediation.

### Rollout order

1. ~~Build the vhd-util-vs-xapi cross-check script~~ ✅ done, validated on `10.88.88.124`.
2. ~~Run audit + ground-truth scan on all affected pools; classify~~ ✅ done — all pools `METADATA-ONLY`.
3. **Reproduce the corruption intentionally on a dev cluster and isolate the trigger (Phase 1.5). ← current step**
4. Build/harden the extended metadata fixer (§5 gaps) in parallel — not blocked on step 3, but its pool-wide rollout is.
5. Pilot the extended fixer on the least-critical affected pool in a maintenance window, **including a post-next-backup-cycle follow-up audit**; verify the fix actually holds.
6. Roll out to remaining pools (no migration needed — all confirmed metadata-only).
7. Stand up the cron audit + log correlation; report findings upstream to #11715.

---

## 7. Immediate Next Actions

- [x] Write the `vhd-util` ⇄ xapi cross-check/classification script (`xcp-vhd-chain-crosscheck.py`, validated on reference host 2026-06-10)
- [x] Fix the xe-parser whitespace bug in `xcp-vdi-unified-audit.py` (previous runs of that script produced invalid/empty results)
- [x] **Re-run the unified audit on all affected pools** — done; verdict METADATA-ONLY across the board, no physical entanglement
- [x] Run `xcp-vhd-chain-crosscheck.py collect` on affected pool masters and classify — complete for all pools
- [x] Establish cross-cluster correlation data (3 prod affected w/ backups vs. 2 dev clean w/o backups) — done, see §1
- [ ] **Design and execute Phase 1.5 controlled reproduction on a dev cluster (§6) — top priority, gates pool-wide remediation rollout**
- [ ] Export the exact backup job config(s) from an affected production cluster to replicate on the dev testbed
- [ ] Instrument the dev reproduction with before/after `xcp-vdi-unified-audit.py --json` diffs per backup-cycle step, plus `SMlog`/`xensource.log` correlation
- [ ] Once reproduced, test the all-or-nothing hypothesis directly (fix one VDI, leave a sibling unfixed, re-run the trigger, compare)
- [ ] Harden `snapshot-fixer.py` into the extended fixer (§5/§6 gaps: hyphen bug, parent-side `snapshots` cleanup, `snapshot_time` scrub, audit-driven fixes, `D1-SUPER-PARENT`/sm-config coverage) — can proceed in parallel with Phase 1.5, but hold pool-wide rollout until Phase 1.5 has a preliminary answer
- [ ] Pilot the extended fixer on the least-critical affected pool in a maintenance window; require zero CRITICAL/HIGH on re-audit **and** a clean follow-up audit after the next backup cycle before rollout
- [ ] Sync the latest fixed fork of `xcp-vdi-unified-audit.py` into this directory (currently stale)
- [ ] Cross-check findings against `snapshot-fixer.py --dry-run` output before touching anything in the Health Dashboard — several confirmed false-positive "orphans" exist upstream (§1)
- [ ] Stand up cron audit (`--skip-info --json`) on each pool master — worth doing now regardless of Phase 1.5 status, as cheap early-warning insurance
- [ ] Once the trigger is isolated, write up and post the cross-cluster correlation + reproduction findings to forum thread #11715

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
- ✅ **Community-reported recurrence post-fix is now independently confirmed in this environment** — individual VDI fixes via SR migration do not hold long-term
- ✅ **Cross-cluster correlation established:** all 3 production clusters (scheduled backups) affected; both dev clusters (no backups) clean, with version/storage held constant — strongly implicates the backup pipeline as trigger
- ❌ No upstream fix path for XCP-ng 8.2.1 (EOL)
- ✅ This environment: read-only diagnostic tooling (audit + ground-truth cross-checker) built and validated on a clean reference host
- ✅ This environment: Phase 1 complete — all affected pools classified `METADATA-ONLY`, zero physical chain entanglement. No storage migration required anywhere.
- ⏳ **This environment: Phase 1.5 (controlled reproduction on a dev cluster to isolate the exact trigger) is now the critical path — pool-wide fixer rollout is gated on this, not just on fixer readiness**

---

## 10. References

- Forum: [VDI not showing in XO 5 from Source (#11715)](https://xcp-ng.org/forum/topic/11715/vdi-not-showing-in-xo-5-from-source) — 56 posts, primary community thread
- Forum: [XenOrchestra not showing VM Disks on Pool (#12152)](https://xcp-ng.org/forum/topic/12152/xenorchestra-not-showing-vm-disks-on-pool-on-single-server-working-xcp-ng-center-is-showing-them)
- GitHub: [vatesfr/xen-orchestra#9578](https://github.com/vatesfr/xen-orchestra/issues/9578)
- GitHub: [vatesfr/xen-orchestra PR #9231 (commit 85596da)](https://github.com/vatesfr/xen-orchestra/commit/85596da79217070bf4431135bbb5b0d2cf04e45b)
- GitHub: [vatesfr/xen-orchestra PR #9381](https://github.com/vatesfr/xen-orchestra/pull/9381)
- GitHub: [xcp-ng/xcp — snapshot-fixer.py](https://github.com/xcp-ng/xcp/blob/master/scripts/snapshot-fixer.py)
- Internal: `xcp-vdi-unified-audit.py` (evolved in place from a single `A1-SELF-REF` check to a 20-check catalog A1–G1; consolidates 4 source scripts — see §5)
- Internal: `xcp-vdi-graph-audit.sh` (superseded bash audit; one of the 4 consolidated sources)
- Internal: `xcp-vhd-chain-crosscheck.py` (ground-truth data-layer tool)
- Internal: `sample-crosscheck-data/` (offline test fixture)
