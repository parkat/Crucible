#!/usr/bin/env bash
# crucible hw_probe.sh — lightweight ON-DEMAND live status of the target box (disk / mem / cpu / temps /
# gpu). Emits ONE JSON object. Every probe is best-effort: a missing tool yields a null field rather
# than failing, so it degrades gracefully across heterogeneous salvage boxes. This is deliberately run
# only on the operator's Refresh button (never on a timer) so it doesn't perturb the target.
#
# Run:  ssh <box> 'bash -s' < scaffold/hw_probe.sh     (the dashboard does exactly this via boxpaths --ssh)
set +e
now=$(date +%s)

read -r l1 l5 l15 _ < /proc/loadavg 2>/dev/null
cores=$(nproc 2>/dev/null || echo null)

mem=$(free -m 2>/dev/null | awk '/^Mem:/{printf "\"total\":%s,\"used\":%s,\"free\":%s,\"available\":%s",$2,$3,$4,$7}')
[ -z "$mem" ] && mem='"total":null,"used":null,"free":null,"available":null'

disk=$(df -h 2>/dev/null | awk 'NR>1 && $6 ~ /^\/($|home|mnt|data|dev\/|crucible)/ {printf "%s{\"mount\":\"%s\",\"size\":\"%s\",\"used\":\"%s\",\"avail\":\"%s\",\"pct\":\"%s\"}",(c++?",":""),$6,$2,$3,$4,$5}')

# extract the FIRST +NN.N temperature token on the label line (the package temp), NOT $NF — which
# was the crit value and carried a trailing ')' -> "temp_c":100.0) invalid JSON, blanking the whole
# hardware panel on common Intel configs (finding #19).
cpu_temp=$(sensors 2>/dev/null | awk '/Package id 0:|Tctl:|CPU Temp/{if(match($0,/\+[0-9]+(\.[0-9]+)?/)){print substr($0,RSTART+1,RLENGTH-1); exit}}')
[ -z "$cpu_temp" ] && cpu_temp=$(awk '{printf "%.1f",$1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
[ -z "$cpu_temp" ] && cpu_temp=null

# coerce any non-numeric field ([N/A] on some GPUs) to null so the JSON stays valid (finding #19).
gpu=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | awk -F', ' 'NF>=4{for(i=1;i<=4;i++){if($i !~ /^[0-9]+(\.[0-9]+)?$/)$i="null"}printf "\"util\":%s,\"mem_used\":%s,\"mem_total\":%s,\"temp\":%s",$1,$2,$3,$4}')
[ -z "$gpu" ] && gpu='"util":null,"mem_used":null,"mem_total":null,"temp":null'

cat <<JSON
{"epoch":$now,
 "cpu":{"cores":$cores,"load1":${l1:-null},"load5":${l5:-null},"load15":${l15:-null},"temp_c":$cpu_temp},
 "mem_mib":{$mem},
 "disk":[$disk],
 "gpu":{$gpu}}
JSON
