#!/bin/bash
# xcp-vdi-graph-audit.sh — read-only audit of XCP-ng VM/VDI/VBD/SR/snapshot
# metadata, looking for parent-child graph inconsistencies that confuse XOA
# (VDIs that are their own VHD parent, snapshot flags pointing at deleted
# targets, ISOs flagged as snapshots, VHD chains crossing SR boundaries,
# snapshot-flagged VDIs attached to live VMs, etc.).
#
# Run on the XCP-ng pool master. Bash 4+ required (dom0 ships it). Performs
# only `xe *-list` / `*-param-get` reads — no state changes.

# Note: deliberately NOT using `set -u` — bash 4.2 (XCP-ng dom0) raises
# "unbound variable" on `${!arr[@]}` / `${#arr[@]}` for empty associative
# arrays, which several of our reverse-index maps legitimately are.

if ! command -v xe >/dev/null 2>&1; then
  echo "xe not found — run this on an XCP-ng host." >&2
  exit 1
fi

TMP=$(mktemp -d) ; trap 'rm -rf "$TMP"' EXIT

# ---- xe -> (uuid, field, value) triples -----------------------------------
# This avoids positional-column bugs: each line is independently keyed by
# uuid + field name, so xe's output ordering is irrelevant.

xe_triples() {
  awk '
    /^[[:space:]]*uuid[[:space:]]*\(/ {
      c = index($0, ":")
      cur = substr($0, c + 1)
      sub(/^[[:space:]]+/, "", cur)
      sub(/[[:space:]]+$/, "", cur)
      next
    }
    /^[[:space:]]*$/ { cur = ""; next }
    cur == "" { next }
    {
      c = index($0, ":")
      if (c == 0) next
      lhs = substr($0, 1, c - 1)
      rhs = substr($0, c + 1)
      sub(/[[:space:]]*\(.*$/, "", lhs)
      sub(/^[[:space:]]+/, "", lhs)
      sub(/^[[:space:]]+/, "", rhs)
      sub(/[[:space:]]+$/, "", rhs)
      if (rhs == "<not in database>") rhs = ""
      print cur "\t" lhs "\t" rhs
    }
  '
}

# Extract a single key out of a "k1: v1; k2: v2" map value
map_get() {
  awk -v map="$1" -v key="$2" '
    BEGIN {
      n = split(map, parts, "; ")
      for (i = 1; i <= n; i++) {
        c = index(parts[i], ": ")
        if (c == 0) continue
        if (substr(parts[i], 1, c - 1) == key) { print substr(parts[i], c + 2); exit }
      }
    }'
}

# ---- collect ---------------------------------------------------------------

echo "Collecting VDI / VBD / VM / SR records via xe ..." >&2

xe vdi-list params=uuid,name-label,is-a-snapshot,snapshot-of,sr-uuid,type,managed,missing,vbd-uuids,sm-config \
  | xe_triples > "$TMP/vdi.tsv"
xe vbd-list params=uuid,vm-uuid,vdi-uuid,type,currently-attached,empty,device \
  | xe_triples > "$TMP/vbd.tsv"
xe vm-list  params=uuid,name-label,is-a-snapshot,is-a-template,is-control-domain,snapshot-of,power-state \
  | xe_triples > "$TMP/vm.tsv"
xe sr-list  params=uuid,name-label,type,content-type \
  | xe_triples > "$TMP/sr.tsv"

# ---- load -----------------------------------------------------------------

declare -A VDI_NAME VDI_ISSNAP VDI_SNAPOF VDI_SR VDI_TYPE VDI_MANAGED VDI_MISSING VDI_VBDS VDI_SMCONFIG VDI_VHDPARENT
declare -A VBD_VM VBD_VDI VBD_TYPE VBD_ATTACHED VBD_EMPTY VBD_DEVICE
declare -A VM_NAME VM_ISSNAP VM_ISTEMPL VM_ISCD VM_SNAPOF VM_POWER
declare -A SR_NAME SR_TYPE SR_CONTENT

while IFS=$'\t' read -r u f v; do
  case $f in
    name-label)        VDI_NAME[$u]=$v ;;
    is-a-snapshot)     VDI_ISSNAP[$u]=$v ;;
    snapshot-of)       VDI_SNAPOF[$u]=$v ;;
    sr-uuid)           VDI_SR[$u]=$v ;;
    type)              VDI_TYPE[$u]=$v ;;
    managed)           VDI_MANAGED[$u]=$v ;;
    missing)           VDI_MISSING[$u]=$v ;;
    vbd-uuids)         VDI_VBDS[$u]=$v ;;
    sm-config)         VDI_SMCONFIG[$u]=$v
                       VDI_VHDPARENT[$u]=$(map_get "$v" vhd-parent) ;;
  esac
  : "${VDI_NAME[$u]:=}"
done < "$TMP/vdi.tsv"

while IFS=$'\t' read -r u f v; do
  case $f in
    vm-uuid)            VBD_VM[$u]=$v ;;
    vdi-uuid)           VBD_VDI[$u]=$v ;;
    type)               VBD_TYPE[$u]=$v ;;
    currently-attached) VBD_ATTACHED[$u]=$v ;;
    empty)              VBD_EMPTY[$u]=$v ;;
    device)             VBD_DEVICE[$u]=$v ;;
  esac
  : "${VBD_VM[$u]:=}"
done < "$TMP/vbd.tsv"

while IFS=$'\t' read -r u f v; do
  case $f in
    name-label)        VM_NAME[$u]=$v ;;
    is-a-snapshot)     VM_ISSNAP[$u]=$v ;;
    is-a-template)     VM_ISTEMPL[$u]=$v ;;
    is-control-domain) VM_ISCD[$u]=$v ;;
    snapshot-of)       VM_SNAPOF[$u]=$v ;;
    power-state)       VM_POWER[$u]=$v ;;
  esac
  : "${VM_NAME[$u]:=}"
done < "$TMP/vm.tsv"

while IFS=$'\t' read -r u f v; do
  case $f in
    name-label)   SR_NAME[$u]=$v ;;
    type)         SR_TYPE[$u]=$v ;;
    content-type) SR_CONTENT[$u]=$v ;;
  esac
  : "${SR_NAME[$u]:=}"
done < "$TMP/sr.tsv"

echo "Loaded ${#VDI_NAME[@]} VDIs, ${#VBD_VM[@]} VBDs, ${#VM_NAME[@]} VMs, ${#SR_NAME[@]} SRs." >&2

# ---- reverse / derived indexes --------------------------------------------

declare -A CHILDREN_BY_SNAPOF CHILDREN_BY_VHDPARENT VDI_VBD_BACK
for u in "${!VDI_NAME[@]}"; do
  s=${VDI_SNAPOF[$u]:-}; [[ -n $s ]] && CHILDREN_BY_SNAPOF[$s]+="$u "
  p=${VDI_VHDPARENT[$u]:-}; [[ -n $p ]] && CHILDREN_BY_VHDPARENT[$p]+="$u "
done
for b in "${!VBD_VM[@]}"; do
  v=${VBD_VDI[$b]:-}
  [[ -n $v ]] && VDI_VBD_BACK[$v]+="$b "
done

# Map VDI -> owning user VM(s) (via VBDs, snapshot VMs roll up to their source)
declare -A VDI_OWNER_VM
for u in "${!VDI_NAME[@]}"; do
  for b in ${VDI_VBD_BACK[$u]:-}; do
    vm=${VBD_VM[$b]:-}; [[ -z $vm ]] && continue
    if [[ ${VM_ISSNAP[$vm]:-} == true && -n ${VM_SNAPOF[$vm]:-} ]]; then
      vm=${VM_SNAPOF[$vm]}
    fi
    VDI_OWNER_VM[$u]+="$vm "
  done
done

# ---- run checks -----------------------------------------------------------

declare -A FINDINGS         # category -> "\n"-separated detail lines
declare -A AFFECTED_VMS     # vm-uuid -> "cat1 cat2 ..."
declare -A GHOST_UUIDS      # ghost uuid -> count of references
declare -i TOTAL=0

add() {
  local cat=$1; shift
  FINDINGS[$cat]+="$*"$'\n'
  TOTAL+=1
}
mark_vm() {
  local vm=$1 cat=$2
  [[ -z $vm ]] && return
  case " ${AFFECTED_VMS[$vm]:-} " in *" $cat "*) ;; *) AFFECTED_VMS[$vm]+="$cat " ;; esac
}

vdi_label() {
  local u=$1 sr=${VDI_SR[$1]:-}
  printf '%s [%s] sr=%s type=%s' "$u" "${VDI_NAME[$u]:-?}" "${SR_NAME[$sr]:-?}" "${VDI_TYPE[$u]:-?}"
}

for u in "${!VDI_NAME[@]}"; do
  sr=${VDI_SR[$u]:-}
  issnap=${VDI_ISSNAP[$u]:-}
  snapof=${VDI_SNAPOF[$u]:-}
  vhdp=${VDI_VHDPARENT[$u]:-}

  if [[ -n $vhdp && $vhdp == "$u" ]]; then
    add self_vhd_parent "$(vdi_label "$u") -- vhd-parent points to self"
    for vm in ${VDI_OWNER_VM[$u]:-}; do mark_vm "$vm" self_vhd_parent; done
  fi

  if [[ -n $snapof && $snapof == "$u" ]]; then
    add self_snapshot_of "$(vdi_label "$u") -- snapshot-of points to self"
    for vm in ${VDI_OWNER_VM[$u]:-}; do mark_vm "$vm" self_snapshot_of; done
  fi

  if [[ -n $snapof && -n ${VDI_NAME[$snapof]:-} ]]; then
    ssr=${VDI_SR[$snapof]:-}
    if [[ $ssr != "$sr" ]]; then
      add snap_of_cross_sr "$(vdi_label "$u") -- snapshot-of=$snapof lives in SR ${SR_NAME[$ssr]:-?} ($ssr); real snapshots stay in their source SR"
      for vm in ${VDI_OWNER_VM[$u]:-}; do mark_vm "$vm" snap_of_cross_sr; done
    fi
  fi

  if [[ -n $vhdp && -n ${VDI_NAME[$vhdp]:-} ]]; then
    psr=${VDI_SR[$vhdp]:-}
    if [[ $psr != "$sr" ]]; then
      add vhd_parent_cross_sr "$(vdi_label "$u") -- vhd-parent=$vhdp lives in SR ${SR_NAME[$psr]:-?} ($psr)"
      for vm in ${VDI_OWNER_VM[$u]:-}; do mark_vm "$vm" vhd_parent_cross_sr; done
    fi
  fi

  if [[ -n $vhdp && -z ${VDI_NAME[$vhdp]:-} ]]; then
    add vhd_parent_dangling "$(vdi_label "$u") -- vhd-parent=$vhdp not in XAPI db"
    GHOST_UUIDS[$vhdp]=$(( ${GHOST_UUIDS[$vhdp]:-0} + 1 ))
    for vm in ${VDI_OWNER_VM[$u]:-}; do mark_vm "$vm" vhd_parent_dangling; done
  fi

  if [[ $issnap == true ]]; then
    if [[ -z $snapof ]]; then
      add snap_without_target "$(vdi_label "$u") -- is-a-snapshot=true but snapshot-of empty"
    elif [[ -z ${VDI_NAME[$snapof]:-} ]]; then
      add snap_target_missing "$(vdi_label "$u") -- snapshot-of=$snapof not in XAPI db"
      GHOST_UUIDS[$snapof]=$(( ${GHOST_UUIDS[$snapof]:-0} + 1 ))
    fi
  fi

  if [[ -n $snapof && ${VDI_ISSNAP[$snapof]:-} == true ]]; then
    add snap_of_a_snap "$(vdi_label "$u") -- snapshot-of=$snapof which itself has is-a-snapshot=true"
  fi

  if [[ $issnap == true ]]; then
    for b in ${VDI_VBD_BACK[$u]:-}; do
      [[ ${VBD_TYPE[$b]:-} == CD ]] && continue
      vm=${VBD_VM[$b]:-}; [[ -z $vm ]] && continue
      [[ ${VM_ISSNAP[$vm]:-} == true ]] && continue
      [[ ${VM_ISTEMPL[$vm]:-} == true ]] && continue
      [[ ${VM_ISCD[$vm]:-} == true ]] && continue
      add snap_attached_to_live_vm "$(vdi_label "$u") -- via vbd=$b (${VBD_DEVICE[$b]:-?}) on live VM ${VM_NAME[$vm]:-?} ($vm)"
      mark_vm "$vm" snap_attached_to_live_vm
    done
  fi

  srtype=${SR_TYPE[$sr]:-}
  srcontent=${SR_CONTENT[$sr]:-}
  if [[ $issnap == true ]] && { [[ $srtype == iso ]] || [[ $srtype == udev ]] || [[ $srcontent == iso ]]; }; then
    add iso_flagged_snapshot "$(vdi_label "$u") -- ISO/removable VDI flagged is-a-snapshot=true"
  fi

  # vbd-uuids back-link mismatch (informational; storage GC normalises this)
  declared=$(printf '%s\n' ${VDI_VBDS[$u]:-} | tr ';' '\n' | awk 'NF' | sort -u | tr '\n' ' ')
  actual=$(printf '%s\n' ${VDI_VBD_BACK[$u]:-} | awk 'NF' | sort -u | tr '\n' ' ')
  if [[ $declared != "$actual" ]]; then
    add vbd_back_link_mismatch "$(vdi_label "$u") -- VDI claims vbds=[${declared:-}] but VBDs actually referencing it=[${actual:-}]"
  fi
done

for parent in "${!CHILDREN_BY_VHDPARENT[@]}"; do
  kids=(${CHILDREN_BY_VHDPARENT[$parent]})
  (( ${#kids[@]} < 2 )) && continue
  declare -A owners=()
  for k in "${kids[@]}"; do
    for vm in ${VDI_OWNER_VM[$k]:-}; do owners[$vm]=1; done
  done
  if (( ${#owners[@]} > 1 )); then
    owner_list=""
    for o in "${!owners[@]}"; do owner_list+="${VM_NAME[$o]:-?}($o) "; done
    add chain_crossover "vhd-parent=$parent shared by children of unrelated VMs: $owner_list-- children: ${kids[*]}"
    for o in "${!owners[@]}"; do mark_vm "$o" chain_crossover; done
  fi
  unset owners
done

# --- Corrupt-snapshot-root cluster pass ---
# If one VDI is named as snapshot-of by VDIs that span unrelated owner VMs OR
# span ISO+user SR types, the named target is almost certainly the corrupt root
# (the symptom you described: one bad VDI "owns" ISOs and unrelated VMs' disks
# as its snapshots).
declare -A CORRUPT_ROOTS    # uuid -> child count
for parent in "${!CHILDREN_BY_SNAPOF[@]}"; do
  kids=(${CHILDREN_BY_SNAPOF[$parent]})
  (( ${#kids[@]} < 2 )) && continue

  declare -A owners=() srs=() srtypes=()
  iso_kid=0 user_kid=0
  for k in "${kids[@]}"; do
    ksr=${VDI_SR[$k]:-}
    srs[$ksr]=1
    kst=${SR_TYPE[$ksr]:-}; ksc=${SR_CONTENT[$ksr]:-}
    srtypes[${kst:-?}]=1
    if [[ $kst == iso || $kst == udev || $ksc == iso ]]; then iso_kid=1; else user_kid=1; fi
    for vm in ${VDI_OWNER_VM[$k]:-}; do owners[$vm]=1; done
  done

  reason=""
  (( ${#owners[@]} > 1 )) && reason+="children belong to ${#owners[@]} unrelated VMs; "
  (( iso_kid && user_kid )) && reason+="children mix ISO and user-disk SRs; "
  (( ${#srs[@]} > 1 )) && reason+="children span ${#srs[@]} SRs; "

  if [[ -n $reason ]]; then
    target_desc="$parent"
    if [[ -n ${VDI_NAME[$parent]:-} ]]; then
      target_desc+=" [${VDI_NAME[$parent]}] (sr=${SR_NAME[${VDI_SR[$parent]:-}]:-?} type=${VDI_TYPE[$parent]:-?})"
    else
      target_desc+=" [NOT IN XAPI]"
      [[ -n ${SR_NAME[$parent]:-} ]] && target_desc+=" <-- is SR ${SR_NAME[$parent]}"
      [[ -n ${VM_NAME[$parent]:-} ]] && target_desc+=" <-- is VM ${VM_NAME[$parent]}"
    fi
    add corrupt_snapshot_root "$target_desc -- ${reason}${#kids[@]} children claim snapshot-of=$parent: ${kids[*]}"
    CORRUPT_ROOTS[$parent]=${#kids[@]}
    for o in "${!owners[@]}"; do mark_vm "$o" corrupt_snapshot_root; done
  fi
  unset owners srs srtypes
done

for b in "${!VBD_VM[@]}"; do
  vdi=${VBD_VDI[$b]:-}; empty=${VBD_EMPTY[$b]:-}; btype=${VBD_TYPE[$b]:-}
  [[ $btype == CD ]] && continue
  [[ $empty == true ]] && continue
  [[ -z $vdi ]] && continue
  if [[ -z ${VDI_NAME[$vdi]:-} ]]; then
    vm=${VBD_VM[$b]:-}
    add vbd_dangling "vbd=$b (${VBD_DEVICE[$b]:-?}) on VM ${VM_NAME[$vm]:-?} ($vm) -> missing vdi=$vdi"
    GHOST_UUIDS[$vdi]=$(( ${GHOST_UUIDS[$vdi]:-0} + 1 ))
    mark_vm "$vm" vbd_dangling
  fi
done

for v in "${!VM_ISSNAP[@]}"; do
  [[ ${VM_ISSNAP[$v]} == true ]] || continue
  src=${VM_SNAPOF[$v]:-}
  if [[ -z $src ]]; then
    add vm_snap_no_source "VM snapshot $v [${VM_NAME[$v]:-?}] -- snapshot-of empty"
  elif [[ -z ${VM_NAME[$src]:-} ]]; then
    add vm_snap_source_missing "VM snapshot $v [${VM_NAME[$v]:-?}] -> snapshot-of=$src (missing)"
    GHOST_UUIDS[$src]=$(( ${GHOST_UUIDS[$src]:-0} + 1 ))
  fi
done

for u in "${!VDI_MISSING[@]}"; do
  [[ ${VDI_MISSING[$u]} == true ]] && add vdi_missing "$(vdi_label "$u") -- missing=true (backing file gone)"
done

# ---- report ---------------------------------------------------------------

declare -A CAT_DESC=(
  [self_vhd_parent]="VDIs that list themselves as their own VHD parent"
  [self_snapshot_of]="VDIs whose snapshot-of points to themselves"
  [corrupt_snapshot_root]="Suspected corrupt root: one VDI is named as snapshot-of by VDIs spanning unrelated VMs / SR types"
  [vhd_parent_cross_sr]="VHD parent lives in a different SR (impossible)"
  [snap_of_cross_sr]="snapshot-of points at a VDI in a different SR (impossible)"
  [vhd_parent_dangling]="VHD parent UUID not present in XAPI"
  [snap_without_target]="is-a-snapshot=true but snapshot-of empty"
  [snap_target_missing]="snapshot-of points at a UUID no longer in XAPI"
  [snap_of_a_snap]="snapshot-of points at another snapshot VDI"
  [snap_attached_to_live_vm]="snapshot VDI attached as a live disk on a non-snapshot VM (XOA hides these)"
  [iso_flagged_snapshot]="ISO / removable-SR VDI flagged as a snapshot"
  [vbd_back_link_mismatch]="VDI.vbd-uuids disagrees with actual referencing VBDs"
  [chain_crossover]="One VHD parent has children belonging to unrelated VMs"
  [vbd_dangling]="VBD references a non-existent VDI UUID"
  [vm_snap_no_source]="VM snapshot with empty snapshot-of"
  [vm_snap_source_missing]="VM snapshot whose source VM is gone"
  [vdi_missing]="VDI flagged missing=true"
)

# Severity: things that actively break XOA's disk display come first
SEVERITY_HIGH=(corrupt_snapshot_root self_vhd_parent self_snapshot_of snap_attached_to_live_vm iso_flagged_snapshot chain_crossover vhd_parent_cross_sr snap_of_cross_sr)
SEVERITY_MED=(vhd_parent_dangling snap_target_missing snap_without_target snap_of_a_snap vbd_dangling vm_snap_source_missing vm_snap_no_source vdi_missing)
SEVERITY_LOW=(vbd_back_link_mismatch)

count_lines() { printf '%s' "$1" | grep -c .; }

echo
echo "================================================================"
echo " XCP-ng VDI / snapshot graph audit"
echo " Host: $(hostname)"
pm=$(xe pool-list params=master --minimal 2>/dev/null)
[[ -n $pm ]] && echo " Pool master ref: $pm"
echo " Run:  $(date -Is)"
echo " Inventory: ${#VDI_NAME[@]} VDIs, ${#VBD_VM[@]} VBDs, ${#VM_NAME[@]} VMs, ${#SR_NAME[@]} SRs"
echo "================================================================"

# --- Diagnosis (interpretive) ---
echo
echo "## DIAGNOSIS"
if (( TOTAL == 0 )); then
  echo "  No inconsistencies detected. Graph is internally consistent."
else
  # Heuristic interpretations
  if (( ${#CORRUPT_ROOTS[@]} > 0 )); then
    echo "  - SUSPECTED CORRUPT ROOT(S): the following VDI UUID(s) are named as snapshot-of by VDIs spanning unrelated VMs / SR types:"
    for r in "${!CORRUPT_ROOTS[@]}"; do
      extra=""
      [[ -n ${VDI_NAME[$r]:-} ]] && extra=" [${VDI_NAME[$r]}] (sr=${SR_NAME[${VDI_SR[$r]:-}]:-?})"
      [[ -z ${VDI_NAME[$r]:-} && -n ${SR_NAME[$r]:-} ]] && extra=" <-- is actually SR ${SR_NAME[$r]}"
      [[ -z ${VDI_NAME[$r]:-} && -n ${VM_NAME[$r]:-} ]] && extra=" <-- is actually VM ${VM_NAME[$r]}"
      printf "      %s  (%d children claim it)%s\n" "$r" "${CORRUPT_ROOTS[$r]}" "$extra"
    done
    echo "    This matches the 'one bad VDI owns everything as its snapshots' pattern, often caused by an incomplete rolling-snapshot backup."
  fi
  if [[ -n ${FINDINGS[self_snapshot_of]:-} ]]; then
    n=$(count_lines "${FINDINGS[self_snapshot_of]}")
    echo "  - $n VDI(s) have snapshot-of pointing to themselves. Combined with mass-victim children, this is the smoking-gun for the failure mode you described."
  fi
  if [[ -n ${FINDINGS[snap_target_missing]:-} ]]; then
    # Are most missing snapshot-of targets the same uuid?
    dup_target=$(printf '%s\n' "${FINDINGS[snap_target_missing]}" \
      | grep -oE 'snapshot-of=[a-f0-9-]+' | sort | uniq -c | sort -rn | head -1)
    n=$(awk '{print $1}' <<<"$dup_target"); g=$(awk '{print $2}' <<<"$dup_target" | cut -d= -f2)
    if (( n > 1 )); then
      echo "  - $n VDIs claim snapshot-of=$g, but that UUID is not a VDI."
      if [[ -n ${SR_NAME[$g]:-} ]]; then
        echo "    NOTE: $g is actually an SR UUID (${SR_NAME[$g]}). snapshot-of is pointing at an SR — strong sign of XAPI db corruption."
      elif [[ -n ${VM_NAME[$g]:-} ]]; then
        echo "    NOTE: $g is actually a VM UUID (${VM_NAME[$g]}). snapshot-of is cross-typed."
      fi
    fi
  fi
  if [[ -n ${FINDINGS[snap_attached_to_live_vm]:-} ]]; then
    n=$(count_lines "${FINDINGS[snap_attached_to_live_vm]}")
    echo "  - $n live disks are flagged is-a-snapshot=true. XOA classifies these as snapshots and hides them from the VM's disk panel — this is the symptom you reported."
  fi
  if [[ -n ${FINDINGS[iso_flagged_snapshot]:-} ]]; then
    n=$(count_lines "${FINDINGS[iso_flagged_snapshot]}")
    echo "  - $n ISO/removable VDIs are flagged is-a-snapshot=true. XOA will treat them as snapshots regardless of SR."
  fi
  if [[ -n ${FINDINGS[self_vhd_parent]:-} ]]; then
    n=$(count_lines "${FINDINGS[self_vhd_parent]}")
    echo "  - $n VDIs are their own VHD parent. Coalesce / GC will loop forever on these; cannot be repaired by GC alone."
  fi
  if [[ -n ${FINDINGS[chain_crossover]:-} ]]; then
    n=$(count_lines "${FINDINGS[chain_crossover]}")
    echo "  - $n VHD parents are shared by VDIs belonging to unrelated VMs. This is the classic 'merged chains' corruption — touching one VM's disk affects another's."
  fi
  if [[ -n ${FINDINGS[vhd_parent_cross_sr]:-} ]]; then
    n=$(count_lines "${FINDINGS[vhd_parent_cross_sr]}")
    echo "  - $n VDIs have a vhd-parent in a different SR. Real VHD chains never cross SR boundaries, so these refs are bogus."
  fi
  if (( ${#GHOST_UUIDS[@]} > 0 )); then
    echo "  - ${#GHOST_UUIDS[@]} unique 'ghost' UUIDs are referenced by metadata but do not resolve. Clustered counts below."
  fi
fi

# --- Severity summary table ---
echo
echo "## SUMMARY BY SEVERITY"
print_sev() {
  local title=$1; shift
  local any=0
  for cat in "$@"; do
    local body=${FINDINGS[$cat]:-}; [[ -z $body ]] && continue
    any=1
    printf "  %-30s %4d   %s\n" "$cat" "$(count_lines "$body")" "${CAT_DESC[$cat]}"
  done
  (( any )) && echo
}
echo " [HIGH]   actively breaks XOA disk display / chain integrity"
print_sev HIGH "${SEVERITY_HIGH[@]}"
echo " [MED]    dangling references / orphans"
print_sev MED "${SEVERITY_MED[@]}"
echo " [LOW]    cosmetic / back-link drift"
print_sev LOW "${SEVERITY_LOW[@]}"
echo "  TOTAL findings: $TOTAL"

# --- Affected VMs ---
if (( ${#AFFECTED_VMS[@]} > 0 )); then
  echo
  echo "## VMs WITH FINDINGS"
  for vm in "${!AFFECTED_VMS[@]}"; do
    printf "  %-40s %s  [%s]\n" "${VM_NAME[$vm]:-?}" "$vm" "${AFFECTED_VMS[$vm]}"
  done | sort
fi

# --- Ghost uuid clustering ---
if (( ${#GHOST_UUIDS[@]} > 0 )); then
  echo
  echo "## GHOST UUID REFERENCE COUNTS (uuid referenced N times but not in XAPI)"
  for g in "${!GHOST_UUIDS[@]}"; do
    note=""
    [[ -n ${SR_NAME[$g]:-} ]] && note=" <-- is SR ${SR_NAME[$g]}"
    [[ -n ${VM_NAME[$g]:-} ]] && note=" <-- is VM ${VM_NAME[$g]}"
    printf "  %4d  %s%s\n" "${GHOST_UUIDS[$g]}" "$g" "$note"
  done | sort -rn
fi

# --- Detailed findings ---
echo
echo "## DETAILED FINDINGS"
for cat in "${SEVERITY_HIGH[@]}" "${SEVERITY_MED[@]}" "${SEVERITY_LOW[@]}"; do
  body=${FINDINGS[$cat]:-}; [[ -z $body ]] && continue
  c=$(count_lines "$body")
  echo
  echo "### [$cat] ${CAT_DESC[$cat]}  (n=$c)"
  printf '%s' "$body" | sed 's/^/  - /'
done

echo
echo "----------------------------------------------------------------"
if (( TOTAL == 0 )); then
  echo " Result: clean — no inconsistencies."
else
  echo " Result: $TOTAL findings across ${#AFFECTED_VMS[@]} VMs."
fi
echo "----------------------------------------------------------------"
