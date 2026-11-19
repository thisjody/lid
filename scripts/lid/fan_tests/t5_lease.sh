#!/bin/bash
# t5: the shard holds a keepalive lease for its timeout (a box stops itself after 30 idle minutes otherwise).
ls -l --time-style=+%FT%T /run/discobox/keepalive/ > "$OUT/lease.txt" 2>&1
date -u +%FT%T > "$OUT/now.txt"
