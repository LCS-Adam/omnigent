#!/usr/bin/env bash
# Emits the e2e shard matrix as `matrix=<json>` on $GITHUB_OUTPUT. Shared by
# e2e.yml and e2e-ui.yml (they differ in NUM_SHARDS and ALLOW_FORK_PR).
#
# Returns an EMPTY matrix ({"include":[]}) when the run should be skipped:
#   - draft PRs, or
#   - a fork's pull_request UNLESS the caller opts in via ALLOW_FORK_PR=true.
#     e2e no longer needs secrets, so it runs fork PRs directly on
#     `pull_request` (ALLOW_FORK_PR=true), just like CI. e2e-ui still needs the
#     gateway secret, leaves ALLOW_FORK_PR unset, and runs forks via the
#     fork-e2e/** mirror push after a maintainer approves.
# An empty matrix yields zero jobs and therefore NO check-runs. This is the
# whole reason for the indirection: a job-level `if:` skip of a matrixed job
# would instead leave one check-run with an unexpanded
# `E2E Tests (shard ${{ matrix.shard_id }}/...)` name.
#
# Env in:  EVENT_NAME (github.event_name), IS_DRAFT, IS_FORK (both may be empty
#          on non-PR events), ALLOW_FORK_PR (optional, default false), NUM_SHARDS.
# Out:     matrix={"include":[{"shard_id":0,"num_shards":N}, ...]}  (or [] empty)

set -euo pipefail

skip=false
if [[ "${IS_DRAFT:-false}" == "true" ]]; then
  skip=true
fi
if [[ "$EVENT_NAME" == "pull_request" && "${IS_FORK:-false}" == "true" \
      && "${ALLOW_FORK_PR:-false}" != "true" ]]; then
  skip=true
fi

if [[ "$skip" == "true" ]]; then
  echo 'matrix={"include":[]}' >> "$GITHUB_OUTPUT"
  echo "skip: empty matrix (event=$EVENT_NAME draft=${IS_DRAFT:-} fork=${IS_FORK:-})"
  exit 0
fi

inc=""
for ((i = 0; i < NUM_SHARDS; i++)); do
  inc+="{\"shard_id\":$i,\"num_shards\":$NUM_SHARDS},"
done
echo "matrix={\"include\":[${inc%,}]}" >> "$GITHUB_OUTPUT"
echo "run: $NUM_SHARDS shards (event=$EVENT_NAME)"
