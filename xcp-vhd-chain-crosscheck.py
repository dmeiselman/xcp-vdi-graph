#!/usr/bin/env python3
"""
xcp-vhd-chain-crosscheck.py — Cross-check xapi snapshot metadata against the
physical VHD chains reported by vhd-util.

Answers the question the xapi-only audits cannot: is the snapshot-metadata
corruption (super-parents, bogus snapshot-of, hidden disks) backed by actually
tangled VHD chains on disk, or is it database-layer noise on top of healthy
storage?

Every snapshot-of claim in xapi is classified into one of three buckets:

  DB-GARBAGE        claim has no physical basis (target absent from the VHD
                    tree, or child and target live in unrelated physical
                    trees, or the claim crosses an SR boundary). Safe to null
                    in state.db; the data layer is unaffected.
  PHYSICAL-RELATED  child and target share a common physical ancestor (the
                    normal snapshot/clone sibling layout). The VHD chain is
                    consistent; only flags may need fixing.
  UNVERIFIABLE      child or target not visible to vhd-util (raw LV, other
                    SR type, scan failure). Needs manual review.

Independently, the physical trees themselves are checked for genuine
entanglement: active leaves of one tree owned by multiple unrelated VMs
(shared chain — legitimate for fast clones, suspicious otherwise), active
VHDs that have children (never normal), and parent-link cycles.

Modes (designed for pools not reachable from the analysis machine):

  collect --out DIR      run on the pool master: dump raw xe + vhd-util
                         output into DIR (copy DIR back for analysis)
  analyze --from DIR     run anywhere: analyze a collected directory
  run                    collect to a temp dir and analyze, on the master

Requires only the Python 3.6 standard library.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime

UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

VHD_SR_TYPES = ("lvm", "lvmoiscsi", "lvmohba", "ext", "nfs", "smb")
LVM_SR_TYPES = ("lvm", "lvmoiscsi", "lvmohba")

COLLECT_FILES = {
    "sr-list.txt":  ["sr-list", "params=uuid,name-label,type,content-type"],
    "vdi-list.txt": ["vdi-list",
                     "params=uuid,name-label,sr-uuid,is-a-snapshot,snapshot-of,"
                     "snapshot-time,snapshots,type,managed,missing,read-only,sm-config"],
    "vbd-list.txt": ["vbd-list",
                     "params=uuid,vm-uuid,vdi-uuid,type,currently-attached,userdevice,empty"],
    "vm-list.txt":  ["vm-list",
                     "params=uuid,name-label,is-a-snapshot,is-a-template,"
                     "is-control-domain,snapshot-of,power-state,other-config"],
}


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd, timeout=300):
    """Run a command, return (rc, stdout, stderr). Never raises on rc!=0."""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out: %s" % " ".join(cmd)
    except FileNotFoundError:
        return 127, "", "not found: %s" % cmd[0]


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

# NB: xe indents every field line except 'uuid' — the leading \s* is load-bearing.
FIELD_RE = re.compile(r'^\s*(\S.*?)\s*\(\s*(?:RO|RW|MRO|MRW|SRO|SRW)\s*\)\s*:\s?(.*)')


def parse_xe_list(output):
    """Parse 'xe <type>-list params=...' output into a list of dicts."""
    records, current = [], {}
    for line in output.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        m = FIELD_RE.match(line)
        if m:
            current[m.group(1).strip()] = m.group(2).strip()
        elif current:
            last = list(current.keys())[-1]
            current[last] += " " + line.strip()
    if current:
        records.append(current)
    return records


def norm(val):
    if val in ("<not in database>", "not in database"):
        return ""
    return val


def parse_map_field(val):
    """Parse 'k1: v1; k2: v2' map fields (sm-config, other-config)."""
    result = {}
    for entry in (val or "").split("; "):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(": ", 1)
        result[parts[0].strip()] = parts[1].strip() if len(parts) == 2 else ""
    return result


SCAN_LINE_RE = re.compile(r'^\s*vhd=(\S+)\s+capacity=(\d+)\s+size=(\d+)\s+'
                          r'hidden=(\d)\s+parent=(\S+)')


def extract_uuid(token):
    """Pull a UUID out of a vhd=/parent= token (VHD-<uuid>, path/<uuid>.vhd)."""
    if not token or token == "none":
        return ""
    m = UUID_RE.search(token)
    return m.group(0) if m else ""


def parse_vhd_scan(output, sr_uuid):
    """Parse vhd-util scan output. Returns (nodes, error_lines).
    nodes: uuid -> dict(parent, hidden, capacity, size, sr)."""
    nodes, errors = {}, []
    for line in output.splitlines():
        if not line.strip():
            continue
        m = SCAN_LINE_RE.match(line)
        if m:
            uuid = extract_uuid(m.group(1))
            if not uuid:
                errors.append(line.strip())
                continue
            nodes[uuid] = {
                "parent": extract_uuid(m.group(5)),
                "hidden": m.group(4) == "1",
                "capacity": int(m.group(2)),
                "size": int(m.group(3)),
                "sr": sr_uuid,
            }
        elif "vhd=" in line or "error" in line.lower() or "failed" in line.lower():
            errors.append(line.strip())
    return nodes, errors


def parse_lvs(output):
    """Extract raw-LV VDI uuids (LV-<uuid>) from lvs output."""
    raw = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("LV-"):
            u = extract_uuid(line)
            if u:
                raw.add(u)
    return raw


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect(outdir, verbose=False):
    """Run on a pool master: dump raw xe + vhd-util output into outdir."""
    rc, _, _ = run_cmd(["xe", "help"], timeout=30)
    if rc == 127:
        sys.exit("[FATAL] 'xe' not found — collect mode must run on an XCP-ng host.")

    os.makedirs(outdir, exist_ok=True)

    def save(name, content):
        with open(os.path.join(outdir, name), "w") as fh:
            fh.write(content)
        if verbose:
            print("[INFO] wrote %s (%d bytes)" % (name, len(content)), file=sys.stderr)

    for fname, xe_args in COLLECT_FILES.items():
        rc, out, err = run_cmd(["xe"] + xe_args)
        if rc != 0:
            print("[WARN] xe %s failed: %s" % (" ".join(xe_args), err.strip()),
                  file=sys.stderr)
        save(fname, out)

    # vhd-util scan per VHD-capable SR
    srs = parse_xe_list(open(os.path.join(outdir, "sr-list.txt")).read())
    scanned = []
    for sr in srs:
        sr_uuid = sr.get("uuid", "")
        sr_type = sr.get("type", "")
        if not sr_uuid or sr_type not in VHD_SR_TYPES:
            continue
        if sr_type in LVM_SR_TYPES:
            vg = "VG_XenStorage-" + sr_uuid
            rc, out, err = run_cmd(["vhd-util", "scan", "-f", "-m", "VHD-*",
                                    "-l", vg, "-p"])
            rc2, lvs_out, _ = run_cmd(["lvs", "--noheadings", "-o", "lv_name", vg])
            save("lvs-%s.txt" % sr_uuid, lvs_out if rc2 == 0 else "")
        else:
            mount = "/var/run/sr-mount/%s" % sr_uuid
            if not os.path.isdir(mount):
                continue
            rc, out, err = run_cmd(["vhd-util", "scan", "-f", "-p",
                                    "-m", "%s/*.vhd" % mount])
        save("vhd-scan-%s.txt" % sr_uuid, out + ("\n" + err if err.strip() else ""))
        scanned.append(sr_uuid)
        if verbose:
            print("[INFO] scanned SR %s (%s)" % (sr_uuid, sr_type), file=sys.stderr)

    rc, host, _ = run_cmd(["hostname"])
    save("meta.json", json.dumps({
        "hostname": host.strip(),
        "collected_utc": datetime.utcnow().isoformat() + "Z",
        "scanned_srs": scanned,
    }, indent=2))
    print("[INFO] collection complete: %s" % outdir, file=sys.stderr)


# ---------------------------------------------------------------------------
# Pool model (built from a collected directory)
# ---------------------------------------------------------------------------

class Pool:
    def __init__(self):
        self.srs = {}          # uuid -> {name, type, content}
        self.vdis = {}         # uuid -> dict of xapi fields
        self.vbds = {}         # uuid -> dict
        self.vms = {}          # uuid -> dict (incl. vm_class)
        self.vhd = {}          # uuid -> physical node (all SRs merged)
        self.raw_lvs = set()   # raw-LV VDI uuids
        self.scan_errors = {}  # sr uuid -> [lines]
        self.scanned_srs = []
        self.meta = {}
        self._children = None  # vhd parent -> [child uuids]

    # -- loading ------------------------------------------------------------

    def load(self, srcdir):
        def read(name):
            path = os.path.join(srcdir, name)
            return open(path).read() if os.path.isfile(path) else ""

        meta_raw = read("meta.json")
        self.meta = json.loads(meta_raw) if meta_raw else {}

        for rec in parse_xe_list(read("sr-list.txt")):
            u = rec.get("uuid", "")
            if u:
                self.srs[u] = {"name": rec.get("name-label", ""),
                               "type": rec.get("type", ""),
                               "content": rec.get("content-type", "")}

        for rec in parse_xe_list(read("vdi-list.txt")):
            u = rec.get("uuid", "")
            if not u:
                continue
            sm = parse_map_field(rec.get("sm-config", ""))
            self.vdis[u] = {
                "name": rec.get("name-label", ""),
                "sr": norm(rec.get("sr-uuid", "")),
                "is_snap": rec.get("is-a-snapshot", "false") == "true",
                "snap_of": norm(rec.get("snapshot-of", "")),
                "snap_time": rec.get("snapshot-time", ""),
                "snapshots": [s.strip() for s in
                              norm(rec.get("snapshots", "")).split(";") if s.strip()],
                "type": rec.get("type", ""),
                "managed": rec.get("managed", "true") == "true",
                "missing": rec.get("missing", "false") == "true",
                "sm_vhd_parent": sm.get("vhd-parent", ""),
                "sm_vdi_type": sm.get("vdi_type", ""),
            }

        for rec in parse_xe_list(read("vbd-list.txt")):
            u = rec.get("uuid", "")
            if u:
                self.vbds[u] = {
                    "vm": norm(rec.get("vm-uuid", "")),
                    "vdi": norm(rec.get("vdi-uuid", "")),
                    "type": rec.get("type", ""),
                    "device": rec.get("userdevice", ""),
                    "empty": rec.get("empty", "false") == "true",
                }

        for rec in parse_xe_list(read("vm-list.txt")):
            u = rec.get("uuid", "")
            if not u:
                continue
            oc = parse_map_field(rec.get("other-config", ""))
            is_snap = rec.get("is-a-snapshot", "false") == "true"
            is_templ = rec.get("is-a-template", "false") == "true"
            is_cd = rec.get("is-control-domain", "false") == "true"
            if is_cd:
                cls = "CONTROL-DOMAIN"
            elif is_templ and not is_snap:
                cls = "TEMPLATE"
            elif is_snap:
                cls = "XO-BACKUP" if any(k.startswith("xo:backup") for k in oc) \
                      else "VM-SNAPSHOT"
            else:
                cls = "REAL-VM"
            self.vms[u] = {"name": rec.get("name-label", ""),
                           "class": cls,
                           "snap_of": norm(rec.get("snapshot-of", ""))}

        for fname in sorted(os.listdir(srcdir)):
            if fname.startswith("vhd-scan-") and fname.endswith(".txt"):
                sr_uuid = fname[len("vhd-scan-"):-len(".txt")]
                nodes, errors = parse_vhd_scan(read(fname), sr_uuid)
                self.vhd.update(nodes)
                if errors:
                    self.scan_errors[sr_uuid] = errors
                self.scanned_srs.append(sr_uuid)
            elif fname.startswith("lvs-") and fname.endswith(".txt"):
                self.raw_lvs |= parse_lvs(read(fname))

        self._children = defaultdict(list)
        for u, node in self.vhd.items():
            if node["parent"]:
                self._children[node["parent"]].append(u)

    # -- physical-tree helpers ----------------------------------------------

    def ancestors(self, uuid):
        """Physical ancestor chain of a VHD (incl. itself). Cycle-safe:
        returns (ordered list, cycle_detected)."""
        chain, seen = [], set()
        cur = uuid
        while cur and cur in self.vhd:
            if cur in seen:
                return chain, True
            seen.add(cur)
            chain.append(cur)
            cur = self.vhd[cur]["parent"]
        if cur and cur not in self.vhd:
            chain.append(cur)  # dangling parent ref — keep it visible
        return chain, False

    def physical_root(self, uuid):
        chain, _ = self.ancestors(uuid)
        return chain[-1] if chain else uuid

    def tree_members(self, root):
        out, stack = [], [root]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(self._children.get(cur, []))
        return out

    # -- ownership ----------------------------------------------------------

    def vdi_owner_vms(self, vdi_uuid, rollup=True):
        """Real VMs reachable from a VDI via VBDs. Snapshot/backup VMs roll
        up to their source VM when it exists."""
        owners = set()
        for vbd in self.vbds.values():
            if vbd["vdi"] != vdi_uuid or not vbd["vm"]:
                continue
            vm_uuid = vbd["vm"]
            vm = self.vms.get(vm_uuid)
            if not vm:
                owners.add(vm_uuid)
                continue
            if rollup and vm["class"] in ("VM-SNAPSHOT", "XO-BACKUP") and vm["snap_of"]:
                src = self.vms.get(vm["snap_of"])
                if src:
                    owners.add(vm["snap_of"])
                    continue
            if vm["class"] in ("REAL-VM", "VM-SNAPSHOT", "XO-BACKUP"):
                owners.add(vm_uuid)
        return owners

    def vm_label(self, vm_uuid):
        vm = self.vms.get(vm_uuid)
        if not vm:
            return "%s (?)" % vm_uuid
        return "%s [%s, %s]" % (vm_uuid, vm["name"], vm["class"])

    def vdi_label(self, vdi_uuid):
        vdi = self.vdis.get(vdi_uuid)
        if not vdi:
            return "%s (no xapi record)" % vdi_uuid
        sr = self.srs.get(vdi["sr"], {})
        return "%s [%s] sr=%s" % (vdi_uuid, vdi["name"], sr.get("name", vdi["sr"]))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def classify_claims(pool, results):
    """Classify every xapi snapshot-of claim against the physical layer."""
    claims_by_target = defaultdict(list)
    for u, vdi in pool.vdis.items():
        if vdi["snap_of"]:
            claims_by_target[vdi["snap_of"]].append(u)

    for target, children in sorted(claims_by_target.items()):
        target_vdi = pool.vdis.get(target)
        target_phys = target in pool.vhd
        target_anc = set(pool.ancestors(target)[0]) if target_phys else set()
        entry = {
            "target": target,
            "target_label": pool.vdi_label(target),
            "target_in_xapi": target_vdi is not None,
            "target_in_scan": target_phys,
            "target_is_raw_lv": target in pool.raw_lvs,
            "claims": [],
        }
        for child in sorted(children):
            child_vdi = pool.vdis[child]
            claim = {"child": child, "child_label": pool.vdi_label(child)}

            if child == target:
                claim["bucket"] = "DB-GARBAGE"
                claim["evidence"] = "snapshot-of points to itself"
            elif target_vdi and child_vdi["sr"] and target_vdi["sr"] and \
                    child_vdi["sr"] != target_vdi["sr"]:
                claim["bucket"] = "DB-GARBAGE"
                claim["evidence"] = ("claim crosses SR boundary (%s -> %s); "
                                     "VHD chains never cross SRs"
                                     % (child_vdi["sr"], target_vdi["sr"]))
            elif child not in pool.vhd:
                if child in pool.raw_lvs:
                    claim["bucket"] = "DB-GARBAGE"
                    claim["evidence"] = ("child is a raw LV — raw volumes have "
                                         "no VHD chain, snapshot-of is bogus")
                elif child_vdi["sr"] in pool.scanned_srs:
                    claim["bucket"] = "UNVERIFIABLE"
                    claim["evidence"] = ("child VDI not found in vhd-util scan "
                                         "of its SR (no backing VHD?)")
                else:
                    claim["bucket"] = "UNVERIFIABLE"
                    claim["evidence"] = "child's SR was not scanned (type=%s)" % \
                        pool.srs.get(child_vdi["sr"], {}).get("type", "?")
            elif not target_phys:
                if target in pool.raw_lvs:
                    claim["bucket"] = "DB-GARBAGE"
                    claim["evidence"] = "target is a raw LV — cannot be a snapshot source"
                elif target_vdi and target_vdi["sr"] in pool.scanned_srs:
                    claim["bucket"] = "DB-GARBAGE"
                    claim["evidence"] = ("target has no VHD in its SR's scan — "
                                         "the claim points at nothing physical")
                elif target_vdi:
                    claim["bucket"] = "UNVERIFIABLE"
                    claim["evidence"] = "target's SR was not scanned"
                else:
                    claim["bucket"] = "DB-GARBAGE"
                    claim["evidence"] = ("target absent from xapi AND from all "
                                         "VHD scans — ghost UUID")
            else:
                child_anc = set(pool.ancestors(child)[0])
                common = child_anc & target_anc
                if common:
                    claim["bucket"] = "PHYSICAL-RELATED"
                    claim["evidence"] = ("shares physical ancestor(s) with target "
                                         "(normal snapshot/clone layout): %s"
                                         % ", ".join(sorted(common)[:3]))
                else:
                    claim["bucket"] = "DB-GARBAGE"
                    claim["evidence"] = ("child and target live in unrelated "
                                         "physical VHD trees (roots %s vs %s)"
                                         % (pool.physical_root(child),
                                            pool.physical_root(target)))
            entry["claims"].append(claim)

        buckets = set(c["bucket"] for c in entry["claims"])
        if buckets == {"DB-GARBAGE"}:
            entry["verdict"] = "DB-GARBAGE"
        elif "DB-GARBAGE" in buckets or "UNVERIFIABLE" in buckets:
            entry["verdict"] = "MIXED"
        else:
            entry["verdict"] = "PHYSICAL-RELATED"
        entry["is_super_parent"] = len(children) > 1
        results["claim_targets"].append(entry)


def check_physical_anomalies(pool, results):
    """Anomalies visible purely at the VHD layer."""
    # Active VHDs that have children — never normal.
    for u, node in sorted(pool.vhd.items()):
        if not node["hidden"] and pool._children.get(u):
            results["active_parents"].append({
                "vhd": u,
                "label": pool.vdi_label(u),
                "children": sorted(pool._children[u]),
            })

    # Parent-link cycles and dangling physical parents.
    seen_dangling = set()
    for u in sorted(pool.vhd):
        chain, cycle = pool.ancestors(u)
        if cycle:
            results["chain_cycles"].append({"start": u, "chain": chain})
        tail = chain[-1] if chain else u
        if tail not in pool.vhd and tail != u and tail not in seen_dangling:
            # ancestors() appends a parent uuid that has no scan node
            seen_dangling.add(tail)
            results["dangling_phys_parents"].append({
                "missing_parent": tail,
                "first_seen_from": u,
            })


def check_shared_trees(pool, results):
    """Physical trees whose active leaves belong to multiple distinct VMs."""
    roots = sorted(set(pool.physical_root(u) for u in pool.vhd))
    for root in roots:
        members = pool.tree_members(root)
        if len(members) < 2:
            continue
        owners = {}  # vm -> [leaf vdis]
        active_leaves = []
        for m in members:
            node = pool.vhd.get(m)
            if not node or node["hidden"]:
                continue
            active_leaves.append(m)
            for vm in pool.vdi_owner_vms(m):
                owners.setdefault(vm, []).append(m)
        real_owners = [v for v in owners
                       if pool.vms.get(v, {}).get("class") == "REAL-VM"]
        if len(real_owners) > 1:
            branch = pool.vhd.get(root, {})
            results["shared_trees"].append({
                "root": root,
                "root_hidden": branch.get("hidden", False),
                "members": len(members),
                "active_leaves": sorted(active_leaves),
                "owner_vms": {vm: sorted(ls) for vm, ls in owners.items()
                              if vm in real_owners},
                "owner_labels": [pool.vm_label(v) for v in sorted(real_owners)],
                "note": ("hidden shared base — consistent with fast-clone "
                         "provenance; verify the VMs are clones of a common "
                         "source, otherwise treat as tangled"
                         if branch.get("hidden", True) else
                         "shared point is an ACTIVE vhd — strong tangle indicator"),
            })


def check_sm_cache(pool, results):
    """sm-config:vhd-parent vs the physical parent reported by vhd-util."""
    for u, vdi in sorted(pool.vdis.items()):
        if u not in pool.vhd or not vdi["sm_vhd_parent"]:
            continue
        phys = pool.vhd[u]["parent"]
        if vdi["sm_vhd_parent"] != phys:
            results["sm_cache_stale"].append({
                "vdi": u,
                "label": pool.vdi_label(u),
                "sm_config_parent": vdi["sm_vhd_parent"],
                "physical_parent": phys or "(none)",
            })


def check_orphans(pool, results):
    """VHDs without xapi records, and VHD-typed VDIs without backing VHDs."""
    for u, node in sorted(pool.vhd.items()):
        if u not in pool.vdis and not node["hidden"]:
            results["orphan_active_vhds"].append({
                "vhd": u, "sr": node["sr"], "size": node["size"],
                "parent": node["parent"] or "(none)",
            })
    for u, vdi in sorted(pool.vdis.items()):
        if (vdi["sr"] in pool.scanned_srs and vdi["managed"]
                and vdi["sm_vdi_type"] == "vhd"
                and u not in pool.vhd and u not in pool.raw_lvs
                and not vdi["missing"]):
            results["vdis_without_backing"].append({
                "vdi": u, "label": pool.vdi_label(u), "type": vdi["type"],
            })


def analyze(srcdir):
    pool = Pool()
    pool.load(srcdir)
    results = {
        "claim_targets": [],
        "active_parents": [],
        "chain_cycles": [],
        "dangling_phys_parents": [],
        "shared_trees": [],
        "sm_cache_stale": [],
        "orphan_active_vhds": [],
        "vdis_without_backing": [],
    }
    classify_claims(pool, results)
    check_physical_anomalies(pool, results)
    check_shared_trees(pool, results)
    check_sm_cache(pool, results)
    check_orphans(pool, results)
    return pool, results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def overall_verdict(results):
    physical_bad = (results["active_parents"] or results["chain_cycles"]
                    or [t for t in results["shared_trees"]
                        if not t["root_hidden"]])
    garbage = [t for t in results["claim_targets"]
               if t["verdict"] in ("DB-GARBAGE", "MIXED")]
    unverifiable = any(c["bucket"] == "UNVERIFIABLE"
                       for t in results["claim_targets"] for c in t["claims"])
    if physical_bad:
        return ("PHYSICAL-ENTANGLEMENT",
                "VHD-layer anomalies found — data-layer remediation "
                "(storage migration) needed for the flagged trees; do NOT "
                "rely on state.db edits alone.")
    if garbage:
        return ("METADATA-ONLY",
                "All snapshot-of corruption is database-layer; physical VHD "
                "trees are internally consistent. state.db cleanup is "
                "sufficient." + (" Some claims were unverifiable — review "
                                 "those manually." if unverifiable else ""))
    return ("CLEAN", "No bogus snapshot-of claims and no physical anomalies.")


def text_report(pool, results):
    L = []
    verdict, verdict_msg = overall_verdict(results)
    L.append("=" * 76)
    L.append(" VHD chain <-> xapi metadata cross-check")
    L.append(" Source host: %s   collected: %s" %
             (pool.meta.get("hostname", "?"), pool.meta.get("collected_utc", "?")))
    L.append(" Analyzed: %s UTC" % datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    L.append("=" * 76)
    L.append("")
    L.append("## VERDICT: %s" % verdict)
    L.append("   %s" % verdict_msg)
    L.append("")
    L.append("## Inventory")
    L.append("   xapi: %d VDIs, %d VBDs, %d VMs, %d SRs" %
             (len(pool.vdis), len(pool.vbds), len(pool.vms), len(pool.srs)))
    L.append("   physical: %d VHDs across %d scanned SR(s); %d raw LV(s)" %
             (len(pool.vhd), len(pool.scanned_srs), len(pool.raw_lvs)))
    for sr in pool.scanned_srs:
        info = pool.srs.get(sr, {})
        n = sum(1 for v in pool.vhd.values() if v["sr"] == sr)
        L.append("     - %s [%s, %s]: %d VHDs%s" %
                 (sr, info.get("name", "?"), info.get("type", "?"), n,
                  "  (%d scan errors!)" % len(pool.scan_errors[sr])
                  if sr in pool.scan_errors else ""))
    unscanned = [u for u, s in pool.srs.items()
                 if s["type"] in VHD_SR_TYPES and u not in pool.scanned_srs]
    if unscanned:
        L.append("   WARNING: VHD-capable SRs with no scan data: %s"
                 % ", ".join(unscanned))

    # snapshot-of claim classification
    targets = results["claim_targets"]
    supers = [t for t in targets if t["is_super_parent"]]
    n_claims = sum(len(t["claims"]) for t in targets)
    by_bucket = defaultdict(int)
    for t in targets:
        for c in t["claims"]:
            by_bucket[c["bucket"]] += 1
    L.append("")
    L.append("## snapshot-of claim classification "
             "(%d claims, %d targets, %d super-parents)" %
             (n_claims, len(targets), len(supers)))
    for b in ("DB-GARBAGE", "PHYSICAL-RELATED", "UNVERIFIABLE"):
        L.append("   %-17s %d" % (b + ":", by_bucket.get(b, 0)))

    interesting = [t for t in targets
                   if t["verdict"] != "PHYSICAL-RELATED" or t["is_super_parent"]]
    for t in interesting:
        L.append("")
        tag = " [SUPER-PARENT: %d children]" % len(t["claims"]) \
              if t["is_super_parent"] else ""
        L.append("   target %s -> verdict %s%s" %
                 (t["target_label"], t["verdict"], tag))
        if not t["target_in_scan"]:
            why = "raw LV" if t["target_is_raw_lv"] else "absent from VHD scans"
            L.append("     (target physical status: %s%s)" %
                     (why, "" if t["target_in_xapi"] else "; not in xapi either"))
        for c in t["claims"]:
            L.append("     - %-16s %s" % (c["bucket"], c["child_label"]))
            L.append("       %s" % c["evidence"])

    def section(key, title, fmt):
        items = results[key]
        L.append("")
        L.append("## %s (%d)" % (title, len(items)))
        if not items:
            L.append("   none")
        for it in items:
            L.append("   - " + fmt(it))

    section("active_parents",
            "Active (hidden=0) VHDs with children — never normal",
            lambda it: "%s children=[%s]" % (it["label"], ", ".join(it["children"])))
    section("chain_cycles", "VHD parent-link cycles — CRITICAL",
            lambda it: " -> ".join(it["chain"]))
    section("dangling_phys_parents",
            "Physical parent refs missing from scan",
            lambda it: "%s (first seen from %s)" %
                       (it["missing_parent"], it["first_seen_from"]))
    section("shared_trees",
            "Physical trees with active leaves on multiple real VMs",
            lambda it: "root=%s (%s) VMs: %s\n       %s" %
                       (it["root"],
                        "hidden base" if it["root_hidden"] else "ACTIVE root",
                        "; ".join(it["owner_labels"]), it["note"]))
    section("sm_cache_stale",
            "sm-config:vhd-parent disagrees with physical parent (refresh via sr-scan)",
            lambda it: "%s sm-config=%s physical=%s" %
                       (it["label"], it["sm_config_parent"], it["physical_parent"]))
    section("orphan_active_vhds",
            "Active VHDs with no xapi VDI record",
            lambda it: "%s sr=%s size=%d parent=%s" %
                       (it["vhd"], it["sr"], it["size"], it["parent"]))
    section("vdis_without_backing",
            "VHD-typed managed VDIs with no backing VHD/LV found",
            lambda it: "%s type=%s" % (it["label"], it["type"]))

    if pool.scan_errors:
        L.append("")
        L.append("## vhd-util scan errors")
        for sr, errs in pool.scan_errors.items():
            for e in errs[:10]:
                L.append("   [%s] %s" % (sr, e))

    L.append("")
    L.append("=" * 76)
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Cross-check xapi snapshot metadata against physical "
                    "VHD chains (vhd-util).")
    sub = p.add_subparsers(dest="cmd")

    pc = sub.add_parser("collect", help="On the pool master: dump raw xe + "
                                        "vhd-util data to a directory.")
    pc.add_argument("--out", required=True, metavar="DIR")
    pc.add_argument("--verbose", "-v", action="store_true")

    pa = sub.add_parser("analyze", help="Anywhere: analyze a collected directory.")
    pa.add_argument("--from", dest="srcdir", required=True, metavar="DIR")
    pa.add_argument("--json", metavar="FILE", help="also write results as JSON")

    pr = sub.add_parser("run", help="On the pool master: collect + analyze in one go.")
    pr.add_argument("--json", metavar="FILE")
    pr.add_argument("--keep", metavar="DIR",
                    help="keep the collected raw data in DIR")
    pr.add_argument("--verbose", "-v", action="store_true")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        sys.exit(1)

    if args.cmd == "collect":
        collect(args.out, verbose=args.verbose)
        return

    if args.cmd == "run":
        srcdir = args.keep or tempfile.mkdtemp(prefix="vhd-crosscheck-")
        collect(srcdir, verbose=args.verbose)
    else:
        srcdir = args.srcdir
        if not os.path.isdir(srcdir):
            sys.exit("[FATAL] no such directory: %s" % srcdir)

    pool, results = analyze(srcdir)
    print(text_report(pool, results))

    if getattr(args, "json", None):
        verdict, msg = overall_verdict(results)
        with open(args.json, "w") as fh:
            json.dump({"verdict": verdict, "verdict_detail": msg,
                       "meta": pool.meta, "results": results}, fh, indent=2)
        print("[INFO] JSON written to %s" % args.json, file=sys.stderr)

    verdict, _ = overall_verdict(results)
    sys.exit(0 if verdict == "CLEAN" else 2 if verdict == "PHYSICAL-ENTANGLEMENT" else 0)


if __name__ == "__main__":
    main()
