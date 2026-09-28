#!/usr/bin/env bash
docker rm -f ssc-appdb-spike >/dev/null 2>&1 && echo "removed ssc-appdb-spike" || echo "not running"
