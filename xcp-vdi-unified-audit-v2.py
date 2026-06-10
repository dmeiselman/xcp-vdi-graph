#!/usr/bin/env python3
"""
xcp-vdi-unified-audit.py — Unified VDI/VM/VBD Metadata Integrity Auditor
for XCP-ng / Citrix Hypervisor

Combines and extends detection logic from:
  - snapshot-fixer_ffc40afd.py       (A2-CONTRADICTION, A1-SELF-REF)
  - super-parent-finder_0bd59673.sh  (sm-config:snapshots super-parents)
  - xcp-vdi-graph-audit_e87532c3.sh  (graph-level checks: VHD chains, cross-SR, etc.)
  - vdi-snapshot-metadata-audit-v2_f00702bf.sh (VM-correlated reciprocal checks)

Usage:
  python3 xcp-vdi-unified-audit.py [--json audit.json] [--csv audit.csv] [--verbose]

Requires: xe CLI available on PATH (run on a pool master or host).
"""

import subprocess
import sys
import os
import json
import csv
import io
import argparse
import re
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NULL_REF = "OpaqueRef:NULL"
EMPTY = ""
NOT_IN_DB = "<not-in-database>"

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class Finding:
    """A single audit finding — the universal reporting unit."""

    def __init__(self, check_id="", severity="", object_type="",
                 object_uuid="", object_name="", summary="", detail="",
                 affected_vm_uuid="", affected_vm_name="", related_uuids=None):
        self.check_id = check_id            # e.g. "A1-SELF-REF"
        self.severity = severity            # CRITICAL, HIGH, MEDIUM, LOW, INFO
        self.object_type = object_type      # VDI, VM, VBD, SR
        self.object_uuid = object_uuid      # UUID of the affected object
        self.object_name = object_name      # name-label for readability
        self.summary = summary              # one-line description
        self.detail = detail                # extended description / context
        self.affected_vm_uuid = affected_vm_uuid  # production VM impacted (if any)
        self.affected_vm_name = affected_vm_name
        self.related_uuids = list(related_uuids) if related_uuids else []

    def sort_key(self):
        return (SEVERITY_ORDER.get(self.severity, 99), self.check_id, self.object_uuid)

    def to_dict(self):
        """Flat dict of all fields (replaces dataclasses.asdict)."""
        return {
            "check_id": self.check_id,
            "severity": self.severity,
            "object_type": self.object_type,
            "object_uuid": self.object_uuid,
            "object_name": self.object_name,
            "summary": self.summary,
            "detail": self.detail,
            "affected_vm_uuid": self.affected_vm_uuid,
            "affected_vm_name": self.affected_vm_name,
            "related_uuids": list(self.related_uuids),
        }


class VDIRecord:
    def __init__(self, uuid="", name_label="", name_description="",
                 sr_uuid="", vbd_uuids=None, is_a_snapshot=False,
                 snapshot_of="", snapshot_time="", snapshots=None,
                 vdi_type="", managed=True, missing=False, read_only=False,
                 sm_config=None):
        self.uuid = uuid
        self.name_label = name_label
        self.name_description = name_description
        self.sr_uuid = sr_uuid
        self.vbd_uuids = list(vbd_uuids) if vbd_uuids else []
        self.is_a_snapshot = is_a_snapshot
        self.snapshot_of = snapshot_of        # UUID or empty
        self.snapshot_time = snapshot_time
        self.snapshots = list(snapshots) if snapshots else []  # XAPI snapshots list
        self.vdi_type = vdi_type              # user, system, suspend, crashdump, etc.
        self.managed = managed
        self.missing = missing
        self.read_only = read_only
        self.sm_config = dict(sm_config) if sm_config else {}
        # Derived / enriched fields
        self.sr_type = ""
        self.owner_vm_uuids = []
        self.owner_vm_names = []
        self.all_vm_uuids = []
        self.all_vm_names = []


class VBDRecord:
    def __init__(self, uuid="", vdi_uuid="", vm_uuid="", device="",
                 vbd_type="", currently_attached=False, mode=""):
        self.uuid = uuid
        self.vdi_uuid = vdi_uuid
        self.vm_uuid = vm_uuid
        self.device = device
        self.vbd_type = vbd_type      # Disk, CD
        self.currently_attached = currently_attached
        self.mode = mode


class VMRecord:
    def __init__(self, uuid="", name_label="", is_a_snapshot=False,
                 is_a_template=False, is_control_domain=False, snapshot_of="",
                 snapshot_time="", snapshots=None, power_state="",
                 other_config=None):
        self.uuid = uuid
        self.name_label = name_label
        self.is_a_snapshot = is_a_snapshot
        self.is_a_template = is_a_template
        self.is_control_domain = is_control_domain
        self.snapshot_of = snapshot_of
        self.snapshot_time = snapshot_time
        self.snapshots = list(snapshots) if snapshots else []
        self.power_state = power_state
        self.other_config = dict(other_config) if other_config else {}
        # Derived: REAL-VM, VM-SNAPSHOT, XO-BACKUP, TEMPLATE, CONTROL-DOMAIN
        self.vm_class = ""


class SRRecord:
    def __init__(self, uuid="", name_label="", sr_type=""):
        self.uuid = uuid
        self.name_label = name_label
        self.sr_type = sr_type    # ext, lvm, nfs, smb, iso, udev, etc.


# ---------------------------------------------------------------------------
# xe CLI helper
# ---------------------------------------------------------------------------

def xe_cmd(args: list, timeout: int = 120) -> str:
    """Run an xe command and return stdout. Raises on failure."""
    cmd = ["xe"] + args
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,   # 'text=True' alias is 3.7+; this works on 3.6+
            timeout=timeout,
        )
        if result.returncode != 0:
            # Some xe commands return non-zero for "not found" — tolerate that
            stderr = result.stderr.strip()
            if "does not exist" in stderr or "could not be found" in stderr:
                return ""
            # sm-config key missing is also benign
            if "Key" in stderr and "not found" in stderr:
                return ""
            print(f"[WARN] xe command failed: {' '.join(cmd)}\n  stderr: {stderr}",
                  file=sys.stderr)
            return ""
        return result.stdout
    except subprocess.TimeoutExpired:
        print(f"[WARN] xe command timed out: {' '.join(cmd)}", file=sys.stderr)
        return ""
    except FileNotFoundError:
        print("[FATAL] 'xe' command not found on PATH. Run this on a XCP-ng host.",
              file=sys.stderr)
        sys.exit(1)


def parse_xe_list(output: str) -> List[Dict[str, str]]:
    """
    Parse the output of 'xe <type>-list params=...' into a list of dicts.

    xe outputs records separated by blank lines, with each field on its own
    line as:
        field ( RO/RW): value
    Multi-valued fields (like snapshots, vbd-uuids) use '; ' separation.
    Map fields (like sm-config) use '; ' between entries and ': ' within.
    """
    records = []
    current = {}
    for line in output.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        # Match field lines: "field-name ( RO): value" or "field-name (MRO): value"
        # xe indents every field line except 'uuid' — must allow leading whitespace
        m = re.match(r'^\s*(\S.*?)\s*\(\s*(?:RO|RW|MRO|MRW|SRO|SRW)\s*\)\s*:\s?(.*)', line)
        if m:
            key = m.group(1).strip()
            val = m.group(2).strip()
            current[key] = val
        else:
            # Continuation line — append to last key
            if current:
                last_key = list(current.keys())[-1]
                current[last_key] += " " + line.strip()
    if current:
        records.append(current)
    return records


def split_list_field(val: str) -> list:
    """Split a '; '-delimited xe list field into a list of stripped values,
    filtering out empties."""
    if not val or val in ("<not in database>", "not in database"):
        return []
    return [x.strip() for x in val.split(";") if x.strip()]


def parse_sm_config(val: str) -> dict:
    """Parse an sm-config map from xe output into a dict."""
    result = {}
    if not val:
        return result
    # sm-config is displayed as: key1: val1; key2: val2; ...
    # But values themselves can contain colons (e.g., timestamps), so we
    # split on '; ' first, then split each entry on the first ': '.
    entries = val.split("; ")
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(": ", 1)
        if len(parts) == 2:
            result[parts[0].strip()] = parts[1].strip()
        elif len(parts) == 1:
            result[parts[0].strip()] = ""
    return result


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

class PoolData:
    """Container for all pool metadata loaded from xe CLI."""

    def __init__(self):
        self.vdis: Dict[str, VDIRecord] = {}
        self.vbds: Dict[str, VBDRecord] = {}
        self.vms: Dict[str, VMRecord] = {}
        self.srs: Dict[str, SRRecord] = {}

        # Reverse indexes built after loading
        self.children_by_snap_of: Dict[str, List[str]] = defaultdict(list)
        self.children_by_vhd_parent: Dict[str, List[str]] = defaultdict(list)
        self.vbds_by_vdi: Dict[str, List[str]] = defaultdict(list)
        self.vbds_by_vm: Dict[str, List[str]] = defaultdict(list)

        # All known UUIDs for existence checks
        self.all_vdi_uuids: Set[str] = set()
        self.all_vm_uuids: Set[str] = set()
        self.all_sr_uuids: Set[str] = set()
        self.all_uuids: Set[str] = set()

    def load(self, verbose: bool = False):
        """Load all data from xe CLI."""
        self._load_srs(verbose)
        self._load_vdis(verbose)
        self._load_vbds(verbose)
        self._load_vms(verbose)
        self._build_reverse_indexes(verbose)
        self._enrich_vdi_ownership(verbose)

    def _load_srs(self, verbose):
        if verbose:
            print("[INFO] Loading SR records...", file=sys.stderr)
        output = xe_cmd(["sr-list",
                         "params=uuid,name-label,type"])
        for rec in parse_xe_list(output):
            sr = SRRecord(
                uuid=rec.get("uuid", ""),
                name_label=rec.get("name-label", ""),
                sr_type=rec.get("type", ""),
            )
            if sr.uuid:
                self.srs[sr.uuid] = sr
                self.all_sr_uuids.add(sr.uuid)
                self.all_uuids.add(sr.uuid)
        if verbose:
            print(f"  Loaded {len(self.srs)} SRs", file=sys.stderr)

    def _load_vdis(self, verbose):
        if verbose:
            print("[INFO] Loading VDI records...", file=sys.stderr)
        output = xe_cmd(["vdi-list",
                         "params=uuid,name-label,name-description,sr-uuid,"
                         "vbd-uuids,is-a-snapshot,snapshot-of,snapshot-time,"
                         "snapshots,type,managed,missing,read-only,sm-config"],
                        timeout=300)
        for rec in parse_xe_list(output):
            vdi = VDIRecord(
                uuid=rec.get("uuid", ""),
                name_label=rec.get("name-label", ""),
                name_description=rec.get("name-description", ""),
                sr_uuid=rec.get("sr-uuid", ""),
                vbd_uuids=split_list_field(rec.get("vbd-uuids", "")),
                is_a_snapshot=(rec.get("is-a-snapshot", "false").lower() == "true"),
                snapshot_of=rec.get("snapshot-of", "").strip(),
                snapshot_time=rec.get("snapshot-time", "").strip(),
                snapshots=split_list_field(rec.get("snapshots", "")),
                vdi_type=rec.get("type", ""),
                managed=(rec.get("managed", "true").lower() == "true"),
                missing=(rec.get("missing", "false").lower() == "true"),
                read_only=(rec.get("read-only", "false").lower() == "true"),
                sm_config=parse_sm_config(rec.get("sm-config", "")),
            )
            if vdi.uuid:
                # Resolve SR type
                if vdi.sr_uuid in self.srs:
                    vdi.sr_type = self.srs[vdi.sr_uuid].sr_type
                # Normalize snapshot_of: treat "<not in database>" and empty as empty
                if vdi.snapshot_of in ("<not in database>", "not in database"):
                    vdi.snapshot_of = ""
                self.vdis[vdi.uuid] = vdi
                self.all_vdi_uuids.add(vdi.uuid)
                self.all_uuids.add(vdi.uuid)
        if verbose:
            print(f"  Loaded {len(self.vdis)} VDIs", file=sys.stderr)

    def _load_vbds(self, verbose):
        if verbose:
            print("[INFO] Loading VBD records...", file=sys.stderr)
        output = xe_cmd(["vbd-list",
                         "params=uuid,vdi-uuid,vm-uuid,userdevice,type,"
                         "currently-attached,mode"],
                        timeout=300)
        for rec in parse_xe_list(output):
            vbd = VBDRecord(
                uuid=rec.get("uuid", ""),
                vdi_uuid=rec.get("vdi-uuid", "").strip(),
                vm_uuid=rec.get("vm-uuid", "").strip(),
                device=rec.get("userdevice", ""),
                vbd_type=rec.get("type", ""),
                currently_attached=(
                    rec.get("currently-attached", "false").lower() == "true"),
                mode=rec.get("mode", ""),
            )
            if vbd.uuid:
                if vbd.vdi_uuid in ("<not in database>", "not in database"):
                    vbd.vdi_uuid = ""
                if vbd.vm_uuid in ("<not in database>", "not in database"):
                    vbd.vm_uuid = ""
                self.vbds[vbd.uuid] = vbd
                self.all_uuids.add(vbd.uuid)
        if verbose:
            print(f"  Loaded {len(self.vbds)} VBDs", file=sys.stderr)

    def _load_vms(self, verbose):
        if verbose:
            print("[INFO] Loading VM records...", file=sys.stderr)
        output = xe_cmd(["vm-list",
                         "params=uuid,name-label,is-a-snapshot,is-a-template,"
                         "is-control-domain,snapshot-of,snapshot-time,snapshots,"
                         "power-state,other-config"],
                        timeout=300)
        for rec in parse_xe_list(output):
            vm = VMRecord(
                uuid=rec.get("uuid", ""),
                name_label=rec.get("name-label", ""),
                is_a_snapshot=(
                    rec.get("is-a-snapshot", "false").lower() == "true"),
                is_a_template=(
                    rec.get("is-a-template", "false").lower() == "true"),
                is_control_domain=(
                    rec.get("is-control-domain", "false").lower() == "true"),
                snapshot_of=rec.get("snapshot-of", "").strip(),
                snapshot_time=rec.get("snapshot-time", "").strip(),
                snapshots=split_list_field(rec.get("snapshots", "")),
                power_state=rec.get("power-state", ""),
                other_config=parse_sm_config(rec.get("other-config", "")),
            )
            if vm.uuid:
                if vm.snapshot_of in ("<not in database>", "not in database"):
                    vm.snapshot_of = ""
                # Classify VM
                if vm.is_control_domain:
                    vm.vm_class = "CONTROL-DOMAIN"
                elif vm.is_a_template and not vm.is_a_snapshot:
                    vm.vm_class = "TEMPLATE"
                elif vm.is_a_snapshot:
                    # Check for XO backup
                    if any(k.startswith("xo:backup") for k in vm.other_config):
                        vm.vm_class = "XO-BACKUP"
                    else:
                        vm.vm_class = "VM-SNAPSHOT"
                else:
                    vm.vm_class = "REAL-VM"
                self.vms[vm.uuid] = vm
                self.all_vm_uuids.add(vm.uuid)
                self.all_uuids.add(vm.uuid)
        if verbose:
            print(f"  Loaded {len(self.vms)} VMs", file=sys.stderr)

    def _build_reverse_indexes(self, verbose):
        if verbose:
            print("[INFO] Building reverse indexes...", file=sys.stderr)

        # VBD -> VDI and VBD -> VM reverse maps
        for vbd_uuid, vbd in self.vbds.items():
            if vbd.vdi_uuid:
                self.vbds_by_vdi[vbd.vdi_uuid].append(vbd_uuid)
            if vbd.vm_uuid:
                self.vbds_by_vm[vbd.vm_uuid].append(vbd_uuid)

        # snapshot-of reverse index
        for vdi_uuid, vdi in self.vdis.items():
            if vdi.snapshot_of and vdi.snapshot_of != vdi_uuid:
                self.children_by_snap_of[vdi.snapshot_of].append(vdi_uuid)

        # vhd-parent reverse index
        for vdi_uuid, vdi in self.vdis.items():
            vhd_parent = vdi.sm_config.get("vhd-parent", "")
            if vhd_parent and vhd_parent != vdi_uuid:
                self.children_by_vhd_parent[vhd_parent].append(vdi_uuid)

    def _enrich_vdi_ownership(self, verbose):
        """For each VDI, determine the VMs that own it via VBDs."""
        if verbose:
            print("[INFO] Enriching VDI ownership...", file=sys.stderr)

        for vdi_uuid, vdi in self.vdis.items():
            # Gather all VBDs that reference this VDI (from reverse index)
            vbd_uuids = self.vbds_by_vdi.get(vdi_uuid, [])
            for vbd_uuid in vbd_uuids:
                vbd = self.vbds.get(vbd_uuid)
                if not vbd or not vbd.vm_uuid:
                    continue
                vm = self.vms.get(vbd.vm_uuid)
                if not vm:
                    continue
                vdi.all_vm_uuids.append(vm.uuid)
                vdi.all_vm_names.append(vm.name_label)
                if vm.vm_class == "REAL-VM":
                    vdi.owner_vm_uuids.append(vm.uuid)
                    vdi.owner_vm_names.append(vm.name_label)

    def get_sr_type(self, sr_uuid: str) -> str:
        sr = self.srs.get(sr_uuid)
        return sr.sr_type if sr else ""

    def get_sr_name(self, sr_uuid: str) -> str:
        sr = self.srs.get(sr_uuid)
        return sr.name_label if sr else ""

    def is_iso_sr(self, sr_uuid: str) -> bool:
        sr_type = self.get_sr_type(sr_uuid)
        return sr_type in ("iso", "udev")

    def get_vm_class(self, vm_uuid: str) -> str:
        vm = self.vms.get(vm_uuid)
        return vm.vm_class if vm else ""

    def get_primary_owner_vm(self, vdi: VDIRecord) -> Tuple[str, str]:
        """Return (uuid, name) of the first REAL-VM owner, or first any-VM, or empty."""
        if vdi.owner_vm_uuids:
            return vdi.owner_vm_uuids[0], vdi.owner_vm_names[0]
        if vdi.all_vm_uuids:
            return vdi.all_vm_uuids[0], vdi.all_vm_names[0]
        return "", ""


# ---------------------------------------------------------------------------
# Check functions — each appends findings to a shared list
# ---------------------------------------------------------------------------

def check_a1_self_ref(pool: PoolData, findings: List[Finding]):
    """A1-SELF-REF: VDI snapshot-of points to itself."""
    for uuid, vdi in pool.vdis.items():
        if vdi.snapshot_of == uuid:
            vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
            findings.append(Finding(
                check_id="A1-SELF-REF",
                severity="CRITICAL",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary=f"VDI snapshot-of points to itself",
                detail=(f"snapshot-of={uuid}, is-a-snapshot={vdi.is_a_snapshot}, "
                        f"type={vdi.vdi_type}, SR={vdi.sr_uuid}"),
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[],
            ))


def check_a2_contradiction(pool: PoolData, findings: List[Finding]):
    """A2-CONTRADICTION: Contradictory is-a-snapshot / snapshot-of / snapshot-time."""
    for uuid, vdi in pool.vdis.items():
        vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
        has_snap_time = (vdi.snapshot_time and
                         vdi.snapshot_time not in ("19700101T00:00:00Z",
                                                    "19700101T00:00:00",
                                                    ""))

        # Case 1: not a snapshot but snapshot-of is set (and not self — that is A1)
        if not vdi.is_a_snapshot and vdi.snapshot_of and vdi.snapshot_of != uuid:
            findings.append(Finding(
                check_id="A2-CONTRADICTION",
                severity="CRITICAL",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="Non-snapshot VDI has snapshot-of set",
                detail=(f"is-a-snapshot=false but snapshot-of={vdi.snapshot_of}"),
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[vdi.snapshot_of],
            ))

        # Case 2: not a snapshot but has snapshot-time
        if not vdi.is_a_snapshot and has_snap_time:
            findings.append(Finding(
                check_id="A2-CONTRADICTION",
                severity="MEDIUM",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="Non-snapshot VDI has snapshot-time set",
                detail=(f"is-a-snapshot=false but snapshot-time={vdi.snapshot_time}"),
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[],
            ))

        # Case 3: is a snapshot but no snapshot-time
        if vdi.is_a_snapshot and not has_snap_time:
            findings.append(Finding(
                check_id="A2-CONTRADICTION",
                severity="MEDIUM",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="Snapshot VDI has no snapshot-time",
                detail=(f"is-a-snapshot=true but snapshot-time is empty/epoch"),
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[],
            ))


def check_a3_circular_chain(pool: PoolData, findings: List[Finding]):
    """A3-CIRCULAR-CHAIN: Circular snapshot-of chains of depth > 1."""
    # For each VDI that has a snapshot-of, walk the chain up to a max depth.
    # Self-refs are handled by A1; here we look for longer cycles.
    MAX_HOPS = 50
    for uuid, vdi in pool.vdis.items():
        if not vdi.snapshot_of or vdi.snapshot_of == uuid:
            continue
        visited = [uuid]
        current = vdi.snapshot_of
        hop = 0
        while current and hop < MAX_HOPS:
            if current == uuid:
                # Found a cycle back to the starting VDI
                vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
                findings.append(Finding(
                    check_id="A3-CIRCULAR-CHAIN",
                    severity="CRITICAL",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary=f"Circular snapshot-of chain of depth {len(visited)}",
                    detail=f"Chain: {' -> '.join(visited)} -> {uuid}",
                    affected_vm_uuid=vm_uuid,
                    affected_vm_name=vm_name,
                    related_uuids=visited[1:],
                ))
                break
            if current in visited:
                # Cycle that doesn't include the starting VDI — still a cycle,
                # but will be reported when we start from a node in the cycle.
                break
            visited.append(current)
            next_vdi = pool.vdis.get(current)
            if not next_vdi:
                break  # chain ends at a missing VDI (handled by B1)
            current = next_vdi.snapshot_of
            hop += 1


def check_b1_orphaned_and_missing_parent(pool: PoolData, findings: List[Finding]):
    """B1: Orphaned snapshots, missing parents, broken reciprocal links."""
    for uuid, vdi in pool.vdis.items():
        vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)

        # B1-ORPHANED-SNAPSHOT: is-a-snapshot=true but no snapshot-of
        if vdi.is_a_snapshot and not vdi.snapshot_of:
            findings.append(Finding(
                check_id="B1-ORPHANED-SNAPSHOT",
                severity="HIGH",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="Snapshot VDI has no snapshot-of reference",
                detail=(f"is-a-snapshot=true, snapshot-of is empty, "
                        f"type={vdi.vdi_type}, SR={vdi.sr_uuid}"),
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[],
            ))

        # B1-MISSING-PARENT: snapshot-of points to a non-existent VDI
        if vdi.snapshot_of and vdi.snapshot_of != uuid:
            if vdi.snapshot_of not in pool.all_vdi_uuids:
                # Determine if the UUID belongs to another object type
                target_type = ""
                if vdi.snapshot_of in pool.all_sr_uuids:
                    target_type = " (this UUID is an SR!)"
                elif vdi.snapshot_of in pool.all_vm_uuids:
                    target_type = " (this UUID is a VM!)"

                findings.append(Finding(
                    check_id="B1-MISSING-PARENT",
                    severity="HIGH",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary=f"snapshot-of points to non-existent VDI{target_type}",
                    detail=(f"snapshot-of={vdi.snapshot_of}, "
                            f"is-a-snapshot={vdi.is_a_snapshot}"),
                    affected_vm_uuid=vm_uuid,
                    affected_vm_name=vm_name,
                    related_uuids=[vdi.snapshot_of],
                ))

        # B1-BROKEN-RECIPROCAL: child points to parent, but parent does not
        # list child in its snapshots field
        if (vdi.snapshot_of and vdi.snapshot_of != uuid and
                vdi.snapshot_of in pool.all_vdi_uuids):
            parent = pool.vdis[vdi.snapshot_of]
            if uuid not in parent.snapshots:
                findings.append(Finding(
                    check_id="B1-BROKEN-RECIPROCAL",
                    severity="MEDIUM",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary="Child not listed in parent's snapshots field",
                    detail=(f"snapshot-of={vdi.snapshot_of} "
                            f"(parent name: {parent.name_label}), "
                            f"but parent's snapshots list does not contain {uuid}"),
                    affected_vm_uuid=vm_uuid,
                    affected_vm_name=vm_name,
                    related_uuids=[vdi.snapshot_of],
                ))


def check_b2_snapshots_field(pool: PoolData, findings: List[Finding]):
    """B2: Problems in the parent VDI's 'snapshots' list field."""
    for uuid, vdi in pool.vdis.items():
        if not vdi.snapshots:
            continue
        vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)

        for child_uuid in vdi.snapshots:
            child = pool.vdis.get(child_uuid)

            # B2-GHOST-REF: listed child doesn't exist
            if not child:
                findings.append(Finding(
                    check_id="B2-GHOST-REF",
                    severity="MEDIUM",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary=f"Snapshots field references non-existent VDI {child_uuid}",
                    detail=f"Parent {uuid} lists {child_uuid} in snapshots but it does not exist",
                    affected_vm_uuid=vm_uuid,
                    affected_vm_name=vm_name,
                    related_uuids=[child_uuid],
                ))
                continue

            # B2-BROKEN-BACKLINK: child exists but has no snapshot-of set
            if not child.snapshot_of:
                findings.append(Finding(
                    check_id="B2-BROKEN-BACKLINK",
                    severity="HIGH",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary=f"Child VDI {child_uuid} has no snapshot-of back-link",
                    detail=(f"Parent {uuid} lists {child_uuid} in snapshots, "
                            f"but child's snapshot-of is empty"),
                    affected_vm_uuid=vm_uuid,
                    affected_vm_name=vm_name,
                    related_uuids=[child_uuid],
                ))

            # B2-CROSS-VDI: child's snapshot-of points to a DIFFERENT parent
            elif child.snapshot_of != uuid and child.snapshot_of != child_uuid:
                findings.append(Finding(
                    check_id="B2-CROSS-VDI",
                    severity="CRITICAL",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary=f"Cross-VDI contamination: child {child_uuid} points elsewhere",
                    detail=(f"Parent {uuid} lists {child_uuid} in snapshots, "
                            f"but child's snapshot-of={child.snapshot_of}"),
                    affected_vm_uuid=vm_uuid,
                    affected_vm_name=vm_name,
                    related_uuids=[child_uuid, child.snapshot_of],
                ))


def check_b3_duplicate_parent_claims(pool: PoolData, findings: List[Finding]):
    """B3-DUPLICATE-CLAIM: A VDI appears in the snapshots list of multiple parents."""
    claimed_by: Dict[str, List[str]] = defaultdict(list)
    for uuid, vdi in pool.vdis.items():
        for child_uuid in vdi.snapshots:
            claimed_by[child_uuid].append(uuid)

    for child_uuid, parents in claimed_by.items():
        if len(parents) > 1:
            child = pool.vdis.get(child_uuid)
            child_name = child.name_label if child else "<missing>"
            # Determine affected VM from the child
            if child:
                vm_uuid, vm_name = pool.get_primary_owner_vm(child)
            else:
                vm_uuid, vm_name = "", ""

            findings.append(Finding(
                check_id="B3-DUPLICATE-CLAIM",
                severity="CRITICAL",
                object_type="VDI",
                object_uuid=child_uuid,
                object_name=child_name,
                summary=f"VDI claimed by {len(parents)} parents in snapshots fields",
                detail=f"Parents: {', '.join(parents)}",
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=parents,
            ))


def check_b4_snapshot_on_live_vm(pool: PoolData, findings: List[Finding]):
    """B4: Snapshot-flagged VDI attached to a live (non-snapshot) VM."""
    for uuid, vdi in pool.vdis.items():
        if not vdi.is_a_snapshot:
            continue

        # Get all VMs this VDI is attached to via VBDs
        vbd_uuids = pool.vbds_by_vdi.get(uuid, [])
        for vbd_uuid in vbd_uuids:
            vbd = pool.vbds.get(vbd_uuid)
            if not vbd or not vbd.vm_uuid:
                continue
            vm = pool.vms.get(vbd.vm_uuid)
            if not vm:
                continue

            if vm.vm_class == "REAL-VM":
                findings.append(Finding(
                    check_id="B4a-REAL-VM-DISK-AS-SNAP",
                    severity="CRITICAL",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary=f"Snapshot VDI attached to production VM '{vm.name_label}'",
                    detail=(f"is-a-snapshot=true, attached to REAL-VM {vm.uuid} "
                            f"via VBD {vbd_uuid}. This disk is HIDDEN in XOA/XenCenter."),
                    affected_vm_uuid=vm.uuid,
                    affected_vm_name=vm.name_label,
                    related_uuids=[vbd_uuid, vm.uuid],
                ))

            elif vm.vm_class == "VM-SNAPSHOT":
                # Check if the snapshot chain is corrupt
                if vdi.snapshot_of == uuid:
                    findings.append(Finding(
                        check_id="B4b-VMSNAPSHOT-CORRUPT-CHAIN",
                        severity="HIGH",
                        object_type="VDI",
                        object_uuid=uuid,
                        object_name=vdi.name_label,
                        summary=f"VM-snapshot disk with self-referential chain",
                        detail=(f"Attached to VM-SNAPSHOT {vm.uuid} "
                                f"('{vm.name_label}'), snapshot-of=self"),
                        affected_vm_uuid=vm.uuid,
                        affected_vm_name=vm.name_label,
                        related_uuids=[vm.uuid],
                    ))

            elif vm.vm_class == "XO-BACKUP":
                # Expected — XO backup VMs contain snapshot VDIs
                findings.append(Finding(
                    check_id="B4c-XO-BACKUP",
                    severity="INFO",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary=f"Snapshot VDI on XO-backup VM (expected)",
                    detail=f"Attached to XO-BACKUP VM {vm.uuid} ('{vm.name_label}')",
                    affected_vm_uuid=vm.uuid,
                    affected_vm_name=vm.name_label,
                    related_uuids=[vm.uuid],
                ))


def check_c1_vhd_parent_self_ref(pool: PoolData, findings: List[Finding]):
    """C1-SELF-VHD-PARENT: VDI's sm-config vhd-parent points to itself."""
    for uuid, vdi in pool.vdis.items():
        vhd_parent = vdi.sm_config.get("vhd-parent", "")
        if vhd_parent and vhd_parent == uuid:
            vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
            findings.append(Finding(
                check_id="C1-SELF-VHD-PARENT",
                severity="HIGH",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="VDI vhd-parent points to itself",
                detail=f"sm-config:vhd-parent={uuid}. Coalesce/GC will infinite-loop.",
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[],
            ))


def check_c2_vhd_parent_dangling(pool: PoolData, findings: List[Finding]):
    """C2-VHD-PARENT-DANGLING: vhd-parent UUID not in XAPI."""
    for uuid, vdi in pool.vdis.items():
        vhd_parent = vdi.sm_config.get("vhd-parent", "")
        if vhd_parent and vhd_parent != uuid and vhd_parent not in pool.all_vdi_uuids:
            vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
            findings.append(Finding(
                check_id="C2-VHD-PARENT-DANGLING",
                severity="MEDIUM",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary=f"vhd-parent references non-existent VDI",
                detail=f"sm-config:vhd-parent={vhd_parent}",
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[vhd_parent],
            ))


def check_c3_vhd_chain_crossover(pool: PoolData, findings: List[Finding]):
    """C3-CHAIN-CROSSOVER: A single VHD parent has children from unrelated VMs."""
    for parent_uuid, child_uuids in pool.children_by_vhd_parent.items():
        if len(child_uuids) < 2:
            continue
        # Collect the set of owner VMs for all children
        owner_vms = set()
        for child_uuid in child_uuids:
            child = pool.vdis.get(child_uuid)
            if child:
                for vm_u in child.owner_vm_uuids:
                    owner_vms.add(vm_u)
                # Also include VMs from the child's VBDs even if not "owner"
                for vm_u in child.all_vm_uuids:
                    owner_vms.add(vm_u)
        # Filter out control domains and templates
        real_owner_vms = set()
        for vm_u in owner_vms:
            vm = pool.vms.get(vm_u)
            if vm and vm.vm_class == "REAL-VM":
                real_owner_vms.add(vm_u)

        if len(real_owner_vms) > 1:
            parent_vdi = pool.vdis.get(parent_uuid)
            parent_name = parent_vdi.name_label if parent_vdi else "<missing>"
            vm_names = []
            for vm_u in real_owner_vms:
                vm = pool.vms.get(vm_u)
                vm_names.append(f"{vm_u} ({vm.name_label})" if vm else vm_u)

            findings.append(Finding(
                check_id="C3-CHAIN-CROSSOVER",
                severity="HIGH",
                object_type="VDI",
                object_uuid=parent_uuid,
                object_name=parent_name,
                summary=f"VHD parent shared by children from {len(real_owner_vms)} unrelated VMs",
                detail=(f"Children: {', '.join(child_uuids)}. "
                        f"Owner VMs: {'; '.join(vm_names)}"),
                affected_vm_uuid=list(real_owner_vms)[0] if real_owner_vms else "",
                affected_vm_name="",
                related_uuids=child_uuids + list(real_owner_vms),
            ))


def check_c4_cross_sr(pool: PoolData, findings: List[Finding]):
    """C4: Cross-SR references in snapshot-of or vhd-parent."""
    for uuid, vdi in pool.vdis.items():
        vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)

        # Cross-SR snapshot-of
        if vdi.snapshot_of and vdi.snapshot_of in pool.all_vdi_uuids:
            parent = pool.vdis[vdi.snapshot_of]
            if parent.sr_uuid and vdi.sr_uuid and parent.sr_uuid != vdi.sr_uuid:
                findings.append(Finding(
                    check_id="C4-SNAP-OF-CROSS-SR",
                    severity="HIGH",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary="snapshot-of crosses SR boundary",
                    detail=(f"VDI in SR {vdi.sr_uuid} ({pool.get_sr_name(vdi.sr_uuid)}), "
                            f"snapshot-of in SR {parent.sr_uuid} "
                            f"({pool.get_sr_name(parent.sr_uuid)})"),
                    affected_vm_uuid=vm_uuid,
                    affected_vm_name=vm_name,
                    related_uuids=[vdi.snapshot_of, vdi.sr_uuid, parent.sr_uuid],
                ))

        # Cross-SR vhd-parent
        vhd_parent = vdi.sm_config.get("vhd-parent", "")
        if vhd_parent and vhd_parent in pool.all_vdi_uuids:
            parent = pool.vdis[vhd_parent]
            if parent.sr_uuid and vdi.sr_uuid and parent.sr_uuid != vdi.sr_uuid:
                findings.append(Finding(
                    check_id="C4-VHD-PARENT-CROSS-SR",
                    severity="HIGH",
                    object_type="VDI",
                    object_uuid=uuid,
                    object_name=vdi.name_label,
                    summary="vhd-parent crosses SR boundary",
                    detail=(f"VDI in SR {vdi.sr_uuid} ({pool.get_sr_name(vdi.sr_uuid)}), "
                            f"vhd-parent in SR {parent.sr_uuid} "
                            f"({pool.get_sr_name(parent.sr_uuid)})"),
                    affected_vm_uuid=vm_uuid,
                    affected_vm_name=vm_name,
                    related_uuids=[vhd_parent, vdi.sr_uuid, parent.sr_uuid],
                ))


def check_c5_corrupt_snapshot_root(pool: PoolData, findings: List[Finding]):
    """C5-CORRUPT-SNAPSHOT-ROOT: A VDI is snapshot-of target for children spanning
    unrelated VMs or mixed SR types."""
    for target_uuid, child_uuids in pool.children_by_snap_of.items():
        if len(child_uuids) < 2:
            continue

        target_vdi = pool.vdis.get(target_uuid)
        target_sr_type = ""
        if target_vdi:
            target_sr_type = target_vdi.sr_type

        # Collect the owner VMs of all children
        child_owner_vms = set()
        child_sr_types = set()
        for child_uuid in child_uuids:
            child = pool.vdis.get(child_uuid)
            if not child:
                continue
            child_sr_types.add(child.sr_type)
            for vm_u in child.all_vm_uuids:
                vm = pool.vms.get(vm_u)
                if vm and vm.vm_class in ("REAL-VM", "VM-SNAPSHOT"):
                    child_owner_vms.add(vm_u)

        # Also add the target's own VMs
        if target_vdi:
            for vm_u in target_vdi.all_vm_uuids:
                vm = pool.vms.get(vm_u)
                if vm and vm.vm_class in ("REAL-VM", "VM-SNAPSHOT"):
                    child_owner_vms.add(vm_u)

        # Filter to REAL-VMs only for "unrelated" check
        real_vms = set()
        for vm_u in child_owner_vms:
            vm = pool.vms.get(vm_u)
            if vm and vm.vm_class == "REAL-VM":
                real_vms.add(vm_u)

        # Mixed ISO/user SR types
        mixed_sr = (len(child_sr_types) > 1 and
                    any(t in ("iso", "udev") for t in child_sr_types))

        if len(real_vms) > 1 or mixed_sr:
            target_name = target_vdi.name_label if target_vdi else "<missing>"
            vm_detail = []
            for vm_u in real_vms:
                vm = pool.vms.get(vm_u)
                vm_detail.append(f"{vm_u} ({vm.name_label})" if vm else vm_u)
            findings.append(Finding(
                check_id="C5-CORRUPT-SNAPSHOT-ROOT",
                severity="HIGH",
                object_type="VDI",
                object_uuid=target_uuid,
                object_name=target_name,
                summary=(f"Snapshot root with {len(child_uuids)} children "
                         f"across {len(real_vms)} VMs"),
                detail=(f"Children: {', '.join(child_uuids[:10])}"
                        f"{'...' if len(child_uuids) > 10 else ''}. "
                        f"VMs: {'; '.join(vm_detail[:10])}. "
                        f"SR types: {', '.join(child_sr_types)}"),
                affected_vm_uuid=list(real_vms)[0] if real_vms else "",
                affected_vm_name="",
                related_uuids=child_uuids[:20] + list(real_vms),
            ))


def check_c6_iso_flagged_snapshot(pool: PoolData, findings: List[Finding]):
    """C6-ISO-FLAGGED-SNAPSHOT: VDI in an ISO/udev SR flagged as snapshot."""
    for uuid, vdi in pool.vdis.items():
        if vdi.is_a_snapshot and pool.is_iso_sr(vdi.sr_uuid):
            vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
            findings.append(Finding(
                check_id="C6-ISO-FLAGGED-SNAPSHOT",
                severity="HIGH",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="ISO/udev VDI flagged as snapshot",
                detail=(f"SR type={vdi.sr_type}, SR={vdi.sr_uuid} "
                        f"({pool.get_sr_name(vdi.sr_uuid)})"),
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[vdi.sr_uuid],
            ))


def check_c7_snap_of_a_snap(pool: PoolData, findings: List[Finding]):
    """C7-SNAP-OF-SNAP: snapshot-of points to another snapshot VDI."""
    for uuid, vdi in pool.vdis.items():
        if not vdi.snapshot_of or vdi.snapshot_of == uuid:
            continue
        parent = pool.vdis.get(vdi.snapshot_of)
        if parent and parent.is_a_snapshot:
            vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
            findings.append(Finding(
                check_id="C7-SNAP-OF-SNAP",
                severity="MEDIUM",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="snapshot-of points to another snapshot",
                detail=(f"snapshot-of={vdi.snapshot_of} which is itself "
                        f"is-a-snapshot=true"),
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[vdi.snapshot_of],
            ))


def check_d1_super_parent_sm_config(pool: PoolData, findings: List[Finding]):
    """D1-SUPER-PARENT: VDI with sm-config:snapshots pointing to other VDIs.
    Mirrors the super-parent-finder script."""
    for uuid, vdi in pool.vdis.items():
        sm_snapshots = vdi.sm_config.get("snapshots", "")
        if not sm_snapshots:
            continue
        # sm-config:snapshots is typically a comma or space-separated list of UUIDs
        snap_uuids = re.findall(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            sm_snapshots
        )
        if not snap_uuids:
            continue

        vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)

        # Check if any of the listed snapshot UUIDs belong to unrelated VMs
        unrelated_snaps = []
        for snap_uuid in snap_uuids:
            snap_vdi = pool.vdis.get(snap_uuid)
            if snap_vdi:
                snap_vms = set(snap_vdi.all_vm_uuids)
                own_vms = set(vdi.all_vm_uuids)
                if snap_vms and own_vms and not snap_vms.intersection(own_vms):
                    unrelated_snaps.append(snap_uuid)

        if unrelated_snaps:
            severity = "HIGH"
            summary = (f"Super-parent with {len(unrelated_snaps)} unrelated "
                       f"snapshot(s) in sm-config")
        else:
            severity = "INFO"
            summary = (f"Super-parent with {len(snap_uuids)} snapshot(s) in "
                       f"sm-config (all appear related)")

        findings.append(Finding(
            check_id="D1-SUPER-PARENT",
            severity=severity,
            object_type="VDI",
            object_uuid=uuid,
            object_name=vdi.name_label,
            summary=summary,
            detail=(f"sm-config:snapshots contains: {', '.join(snap_uuids[:10])}"
                    f"{'...' if len(snap_uuids) > 10 else ''}"
                    f"{'. Unrelated: ' + ', '.join(unrelated_snaps) if unrelated_snaps else ''}"),
            affected_vm_uuid=vm_uuid,
            affected_vm_name=vm_name,
            related_uuids=snap_uuids,
        ))


def check_e1_vbd_dangling(pool: PoolData, findings: List[Finding]):
    """E1-VBD-DANGLING: VBD references a VDI UUID that doesn't exist."""
    for uuid, vbd in pool.vbds.items():
        if vbd.vdi_uuid and vbd.vdi_uuid not in pool.all_vdi_uuids:
            vm = pool.vms.get(vbd.vm_uuid)
            vm_name = vm.name_label if vm else ""
            findings.append(Finding(
                check_id="E1-VBD-DANGLING",
                severity="MEDIUM",
                object_type="VBD",
                object_uuid=uuid,
                object_name=f"VBD on device {vbd.device}",
                summary=f"VBD references non-existent VDI {vbd.vdi_uuid}",
                detail=(f"VBD {uuid} on VM {vbd.vm_uuid} "
                        f"('{vm_name}') references missing VDI"),
                affected_vm_uuid=vbd.vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[vbd.vdi_uuid, vbd.vm_uuid],
            ))


def check_e2_vbd_backlink_mismatch(pool: PoolData, findings: List[Finding]):
    """E2-VBD-BACKLINK-MISMATCH: VDI's declared vbd-uuids disagrees with VBDs
    that actually reference it."""
    for uuid, vdi in pool.vdis.items():
        declared = set(vdi.vbd_uuids)
        actual = set(pool.vbds_by_vdi.get(uuid, []))

        if declared != actual:
            # Only report if there's a meaningful difference
            missing_from_declared = actual - declared
            extra_in_declared = declared - actual

            if not missing_from_declared and not extra_in_declared:
                continue

            vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
            parts = []
            if missing_from_declared:
                parts.append(f"VBDs referencing VDI but not in vbd-uuids: "
                             f"{', '.join(missing_from_declared)}")
            if extra_in_declared:
                parts.append(f"vbd-uuids listing non-referencing VBDs: "
                             f"{', '.join(extra_in_declared)}")

            findings.append(Finding(
                check_id="E2-VBD-BACKLINK-MISMATCH",
                severity="LOW",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="VBD back-link mismatch",
                detail="; ".join(parts),
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=list(missing_from_declared | extra_in_declared),
            ))


def check_f1_vm_snapshot_issues(pool: PoolData, findings: List[Finding]):
    """F1: VM snapshot metadata issues."""
    for uuid, vm in pool.vms.items():
        if not vm.is_a_snapshot:
            continue

        # F1-VM-SNAP-NO-SOURCE: VM snapshot with empty snapshot-of
        if not vm.snapshot_of:
            findings.append(Finding(
                check_id="F1-VM-SNAP-NO-SOURCE",
                severity="MEDIUM",
                object_type="VM",
                object_uuid=uuid,
                object_name=vm.name_label,
                summary="VM snapshot has no snapshot-of reference",
                detail=f"VM class={vm.vm_class}, snapshot-of is empty",
                affected_vm_uuid=uuid,
                affected_vm_name=vm.name_label,
                related_uuids=[],
            ))

        # F1-VM-SNAP-SOURCE-MISSING: source VM doesn't exist
        elif vm.snapshot_of not in pool.all_vm_uuids:
            findings.append(Finding(
                check_id="F1-VM-SNAP-SOURCE-MISSING",
                severity="MEDIUM",
                object_type="VM",
                object_uuid=uuid,
                object_name=vm.name_label,
                summary=f"VM snapshot source VM {vm.snapshot_of} not found",
                detail=(f"VM class={vm.vm_class}, snapshot-of={vm.snapshot_of} "
                        f"does not exist in pool"),
                affected_vm_uuid=uuid,
                affected_vm_name=vm.name_label,
                related_uuids=[vm.snapshot_of],
            ))


def check_f2_vm_contradiction(pool: PoolData, findings: List[Finding]):
    """F2: VM-level contradictions (mirrors A2 but for VM records)."""
    for uuid, vm in pool.vms.items():
        # Non-snapshot VM with snapshot-of set
        if (not vm.is_a_snapshot and not vm.is_a_template and
                not vm.is_control_domain and vm.snapshot_of):
            findings.append(Finding(
                check_id="F2-VM-CONTRADICTION",
                severity="CRITICAL",
                object_type="VM",
                object_uuid=uuid,
                object_name=vm.name_label,
                summary="Non-snapshot VM has snapshot-of set",
                detail=(f"is-a-snapshot=false, is-a-template=false, "
                        f"snapshot-of={vm.snapshot_of}"),
                affected_vm_uuid=uuid,
                affected_vm_name=vm.name_label,
                related_uuids=[vm.snapshot_of],
            ))


def check_g1_vdi_missing(pool: PoolData, findings: List[Finding]):
    """G1-VDI-MISSING: VDI flagged missing=true."""
    for uuid, vdi in pool.vdis.items():
        if vdi.missing:
            vm_uuid, vm_name = pool.get_primary_owner_vm(vdi)
            findings.append(Finding(
                check_id="G1-VDI-MISSING",
                severity="MEDIUM",
                object_type="VDI",
                object_uuid=uuid,
                object_name=vdi.name_label,
                summary="VDI flagged as missing (backing store gone)",
                detail=f"managed={vdi.managed}, type={vdi.vdi_type}, SR={vdi.sr_uuid}",
                affected_vm_uuid=vm_uuid,
                affected_vm_name=vm_name,
                related_uuids=[vdi.sr_uuid],
            ))


# ---------------------------------------------------------------------------
# Ghost UUID clustering (from graph-audit script)
# ---------------------------------------------------------------------------

def ghost_uuid_analysis(pool: PoolData, findings: List[Finding]) -> str:
    """Analyze ghost UUIDs — snapshot-of targets that are not VDIs.
    Returns a text report section."""
    ghost_targets: Dict[str, List[str]] = defaultdict(list)
    for uuid, vdi in pool.vdis.items():
        if (vdi.snapshot_of and
                vdi.snapshot_of != uuid and
                vdi.snapshot_of not in pool.all_vdi_uuids):
            ghost_targets[vdi.snapshot_of].append(uuid)

    if not ghost_targets:
        return ""

    lines = ["\n## Ghost UUID Cluster Analysis\n"]
    lines.append(f"Found {len(ghost_targets)} ghost target UUID(s) referenced "
                 f"by {sum(len(v) for v in ghost_targets.values())} VDI(s).\n")

    for ghost, referrers in sorted(ghost_targets.items(),
                                    key=lambda x: -len(x[1])):
        lines.append(f"### Ghost UUID: {ghost}")
        # What is this UUID?
        if ghost in pool.all_sr_uuids:
            sr = pool.srs[ghost]
            lines.append(f"  → This is an SR: '{sr.name_label}' (type={sr.sr_type})")
        elif ghost in pool.all_vm_uuids:
            vm = pool.vms[ghost]
            lines.append(f"  → This is a VM: '{vm.name_label}' (class={vm.vm_class})")
        else:
            lines.append(f"  → UUID not found in any XAPI table")

        lines.append(f"  Referencing VDIs ({len(referrers)}):")
        for ref_uuid in referrers[:20]:
            vdi = pool.vdis.get(ref_uuid)
            name = vdi.name_label if vdi else "?"
            lines.append(f"    - {ref_uuid} ({name})")
        if len(referrers) > 20:
            lines.append(f"    ... and {len(referrers) - 20} more")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_text_report(findings: List[Finding], pool: PoolData,
                         elapsed: float) -> str:
    """Generate a human-readable text report."""
    lines = []
    lines.append("=" * 78)
    lines.append("  XCP-ng / Citrix Hypervisor — Unified VDI Metadata Audit Report")
    lines.append(f"  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    lines.append(f"  Scan duration: {elapsed:.1f}s")
    lines.append("=" * 78)

    # Pool summary
    lines.append(f"\n## Pool Inventory\n")
    lines.append(f"  SRs:  {len(pool.srs):>6}")
    lines.append(f"  VDIs: {len(pool.vdis):>6}")
    lines.append(f"  VBDs: {len(pool.vbds):>6}")
    lines.append(f"  VMs:  {len(pool.vms):>6}")

    vm_classes = defaultdict(int)
    for vm in pool.vms.values():
        vm_classes[vm.vm_class] += 1
    for cls in ("REAL-VM", "VM-SNAPSHOT", "XO-BACKUP", "TEMPLATE", "CONTROL-DOMAIN"):
        if cls in vm_classes:
            lines.append(f"    {cls}: {vm_classes[cls]}")

    vdi_types = defaultdict(int)
    for vdi in pool.vdis.values():
        vdi_types[vdi.vdi_type] += 1
    lines.append(f"\n  VDI types:")
    for t, c in sorted(vdi_types.items(), key=lambda x: -x[1]):
        lines.append(f"    {t}: {c}")

    snap_count = sum(1 for v in pool.vdis.values() if v.is_a_snapshot)
    lines.append(f"\n  VDIs flagged is-a-snapshot=true: {snap_count}")

    # Filter out INFO-level for counting
    actionable = [f for f in findings if f.severity != "INFO"]
    info_findings = [f for f in findings if f.severity == "INFO"]

    # Severity summary
    lines.append(f"\n## Findings Summary\n")
    sev_counts = defaultdict(int)
    for f in findings:
        sev_counts[f.severity] += 1
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        if sev in sev_counts:
            marker = " <<<" if sev in ("CRITICAL", "HIGH") and sev_counts[sev] > 0 else ""
            lines.append(f"  {sev:>8}: {sev_counts[sev]:>5}{marker}")
    lines.append(f"  {'TOTAL':>8}: {len(findings):>5}")

    if not actionable:
        lines.append("\n  ✅ No actionable issues found. Pool metadata looks healthy.\n")
        return "\n".join(lines)

    # Check-level summary
    lines.append(f"\n## Findings by Check\n")
    check_counts = defaultdict(lambda: defaultdict(int))
    for f in findings:
        check_counts[f.check_id][f.severity] += 1
    for check_id in sorted(check_counts.keys()):
        total = sum(check_counts[check_id].values())
        sevs = ", ".join(f"{s}:{c}" for s, c in
                         sorted(check_counts[check_id].items(),
                                key=lambda x: SEVERITY_ORDER.get(x[0], 99)))
        lines.append(f"  {check_id:<30} {total:>4}  ({sevs})")

    # Affected production VMs
    affected_vms = {}
    for f in actionable:
        if f.affected_vm_uuid and f.affected_vm_uuid not in affected_vms:
            vm = pool.vms.get(f.affected_vm_uuid)
            if vm and vm.vm_class == "REAL-VM":
                affected_vms[f.affected_vm_uuid] = f.affected_vm_name

    if affected_vms:
        lines.append(f"\n## Affected Production VMs ({len(affected_vms)})\n")
        for vm_uuid, vm_name in sorted(affected_vms.items(), key=lambda x: x[1]):
            # Count findings for this VM
            vm_findings = [f for f in actionable if f.affected_vm_uuid == vm_uuid]
            sev_summary = defaultdict(int)
            for f in vm_findings:
                sev_summary[f.severity] += 1
            sev_str = ", ".join(f"{s}:{c}" for s, c in
                                sorted(sev_summary.items(),
                                       key=lambda x: SEVERITY_ORDER.get(x[0], 99)))
            lines.append(f"  {vm_name}")
            lines.append(f"    UUID: {vm_uuid}")
            lines.append(f"    Findings: {len(vm_findings)} ({sev_str})")

    # Ghost UUID analysis
    ghost_report = ghost_uuid_analysis(pool, findings)
    if ghost_report:
        lines.append(ghost_report)

    # Detailed findings
    lines.append(f"\n## Detailed Findings\n")
    sorted_findings = sorted(actionable, key=lambda f: f.sort_key())
    current_severity = None
    for i, f in enumerate(sorted_findings, 1):
        if f.severity != current_severity:
            current_severity = f.severity
            lines.append(f"\n### {current_severity}\n")

        lines.append(f"  [{i}] {f.check_id} | {f.object_type} {f.object_uuid}")
        lines.append(f"      Name: {f.object_name}")
        lines.append(f"      Summary: {f.summary}")
        lines.append(f"      Detail: {f.detail}")
        if f.affected_vm_uuid:
            lines.append(f"      Affected VM: {f.affected_vm_name} ({f.affected_vm_uuid})")
        if f.related_uuids:
            lines.append(f"      Related: {', '.join(f.related_uuids[:10])}")
        lines.append("")

    # Info findings summary (condensed)
    if info_findings:
        lines.append(f"\n### INFO ({len(info_findings)} findings — condensed)\n")
        info_by_check = defaultdict(int)
        for f in info_findings:
            info_by_check[f.check_id] += 1
        for check_id, count in sorted(info_by_check.items()):
            lines.append(f"  {check_id}: {count}")

    lines.append("\n" + "=" * 78)
    lines.append("  End of Report")
    lines.append("=" * 78 + "\n")

    return "\n".join(lines)


def write_json_report(findings: List[Finding], pool: PoolData,
                      filepath: str, elapsed: float):
    """Write findings as a JSON file."""
    report = {
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "scan_duration_seconds": round(elapsed, 2),
        "inventory": {
            "srs": len(pool.srs),
            "vdis": len(pool.vdis),
            "vbds": len(pool.vbds),
            "vms": len(pool.vms),
        },
        "summary": {
            sev: sum(1 for f in findings if f.severity == sev)
            for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
        },
        "findings": [f.to_dict() for f in sorted(findings, key=lambda f: f.sort_key())],
    }
    with open(filepath, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"[INFO] JSON report written to {filepath}", file=sys.stderr)


def write_csv_report(findings: List[Finding], filepath: str):
    """Write findings as a CSV file."""
    fieldnames = [
        "severity", "check_id", "object_type", "object_uuid", "object_name",
        "summary", "detail", "affected_vm_uuid", "affected_vm_name",
        "related_uuids",
    ]
    with open(filepath, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for f in sorted(findings, key=lambda f: f.sort_key()):
            row = f.to_dict()
            row["related_uuids"] = "; ".join(row["related_uuids"])
            writer.writerow(row)
    print(f"[INFO] CSV report written to {filepath}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Unified VDI/VM/VBD metadata integrity auditor for XCP-ng",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Check ID reference:
  A1  Self-referential snapshot-of        A2  Contradictory flags
  A3  Circular snapshot-of chains         B1  Orphaned/missing parents
  B2  Snapshots-field corruption          B3  Duplicate parent claims
  B4  Snapshot VDI on live VM             C1  Self-referential vhd-parent
  C2  Dangling vhd-parent                C3  VHD chain crossover
  C4  Cross-SR references                C5  Corrupt snapshot root
  C6  ISO VDI flagged snapshot            C7  Snapshot-of-a-snapshot
  D1  Super-parent sm-config              E1  Dangling VBD
  E2  VBD back-link mismatch             F1  VM snapshot issues
  F2  VM contradictions                   G1  VDI missing flag
""")
    parser.add_argument("--json", metavar="FILE",
                        help="Write findings to a JSON file")
    parser.add_argument("--csv", metavar="FILE",
                        help="Write findings to a CSV file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print progress to stderr")
    parser.add_argument("--skip-info", action="store_true",
                        help="Omit INFO-level findings from output files")
    args = parser.parse_args()

    start_time = datetime.utcnow()

    # Load all data
    pool = PoolData()
    pool.load(verbose=args.verbose)

    # Run all checks
    findings: List[Finding] = []

    checks = [
        ("A1-SELF-REF",            check_a1_self_ref),
        ("A2-CONTRADICTION",       check_a2_contradiction),
        ("A3-CIRCULAR-CHAIN",      check_a3_circular_chain),
        ("B1-ORPHAN/MISSING",      check_b1_orphaned_and_missing_parent),
        ("B2-SNAPSHOTS-FIELD",     check_b2_snapshots_field),
        ("B3-DUPLICATE-CLAIM",     check_b3_duplicate_parent_claims),
        ("B4-SNAP-ON-LIVE-VM",     check_b4_snapshot_on_live_vm),
        ("C1-SELF-VHD-PARENT",     check_c1_vhd_parent_self_ref),
        ("C2-VHD-PARENT-DANGLING", check_c2_vhd_parent_dangling),
        ("C3-CHAIN-CROSSOVER",     check_c3_vhd_chain_crossover),
        ("C4-CROSS-SR",            check_c4_cross_sr),
        ("C5-CORRUPT-SNAP-ROOT",   check_c5_corrupt_snapshot_root),
        ("C6-ISO-FLAGGED-SNAP",    check_c6_iso_flagged_snapshot),
        ("C7-SNAP-OF-SNAP",        check_c7_snap_of_a_snap),
        ("D1-SUPER-PARENT",        check_d1_super_parent_sm_config),
        ("E1-VBD-DANGLING",        check_e1_vbd_dangling),
        ("E2-VBD-BACKLINK",        check_e2_vbd_backlink_mismatch),
        ("F1-VM-SNAPSHOT",         check_f1_vm_snapshot_issues),
        ("F2-VM-CONTRADICTION",    check_f2_vm_contradiction),
        ("G1-VDI-MISSING",         check_g1_vdi_missing),
    ]

    for check_name, check_fn in checks:
        if args.verbose:
            print(f"[INFO] Running check: {check_name}...", file=sys.stderr)
        check_fn(pool, findings)

    elapsed = (datetime.utcnow() - start_time).total_seconds()

    # Filter INFO if requested
    output_findings = findings
    if args.skip_info:
        output_findings = [f for f in findings if f.severity != "INFO"]

    # Generate text report to stdout
    report = generate_text_report(findings, pool, elapsed)
    print(report)

    # Write optional output files
    if args.json:
        write_json_report(output_findings, pool, args.json, elapsed)
    if args.csv:
        write_csv_report(output_findings, args.csv)

    # Exit code: non-zero if CRITICAL or HIGH findings exist
    has_critical = any(f.severity in ("CRITICAL", "HIGH") for f in findings)
    sys.exit(2 if has_critical else 0)


if __name__ == "__main__":
    main()