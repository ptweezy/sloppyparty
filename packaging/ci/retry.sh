#!/usr/bin/env bash
# Shared retry helper for CI steps. Source this then prefix a flaky, idempotent
# command with `retry` to re-run it with backoff instead of failing the job on
# the first transient blip (package-mirror hiccups, registry/API rate-limits,
# git remote errors, ...).
#
# Usage: source packaging/ci/retry.sh && retry apt-get update

retry() {
	local max=5 delay=10 n=1
	until "$@"; do
		if [ "$n" -ge "$max" ]; then
			echo "::error::giving up after $n attempts: $*" >&2
			return 1
		fi
		echo "::warning::attempt $n/$max failed: $*; retrying in ${delay}s" >&2
		sleep "$delay"
		n=$((n + 1))
		delay=$((delay * 2))
	done
}
