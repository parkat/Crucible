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
# Usage:  bash hardware_scan.sh  > hardware.json
set -u

j_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

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
  "power_control": { "ipmitool": $HAS_IPMI },
  "bandwidth_gbps": $BW_GBPS,
  "bandwidth_reason": $BW_REASON,
  "notes": "bandwidth_gbps is the MEASURED effective BW for the roofline; null means needs-probe, not failure"
}
JEOF
