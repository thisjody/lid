#!/bin/bash
# t4: shard 0 fails with output; shard 1 overruns its timeout. Both must be reported, results pulled, boxes archived.
echo "shard $SHARD_INDEX wrote this" > "$OUT/marker.txt"
[ "$SHARD_INDEX" = 0 ] && exit 3
sleep 600
