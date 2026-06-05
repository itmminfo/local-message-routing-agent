#!/bin/sh
set -eu

OLLAMA_HOST="${OLLAMA_HOST:-http://ollama:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"
OLLAMA_WAIT_TIMEOUT_SECONDS="${OLLAMA_WAIT_TIMEOUT_SECONDS:-120}"
OLLAMA_WARMUP_ENABLED="${OLLAMA_WARMUP_ENABLED:-true}"
OLLAMA_WARMUP_TIMEOUT_SECONDS="${OLLAMA_WARMUP_TIMEOUT_SECONDS:-180}"
OLLAMA_PULL_MAX_ATTEMPTS="${OLLAMA_PULL_MAX_ATTEMPTS:-3}"
OLLAMA_PULL_RETRY_DELAY_SECONDS="${OLLAMA_PULL_RETRY_DELAY_SECONDS:-5}"
OLLAMA_MODEL_EXPECTED_DIGEST="${OLLAMA_MODEL_EXPECTED_DIGEST:-}"

# Ollama stores an untagged model (e.g. "qwen2.5") as "<name>:latest", so match
# that form when scanning /api/tags. A tagged model (e.g. "qwen2.5:3b") is used
# verbatim. Without this, an untagged OLLAMA_MODEL would never match and the
# script would wrongly report the pulled model as missing.
case "$OLLAMA_MODEL" in
    *:*) OLLAMA_MODEL_REF="$OLLAMA_MODEL" ;;
    *) OLLAMA_MODEL_REF="${OLLAMA_MODEL}:latest" ;;
esac

# Optional provenance check: if OLLAMA_MODEL_EXPECTED_DIGEST is set, the digest
# reported by /api/tags for the model must contain it, otherwise startup fails.
# Unset by default, so this is opt-in and does not change normal behavior.
verify_model_digest() {
    if [ -z "$OLLAMA_MODEL_EXPECTED_DIGEST" ]; then
        return 0
    fi
    # Accept an optional sha256: prefix, then require a full 64-hex digest so a
    # short/partial value cannot match by accident.
    digest_hex="${OLLAMA_MODEL_EXPECTED_DIGEST#sha256:}"
    if [ "${#digest_hex}" -ne 64 ]; then
        echo "Invalid OLLAMA_MODEL_EXPECTED_DIGEST: expected 64 hex chars (optionally sha256: prefixed)." >&2
        exit 1
    fi
    case "$digest_hex" in
        *[!0-9a-f]*)
            echo "Invalid OLLAMA_MODEL_EXPECTED_DIGEST: non-hex characters." >&2
            exit 1
            ;;
    esac
    # Bind the digest to THIS model's object, not to any "digest" field in the
    # response: match "name":"$OLLAMA_MODEL_REF" followed, within the same JSON
    # object (before the next "}"), by "digest":"$digest_hex". Without this, a
    # different model's digest in a shared volume could satisfy the check. It is still
    # a regex over JSON, not a structural parse; a production check would use a JSON
    # parser (jq) for an unambiguous model -> digest mapping.
    if printf "%s" "$tags" | grep -Eq "\"name\": *\"$OLLAMA_MODEL_REF\"[^}]*\"digest\": *\"$digest_hex\""; then
        echo "Ollama model ${OLLAMA_MODEL} digest verified (sha256:${digest_hex})."
    else
        echo "Ollama model ${OLLAMA_MODEL} digest mismatch: ${digest_hex} is not the digest of ${OLLAMA_MODEL_REF} in /api/tags." >&2
        exit 1
    fi
}

warm_up_model() {
    case "$OLLAMA_WARMUP_ENABLED" in
        true | TRUE | 1 | yes | YES)
            echo "Warming up Ollama model ${OLLAMA_MODEL} through ${OLLAMA_HOST}/api/chat (timeout ${OLLAMA_WARMUP_TIMEOUT_SECONDS}s)..."
            warmup_payload="$(printf '{"model":"%s","messages":[{"role":"user","content":"Return OK."}],"stream":false,"options":{"temperature":0}}' "$OLLAMA_MODEL")"

            if curl -fsS --max-time "$OLLAMA_WARMUP_TIMEOUT_SECONDS" -o /dev/null -X POST "${OLLAMA_HOST}/api/chat" \
                -H "Content-Type: application/json" \
                -d "$warmup_payload"; then
                echo "Ollama model ${OLLAMA_MODEL} warm-up completed."
            else
                status=$?
                if [ "$status" -eq 28 ]; then
                    echo "Failed to warm up Ollama model ${OLLAMA_MODEL}: timed out after ${OLLAMA_WARMUP_TIMEOUT_SECONDS}s." >&2
                else
                    echo "Failed to warm up Ollama model ${OLLAMA_MODEL}." >&2
                fi
                echo "Warm-up failed; continuing because the model is already pulled. The first request will load it into memory." >&2
            fi
            ;;
        false | FALSE | 0 | no | NO)
            echo "Warm-up is disabled by OLLAMA_WARMUP_ENABLED=${OLLAMA_WARMUP_ENABLED}."
            ;;
        *)
            echo "Invalid OLLAMA_WARMUP_ENABLED value: ${OLLAMA_WARMUP_ENABLED}. Use true or false." >&2
            exit 1
            ;;
    esac
}

# Pull is the most failure-prone step (large download over the network), so it is
# retried with a linear backoff. A per-attempt timeout is intentionally not set:
# a model download is legitimately long and must not be killed mid-transfer.
pull_model() {
    pull_payload="$(printf '{"model":"%s","stream":false}' "$OLLAMA_MODEL")"
    attempt=1
    while [ "$attempt" -le "$OLLAMA_PULL_MAX_ATTEMPTS" ]; do
        if curl -fsS -X POST "${OLLAMA_HOST}/api/pull" \
            -H "Content-Type: application/json" \
            -d "$pull_payload"; then
            return 0
        fi

        if [ "$attempt" -ge "$OLLAMA_PULL_MAX_ATTEMPTS" ]; then
            break
        fi

        backoff=$((attempt * OLLAMA_PULL_RETRY_DELAY_SECONDS))
        echo "Pull attempt ${attempt}/${OLLAMA_PULL_MAX_ATTEMPTS} failed; retrying in ${backoff}s..." >&2
        sleep "$backoff"
        attempt=$((attempt + 1))
    done
    return 1
}

echo "Waiting for Ollama API at ${OLLAMA_HOST}..."

elapsed=0
tags=""

while true; do
    if tags="$(curl -fsS "${OLLAMA_HOST}/api/tags")"; then
        break
    fi

    if [ "$elapsed" -ge "$OLLAMA_WAIT_TIMEOUT_SECONDS" ]; then
        echo "Ollama API was not reachable after ${OLLAMA_WAIT_TIMEOUT_SECONDS}s." >&2
        exit 1
    fi

    sleep 2
    elapsed=$((elapsed + 2))
    echo "Still waiting for Ollama API... ${elapsed}s"
done

echo "Ollama API is reachable."

if printf "%s" "$tags" | grep -F "\"${OLLAMA_MODEL_REF}\"" >/dev/null; then
    echo "Ollama model ${OLLAMA_MODEL} is already available."
    verify_model_digest
    warm_up_model
    exit 0
fi

echo "Pulling Ollama model ${OLLAMA_MODEL} into server ${OLLAMA_HOST}..."

if ! pull_model; then
    echo "Failed to pull Ollama model ${OLLAMA_MODEL} after ${OLLAMA_PULL_MAX_ATTEMPTS} attempts." >&2
    exit 1
fi

echo "Verifying Ollama model ${OLLAMA_MODEL}..."
tags="$(curl -fsS "${OLLAMA_HOST}/api/tags")"

if printf "%s" "$tags" | grep -F "\"${OLLAMA_MODEL_REF}\"" >/dev/null; then
    echo "Ollama model ${OLLAMA_MODEL} is ready."
    verify_model_digest
    warm_up_model
    exit 0
fi

echo "Ollama pull completed, but model ${OLLAMA_MODEL} was not found." >&2
exit 1
