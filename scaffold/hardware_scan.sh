#!/usr/bin/env bash
# crucible hardware_scan.sh — runs ON THE TARGET, emits JSON to stdout.
#
# Drives the ISA guard (doctrine/05) and every roofline (doctrine/01). The single most
# important output is MEASURED memory bandwidth (STREAM triad), because DDR spec sheets
# are fiction once you account for populated channels, rank count, and the IMC.
#
# If no C compiler is present, bandwidth is reported as null with a reason — that is a
# logged "unknown, needs probe", NOT a failure (doctrine/02).
#
# Emits a COMPLETE contract: isa, topology, memory, storage, power_control{ipmitool,
# wake_on_lan,wol_mac,wol_iface} (ethtool, root-only Wake-on flag via passwordless sudo),
# and gpu{present,name,arch(sm_NN),vram_total_mib,driver,cuda_toolkit} (nvidia-smi). Every
# detected field degrades to false/null when its tool is absent.
#
# RECONCILE (existing box): on a FIRST scan, write the output straight to hardware.json.
# On a RE-scan of a box whose hardware.json has hand-curated fields (measured
# bandwidth_gbps, bandwidth_reason, notes, gpu.history/note, power_control.recovery_note/
# wol_helper, storage.free_gb), DEEP-MERGE this output UNDER the curated file — curated
# wins on conflict — so detected values refresh but measurements/prose are never clobbered.
#
# Usage:  bash hardware_scan.sh  > hardware.json        # first scan
#         bash hardware_scan.sh  > /tmp/scan.json       # re-scan, then deep-merge (above)
set -u

j_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# emit a JSON string, or bare null when empty (for fields that degrade gracefully)
j_or_null() { if [ -z "$1" ]; then printf 'null'; else printf '"%s"' "$(j_str "$1")"; fi; }

# ---- ISA flags (presence/absence is what the ISA guard checks) ---------------
FLAGS="$(grep -m1 '^flags' /proc/cpuinfo 2>/dev/null | cut -d: -f2-)"
[ -z "$FLAGS" ] && FLAGS="$(grep -m1 '^Features' /proc/cpuinfo 2>/dev/null | cut -d: -f2-)"  # ARM
has() { case " $FLAGS " in *" $1 "*) echo true ;; *) echo false ;; esac; }

ISA_SSE2=$(has sse2); ISA_SSSE3=$(has ssse3); ISA_SSE41=$(has sse4_1)
ISA_SSE42=$(has sse4_2); ISA_AVX=$(has avx); ISA_AVX2=$(has avx2)
ISA_AVX512F=$(has avx512f); ISA_F16C=$(has f16c); ISA_FMA=$(has fma)
ISA_NEON=$(has neon); ISA_ASIMD=$(has asimd)

# ---- topology ----------------------------------------------------------------
ARCH="$(uname -m)"
NPROC="$(nproc 2>/dev/null || echo 0)"
if command -v lscpu >/dev/null 2>&1; then
  CORES_PER_SOCKET="$(lscpu | awk -F: '/Core\(s\) per socket/{gsub(/ /,"",$2);print $2}')"
  SOCKETS="$(lscpu | awk -F: '/Socket\(s\)/{gsub(/ /,"",$2);print $2}')"
  THREADS_PER_CORE="$(lscpu | awk -F: '/Thread\(s\) per core/{gsub(/ /,"",$2);print $2}')"
  NUMA_NODES="$(lscpu | awk -F: '/NUMA node\(s\)/{gsub(/ /,"",$2);print $2}')"
  MODEL_NAME="$(lscpu | awk -F: '/Model name/{sub(/^[ \t]+/,"",$2);print $2; exit}')"
  L2="$(lscpu | awk -F: '/L2 cache/{sub(/^[ \t]+/,"",$2);print $2; exit}')"
  L3="$(lscpu | awk -F: '/L3 cache/{sub(/^[ \t]+/,"",$2);print $2; exit}')"
else
  CORES_PER_SOCKET=0; SOCKETS=1; THREADS_PER_CORE=1; NUMA_NODES=1
  MODEL_NAME="$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')"; L2=""; L3=""
fi

# ---- memory ------------------------------------------------------------------
MEM_KB="$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
MEM_BYTES=$(( MEM_KB * 1024 ))
# DIMM detail (needs root via the sudoers grant); best-effort.
DIMM_SPEED=""; DIMM_RANK=""; CHANNELS_POP=""
if command -v dmidecode >/dev/null 2>&1; then
  DIMM_SPEED="$(dmidecode -t memory 2>/dev/null | awk -F: '/Configured Memory Speed|Speed:/{gsub(/^[ \t]+/,"",$2); if($2 !~ /Unknown/ && $2!=""){print $2; exit}}')"
  DIMM_RANK="$(dmidecode -t memory 2>/dev/null | awk -F: '/Rank:/{gsub(/ /,"",$2); if($2!=""){print $2; exit}}')"
  CHANNELS_POP="$(dmidecode -t memory 2>/dev/null | grep -c 'Size:.*[0-9]* *[MG]B')"
fi

# ---- storage (sets the page-from-disk regime) --------------------------------
ROOT_DEV="$(findmnt -no SOURCE / 2>/dev/null | sed 's/[0-9]*$//; s|/dev/||')"
ROTA="$(cat /sys/block/${ROOT_DEV}/queue/rotational 2>/dev/null || echo unknown)"
case "$ROTA" in 0) STORAGE="ssd";; 1) STORAGE="hdd";; *) STORAGE="unknown";; esac

# ---- power control available? (for watchdog recovery, doctrine/05) -----------
HAS_IPMI=false; command -v ipmitool >/dev/null 2>&1 && HAS_IPMI=true

# ---- Wake-on-LAN (so the resolver can wake a sleeping box, doctrine/05) -------
# Magic-packet ("Wake-on: g") + the MAC/iface to target it. nulls if undetectable.
WOL_ON=false; WOL_MAC=""; WOL_IFACE=""
WOL_IFACE="$(ip route 2>/dev/null | awk '/^default/{print $5; exit}')"
[ -z "$WOL_IFACE" ] && WOL_IFACE="$(ls /sys/class/net 2>/dev/null | grep -v '^lo$' | head -1)"
if [ -n "$WOL_IFACE" ]; then
  WOL_MAC="$(cat "/sys/class/net/$WOL_IFACE/address" 2>/dev/null)"
  if command -v ethtool >/dev/null 2>&1; then
    # The "Wake-on:" line is root-only; try passwordless sudo first (startup grants it),
    # fall back to plain ethtool. The Supports line confirms the NIC can do magic-packet.
    ETH="$(sudo -n ethtool "$WOL_IFACE" 2>/dev/null)"; [ -z "$ETH" ] && ETH="$(ethtool "$WOL_IFACE" 2>/dev/null)"
    WOL_FLAG="$(printf '%s\n' "$ETH" | awk -F: '/^[[:space:]]*Wake-on:/{gsub(/ /,"",$2);print $2}' | tail -1)"
    case "$WOL_FLAG" in *g*) WOL_ON=true;; esac   # 'g' = magic-packet wake armed
  fi
fi

# ---- GPU (drives the build-cuda<NN> selection + the hybrid CPU/GPU budget) ----
# nvidia-smi -> present/name/arch(sm_NN)/vram/driver/cuda. present:false if absent.
GPU_PRESENT=false; GPU_NAME=""; GPU_ARCH=""; GPU_VRAM="null"; GPU_DRIVER=""; GPU_CUDA=""
if command -v nvidia-smi >/dev/null 2>&1; then
  # compute_cap query needs a recent driver; fall back to name-only if it errors.
  GLINE="$(nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap \
            --format=csv,noheader,nounits 2>/dev/null | head -1)"
  [ -z "$GLINE" ] && GLINE="$(nvidia-smi --query-gpu=name,memory.total,driver_version \
            --format=csv,noheader,nounits 2>/dev/null | head -1)"
  if [ -n "$GLINE" ]; then
    GPU_PRESENT=true
    GPU_NAME="$(printf '%s' "$GLINE"  | awk -F',' '{sub(/^ +/,"",$1);print $1}')"
    GVRAM="$(printf '%s' "$GLINE"     | awk -F',' '{gsub(/ /,"",$2);print $2}')"
    case "$GVRAM" in ''|*[!0-9]*) GPU_VRAM="null";; *) GPU_VRAM="$GVRAM";; esac
    GPU_DRIVER="$(printf '%s' "$GLINE"| awk -F',' '{gsub(/ /,"",$3);print $3}')"
    CC="$(printf '%s' "$GLINE"        | awk -F',' '{gsub(/ /,"",$4);print $4}')"   # e.g. 5.0
    [ -n "$CC" ] && GPU_ARCH="sm_$(printf '%s' "$CC" | tr -d '.')"
    # CUDA runtime version from the smi header, else nvcc if a toolkit is installed.
    GPU_CUDA="$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -1)"
    [ -z "$GPU_CUDA" ] && command -v nvcc >/dev/null 2>&1 && \
      GPU_CUDA="$(nvcc --version 2>/dev/null | awk '/release/{gsub(/,/,"",$5);print $5;exit}')"
  fi
fi

# ---- MEASURED bandwidth: inline STREAM triad ---------------------------------
BW_GBPS="null"; BW_REASON="\"not measured\""
CC=""; for c in cc gcc clang; do command -v "$c" >/dev/null 2>&1 && { CC="$c"; break; }; done
if [ -n "$CC" ]; then
  TMP="$(mktemp -d)"
  cat > "$TMP/stream.c" << 'CEOF'
/* Minimal STREAM triad: a[i] = b[i] + scale*c[i]. Reports best GB/s over reps.
   Array sized to dwarf LLC so we measure DRAM, not cache. */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#ifdef _OPENMP
#include <omp.h>
#endif
#ifndef N
#define N (40*1000*1000)   /* 40M doubles/array -> ~960MB working set */
#endif
static double a[N], b[N], c[N];
static double now(){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec*1e-9; }
int main(void){
  const double scale=3.0; double best=1e30;
  #pragma omp parallel for
  for(long i=0;i<N;i++){ a[i]=1.0; b[i]=2.0; c[i]=0.5; }
  for(int r=0;r<8;r++){
    double t=now();
    #pragma omp parallel for
    for(long i=0;i<N;i++) a[i]=b[i]+scale*c[i];
    t=now()-t;
    if(t<best) best=t;
  }
  /* triad touches 3 arrays per element (2 read + 1 write) */
  double bytes=3.0*sizeof(double)*(double)N;
  printf("%.2f\n", bytes/best/1e9);
  if(a[N-1]<0) return 1;   /* prevent dead-code elimination */
  return 0;
}
CEOF
  # Compile WITHOUT -march=native concerns (this is the target itself); try OpenMP.
  if "$CC" -O3 -fopenmp -o "$TMP/stream" "$TMP/stream.c" 2>/dev/null \
     || "$CC" -O3 -o "$TMP/stream" "$TMP/stream.c" 2>/dev/null; then
    OUT="$("$TMP/stream" 2>/dev/null)"
    if [ -n "$OUT" ]; then BW_GBPS="$OUT"; BW_REASON="\"STREAM triad, multi-thread best-of-8\""; fi
  else
    BW_REASON="\"compile failed\""
  fi
  rm -rf "$TMP"
else
  BW_REASON="\"no C compiler on target\""
fi

# ---- emit JSON ---------------------------------------------------------------
cat << JEOF
{
  "scanned_epoch": $(date +%s),
  "arch": "$(j_str "$ARCH")",
  "model_name": "$(j_str "$MODEL_NAME")",
  "isa": {
    "sse2": $ISA_SSE2, "ssse3": $ISA_SSSE3, "sse4_1": $ISA_SSE41, "sse4_2": $ISA_SSE42,
    "avx": $ISA_AVX, "avx2": $ISA_AVX2, "avx512f": $ISA_AVX512F,
    "f16c": $ISA_F16C, "fma": $ISA_FMA, "neon": $ISA_NEON, "asimd": $ISA_ASIMD
  },
  "topology": {
    "nproc": $NPROC, "sockets": ${SOCKETS:-1}, "cores_per_socket": ${CORES_PER_SOCKET:-0},
    "threads_per_core": ${THREADS_PER_CORE:-1}, "numa_nodes": ${NUMA_NODES:-1},
    "l2_cache": "$(j_str "$L2")", "l3_cache": "$(j_str "$L3")"
  },
  "memory": {
    "total_bytes": $MEM_BYTES,
    "dimm_speed": "$(j_str "$DIMM_SPEED")", "dimm_rank": "$(j_str "$DIMM_RANK")",
    "channels_populated": "$(j_str "$CHANNELS_POP")"
  },
  "storage": { "root_device": "$(j_str "$ROOT_DEV")", "type": "$STORAGE" },
  "power_control": {
    "ipmitool": $HAS_IPMI,
    "wake_on_lan": $WOL_ON,
    "wol_mac": $(j_or_null "$WOL_MAC"),
    "wol_iface": $(j_or_null "$WOL_IFACE")
  },
  "gpu": {
    "present": $GPU_PRESENT,
    "name": $(j_or_null "$GPU_NAME"),
    "arch": $(j_or_null "$GPU_ARCH"),
    "vram_total_mib": $GPU_VRAM,
    "driver": $(j_or_null "$GPU_DRIVER"),
    "cuda_toolkit": $(j_or_null "$GPU_CUDA")
  },
  "bandwidth_gbps": $BW_GBPS,
  "bandwidth_reason": $BW_REASON,
  "notes": "bandwidth_gbps is the MEASURED effective BW for the roofline; null means needs-probe, not failure"
}
JEOF
