# One command that puts a candidate archive through every endpoint behaviour the stub
# can play, inside the pinned target image, and returns non-zero if any of them fails.
#
#   sh scripts/live_run/gate_archive.sh <archive.zip> [expected-sha256]
#
# Why a wrapper. The candidate is rebuilt several times a night, so a gate result is
# only worth anything next to the bytes it was taken on; and a matrix run by hand is a
# matrix that gets run once. This prints the sha256 it actually gated, one line per
# mode, and a verdict — around forty seconds for the whole set on a warm image.
#
# Modes, and what a failure in each one means:
#   script    the whole tool path to a verified finish; the archive does not start
#   flat      a tool call with no `function` wrapper; the client silently drops to
#             json_schema and pays for it for the rest of the task
#   shape     native tools refused with 400; the format ladder does not descend
#   escape    two attempts to leave the workspace; an artefact appears outside it
#   degraded  two 500s and then a reply that lost its payload; the deliverable is
#             truncated, which is how REVIEW-public-q36 scored a zero
#   hang      the endpoint never answers; the loop does not give up on the budget
set -u
archive=${1:-}
expected=${2:-}

if [ -z "$archive" ]; then
    echo "usage: sh scripts/live_run/gate_archive.sh <archive.zip> [expected-sha256]" >&2
    exit 2
fi
if [ ! -f "$archive" ]; then
    echo "no such archive: $archive" >&2
    exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
image=secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3

# Say which is missing, the environment or the archive. Without this every mode fails
# with "the container did not start" and the verdict blames the bytes for a machine
# that has no docker or has never seen the pinned image.
if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed here; this gate needs the target image" >&2
    exit 2
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "the pinned image is not available locally: $image" >&2
    echo "the gate does not pull it — a machine without it cannot judge these bytes" >&2
    exit 2
fi
stamp=$(date +%H%M%S)
out="$repo/build/gate-$stamp"
mkdir -p "$out"

sha=$(sha256sum "$archive" 2>/dev/null | cut -d' ' -f1)
if [ -z "$sha" ]; then
    sha=$(shasum -a 256 "$archive" | cut -d' ' -f1)
fi
if [ -n "$expected" ] && [ "$expected" != "$sha" ]; then
    echo "FAIL: $archive is $sha, not the expected $expected" >&2
    exit 2
fi

echo "archive: $archive"
echo "sha256:  $sha"
echo "logs:    $out"
echo

status=0
port=8800
for mode in script flat shape escape degraded hang; do
    budget=120
    [ "$mode" = "hang" ] && budget=60
    port=$((port + 1))
    if EXPECT_SHA256="$sha" sh "$here/run_repeats.sh" "$mode" "$port" "$budget" \
        "gate_$mode" 1 "$archive" > "$out/$mode.log" 2>&1; then
        echo "PASS  $mode"
    else
        echo "FAIL  $mode   (see $out/$mode.log)"
        grep '^gate FAIL' "$out/$mode.log" | sed 's/^/        /'
        status=1
    fi
done

echo
if [ "$status" = "0" ]; then
    echo "all six modes passed on $sha"
    echo "this proves the archive starts, unpacks, keeps its modes, walks the tool path,"
    echo "descends the format ladder, refuses to write outside the workspace, survives a"
    echo "payload-less reply and stops on its budget. It says nothing about solvability."
else
    echo "at least one mode failed on $sha — do not ship these bytes without reading the log"
fi
exit "$status"
