#!/bin/sh
set -eu

nodes="redis-node-1 redis-node-2 redis-node-3 redis-node-4 redis-node-5 redis-node-6"

echo "Waiting for all six Redis nodes..."
for node in $nodes; do
  ready=0
  for attempt in $(seq 1 60); do
    if redis-cli -h "$node" -p 6379 ping 2>/dev/null | grep -q PONG; then
      ready=1
      break
    fi
    sleep 1
  done
  if [ "$ready" -ne 1 ]; then
    echo "Redis node $node is unavailable" >&2
    exit 1
  fi
done

# Persistent node metadata must not be bootstrapped a second time on restart.
known_nodes=$(redis-cli --raw -h redis-node-1 -p 6379 cluster nodes | wc -l | tr -d ' ')
if [ "$known_nodes" -eq 1 ]; then
  echo "Creating Redis Cluster with three primaries and one replica per primary..."
  redis-cli --cluster create \
    redis-node-1:6379 redis-node-2:6379 redis-node-3:6379 \
    redis-node-4:6379 redis-node-5:6379 redis-node-6:6379 \
    --cluster-replicas 1 --cluster-yes
else
  echo "Redis Cluster already initialized; checking readiness without resetting data."
fi

for attempt in $(seq 1 90); do
  info=$(redis-cli --raw -h redis-node-1 -p 6379 cluster info)
  if printf '%s\n' "$info" | grep -q 'cluster_state:ok' &&
     printf '%s\n' "$info" | grep -q 'cluster_slots_assigned:16384' &&
     redis-cli --raw -h redis-node-1 -p 6379 cluster nodes |
       awk '{ if ($3 ~ /(^|,)master(,|$)/) primaries++; if ($3 ~ /(^|,)slave(,|$)/) replicas++ } END { exit !(primaries == 3 && replicas == 3) }'
  then
    echo "Redis Cluster ready: 3 primaries, 3 replicas, all 16,384 slots assigned."
    exit 0
  fi
  sleep 1
done

echo "Redis Cluster did not become healthy; inspect redis-cli cluster nodes and cluster info." >&2
exit 1
