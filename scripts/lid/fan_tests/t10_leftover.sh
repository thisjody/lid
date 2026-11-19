#!/bin/bash
# t10: a background process the job leaves behind must not change out/ after the result: the runner kills it first.
echo before > "$OUT/frozen.txt"
( sleep 15; echo after > "$OUT/frozen.txt" ) &
exit 0
