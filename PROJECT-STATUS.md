# XCP-ng VDI Snapshot-Metadata Corruption — Project Status & Remediation Plan

*Last updated: 2026-06-10 (cross-check tool built & validated; critical parser bug
fixed in unified auditor — see §2)*

## 1. Problem Statement

Several XCP-ng 8.3.x pools (LVM/iSCSI SRs) have xapi snapshot-metadata corruption with
two visible symptoms:

1. **XO 5 hides VM disks** — live disks carry snapshot-flavored metadata
   (`is-a-snapshot=true`, or `is-a-snapshot=false` with `snapshot-of` populated), and
   Xen Orchestra classifies them as snapshots and removes them from the VM disk panel.
2. **A "super parent" VDI** is named as `snapshot-of` by VDIs spanning unrelated VMs
   and even ISO SRs — one bad VDI appears to "own" everything as its snapshots.

**Central open question:** are the actual VHD chains on disk tangled, or is the
corruption confined to the xapi database — i.e. misbehaving snapshot bookkeeping
and/or fast-clone VDIs that never fully separated from their parent disks?

### Suspected trigger

XO rolling-snapshot backup jobs and/or snapshot-revert cycles. Per-VM correlation has
**not yet been done**. External context:
[XCP-ng forum thread 11715](https://xcp-ng.org/forum/topic/11715/vdi-not-showing-in-xo-5-from-source.?lang=en-US):

- Affected VDIs there showed exactly the `is-a-snapshot: false` + `snapshot-of: <uuid>`
  contradiction, appearing **after snapshot/revert operations**.
- xo-server commit `fix(xo-server): improve handling of xapi snapshots (#9231)` made
  XO 5 treat any VDI with `snapshot_of` set as a snapshot — so part of the visibility
  symptom is an XO-side regression sitting on top of stale (previously tolerated)
  xapi metadata.
- VM workloads, backups, and snapshots kept working — display/management layer only.
- Reported workarounds: halt → snapshot → revert → delete-snapshots cycle, or
  migrating the VM to another SR and back (both force fresh metadata).
- Vates staff (Anthoine B, Mathieu RA, Olivier Lambert) were investigating.

### Constraints & environment

| Item | Status |
|---|---|
| XCP-ng version | 8.3.x on affected pools |
| SR types | LVM-based (lvmoiscsi / iSCSI) |
| VM provenance | Mixed (clones, fresh installs, imports — varies by pool) |
| Downtime windows | Available |
| Spare SR capacity | Available (enables migration-based remediation) |
| XO | Kept up to date; pool-side fix still expected to be needed (XML state) |
| Access from this workstation | Clean reference host only: `root@10.88.88.124` (no corruption; used for xe CLI shape). Affected pools are not reachable — data is relayed. |

## 2. Tooling Inventory (this directory)

### `snapshot-fixer.py` — the only **write** tool
Vates-style fixer. Disables HA, stops xapi, backs up `/var/lib/xcp/state.db`, edits the
XML directly, restarts. Fixes only two patterns:
non-snapshot records with `snapshot_of` set → nulled; self-referential VDI snapshots →
nulled + unflagged. Subcommands: `dry-run`, `rewrite`, `restore-backup`.

- **Why XML editing is necessary:** `is-a-snapshot` and `snapshot-of` are read-only
  through xapi — there is no `xe vdi-param-set` path. Pool-side correction requires the
  stop-xapi/rewrite approach (or VDI migration, which creates fresh records).
- **Known bug:** the self-reference branch checks `record.get('is-a-snapshot')`
  (hyphens), but `state.db` XML attributes use underscores (`is_a_snapshot`), so that
  branch never fires (`snapshot-fixer.py:113`).
- **Known gap:** it never cleans the parent side — stale entries in parents'
  `snapshots` lists and bogus `snapshot_time` values survive the fix, so reciprocal-link
  findings (B1/B2 below) persist after a run. Confirmed observation: "it works but
  doesn't address the root cause."

### `xcp-vdi-graph-audit.sh` — read-only bash audit (superseded)
Pool-master bash 4.2-compatible audit, 17 finding categories, severity-ranked report
with ghost-UUID clustering. Detection logic was folded into the unified auditor.

### `xcp-vdi-unified-audit-v2.py` — read-only consolidated auditor (current)
20 checks (A1–G1: self-refs, contradictions, circular chains, orphans, broken
reciprocal links, duplicate parent claims, snapshots-on-live-VMs, vhd-parent issues,
cross-SR refs, corrupt snapshot roots, super-parents, dangling VBDs, VM-level issues,
missing VDIs). Classifies VMs (REAL-VM / VM-SNAPSHOT / XO-BACKUP / TEMPLATE /
CONTROL-DOMAIN), outputs text/JSON/CSV, exits non-zero on CRITICAL/HIGH.

It consolidates two scripts referenced in its header that are **not** in this
directory: `super-parent-finder.sh` and `vdi-snapshot-metadata-audit-v2.sh`.

> **PARSER BUG (fixed 2026-06-10):** the xe-output field regex
> (`^(\S.*?)\s*\(...`) rejected leading whitespace, but xe indents every field line
> except `uuid`. Every field was silently swallowed as a "continuation" of the uuid
> line, so this copy of the auditor parsed all records as effectively empty.
> Fixed here by allowing leading whitespace (`^\s*(\S.*?)...`); verified on the
> reference host (now reports real findings: 6 HIGH B1-ORPHANED-SNAPSHOT,
> 4 G1-VDI-MISSING, 1 A2-CONTRADICTION there — note B1-ORPHANED-SNAPSHOT can be
> benign: a kept snapshot whose source disk was deliberately deleted).
>
> Daniel had independently found and fixed this same bug in his fork of the tool —
> the copy in this directory was the stale pre-fix version. Audit results produced
> by the *fixed fork* remain valid; only results from this stale copy were garbage.
> TODO: sync the latest fork into this directory so it stays the single source of
> truth.

### `xcp-vhd-chain-crosscheck.py` — data-layer ground truth (NEW, built 2026-06-10)
The Phase 1 tool: joins `vhd-util scan` physical VHD trees with xapi metadata and
classifies **every `snapshot-of` claim** into:

- `DB-GARBAGE` — no physical basis (target absent from VHD tree, child/target in
  unrelated trees, cross-SR claim, raw-LV involvement, self-reference). Safe to null
  in state.db.
- `PHYSICAL-RELATED` — child and target share a common physical ancestor (the normal
  snapshot/clone sibling-under-hidden-base layout). Chain is fine; only flags may
  need fixing.
- `UNVERIFIABLE` — not visible to vhd-util; manual review.

Independently detects physical anomalies: active (hidden=0) VHDs with children
(never normal — strong tangle indicator), parent-link cycles, dangling physical
parents, trees whose active leaves span multiple real VMs (fast-clone vs. tangle,
with the hidden/active share-point as the discriminator), stale `sm-config:vhd-parent`
vs. physical parent, orphan active VHDs, and VDIs with no backing VHD/LV.

Emits an overall verdict: `CLEAN` / `METADATA-ONLY` (state.db cleanup sufficient) /
`PHYSICAL-ENTANGLEMENT` (migration needed; exit code 2).

Designed around the access constraint (affected pools unreachable from the
analysis machine):

```sh
# on the affected pool master (read-only: xe list/vhd-util scan/lvs only):
python3 xcp-vhd-chain-crosscheck.py collect --out /root/crosscheck-data
# copy the directory back, then anywhere:
python3 xcp-vhd-chain-crosscheck.py analyze --from ./crosscheck-data [--json out.json]
# or both at once on the master:
python3 xcp-vhd-chain-crosscheck.py run [--keep DIR] [--json FILE]
```

Python 3.6.8 stdlib only (verified against the host's interpreter). Validated
end-to-end on the reference host 2026-06-10: 44 VHDs across 2 LVM SRs, 11
snapshot-of claims all correctly classified PHYSICAL-RELATED, verdict CLEAN; offline
`analyze` verified on the workstation against `./sample-crosscheck-data/` (kept in
this directory as a test fixture).

### The three layers
1. **xapi DB fields** (`is-a-snapshot`, `snapshot-of`, `snapshots`) — pure bookkeeping.
   What XO reads, what the audits check, what the fixer edits. Corruption here breaks
   display/management, never data.
2. **sm-config `vhd-parent`** — the storage manager's cached view (partially audited).
3. **Actual VHD chains in the LVM VG** — ground truth, now covered by
   `xcp-vhd-chain-crosscheck.py`.

Corollary: a shared VHD parent across "unrelated" VMs is **not inherently corruption**
— fast clones legitimately share a hidden read-only base copy until coalesce separates
them — so the `C3-CHAIN-CROSSOVER` check can flag normal clone topology. Only the data
layer can disambiguate.

## 3. Recommended Plan

### Phase 1 — Data-layer ground truth (read-only, no downtime) — **TOOL BUILT ✅**

`xcp-vhd-chain-crosscheck.py` (see §2) implements this phase. Remaining Phase 1 work
is execution, per affected pool:

1. Copy the script to the pool master; run `collect --out DIR` (read-only).
2. Copy DIR back; run `analyze --from DIR --json pool-<name>.json`.
3. Read the verdict: `METADATA-ONLY` → Phase 2 metadata path;
   `PHYSICAL-ENTANGLEMENT` → Phase 2 migration path for the flagged trees.
4. Additionally run `xe sr-scan` per SR and review `/var/log/SMlog` for coalesce
   errors — a perpetually failing coalesce would explain clones that never separated
   (not yet automated).

The three-bucket model the tool applies:

| Bucket | Evidence | Disposition |
|---|---|---|
| DB-GARBAGE | Claim has no physical basis in the VHD tree | Safe to null in state.db; record deletable if no backing LV |
| PHYSICAL-RELATED | Child and target share a physical ancestor (clone/snapshot layout) | Fix flags only; **never** touch the VHD |
| Genuinely tangled (physical anomalies) | Active-VHD-with-children, cycles, active shared roots | Data-layer remediation (migration) |

This directly answers the tangled-vs-never-separated question.

### Phase 2 — Remediate per bucket

**Metadata-only (expected majority), per pool in a maintenance window:**

1. **Pause XO backup schedules** for the pool first — don't let the suspected trigger
   race the rewrite.
2. `xe pool-dump-database file-name=...` as a second backup beyond the state.db copy.
3. Run the **extended fixer** (to be built from `snapshot-fixer.py`):
   - fix the `is-a-snapshot`/`is_a_snapshot` hyphen bug;
   - drive fixes from audit findings, not just the two hardcoded patterns;
   - null bogus `snapshot_of`, clear wrong `is_a_snapshot`, scrub stale
     `snapshot_time`, and clean parents' `snapshots` lists (reciprocal integrity);
   - keep dry-run / rewrite / restore-backup semantics.
4. Restart, re-run the unified audit, require zero CRITICAL/HIGH before re-enabling
   backups.

**Genuinely tangled chains:** do **not** db-edit. Storage-migrate (or offline
`vdi-copy`) the affected VMs' disks to the spare SR — this writes a brand-new
single-VM chain *and* fresh xapi records in one operation (which is why migration
"fixed it" in the forum thread). Let GC clean the source SR afterwards.

**The super-parent VDI itself:** only after its claimed children are cleaned, and only
if `vhd-util scan` shows nothing chained to it, use `xe vdi-forget` (drops the record,
leaves the LV for `sr-scan` to reconcile) rather than `vdi-destroy`.

### Phase 3 — Root cause & recurrence prevention

Fixing the DB doesn't stop re-corruption.

- **Forensic lead:** bogus `snapshot-time` values on A2-contradiction VDIs timestamp
  when the corruption happened. Correlate them with XO backup-job logs and
  `xensource.log` to identify the exact operation (interrupted rolling snapshot vs.
  revert).
- **Early detection:** cron the unified audit (`--skip-info --json`) on each pool
  master and alert on non-zero exit, catching recurrence before disks vanish in XO.
- **Upstream:** post correlation results to forum thread 11715 — Vates was actively
  investigating, and per-VM trigger data would help land a real upstream fix.

### Rollout order

1. Build the vhd-util-vs-xapi cross-check script (output shape testable against
   `10.88.88.124`).
2. Run audit + ground-truth scan on the **least critical** affected pool; classify.
3. Pilot the extended metadata fixer there in a window; verify clean re-audit.
4. Roll out to remaining pools; migrate any genuinely tangled VMs.
5. Stand up the cron audit + log correlation.

## 4. Immediate Next Actions

- [x] Write the `vhd-util` ⇄ xapi cross-check/classification script
      (`xcp-vhd-chain-crosscheck.py`, validated on reference host 2026-06-10)
- [x] Fix the xe-parser whitespace bug in `xcp-vdi-unified-audit-v2.py`
      (previous runs of that script produced invalid/empty results)
- [ ] **Re-run the unified audit on all affected pools** — pre-fix results are void
- [ ] Run `xcp-vhd-chain-crosscheck.py collect` on one affected pool master and
      relay the directory back for the first real classification pass
- [ ] Harden `snapshot-fixer.py` (hyphen bug at line 113, parent-side `snapshots`
      list cleanup, `snapshot_time` scrub, audit-driven fixes)
- [ ] Correlate A2 `snapshot-time` values with XO backup/revert history on that pool

## 5. Validation Log

| Date | What | Result |
|---|---|---|
| 2026-06-10 | xe output format, Python 3.6.8, vhd-util presence on reference host | Confirmed; parsers built against real output |
| 2026-06-10 | First cross-check run on reference host | Exposed the leading-whitespace parser bug (0 records parsed) |
| 2026-06-10 | Regex fix applied to cross-check + unified auditor | Confirmed via direct regex test against real xe lines |
| 2026-06-10 | Cross-check `run` on reference host (lvm + lvmoiscsi SRs) | 44 VHDs, 11 claims all PHYSICAL-RELATED, verdict CLEAN, exit 0 |
| 2026-06-10 | Cross-check `analyze --from` offline on workstation | Identical report from `sample-crosscheck-data/`; collect/relay workflow proven |
| 2026-06-10 | Repaired unified audit on reference host | Now parses real records; 11 findings (6 B1-ORPHANED-SNAPSHOT HIGH, 4 G1, 1 A2) on a physically clean host |
