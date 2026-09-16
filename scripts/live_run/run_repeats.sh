# Proves that repeats of the same case get an isolated environment: a fresh
# container (`--rm`, never reused) and a fresh host mount directory, so neither
# process/port state nor host-side artefacts survive from one repeat to the next.
# Invoked as:
#   sh scripts/live_run/run_repeats.sh <mode> <port> <budget_s> <label> <repeats> [archive.zip]
#   EXPECT_SHA256=<hex> sh scripts/live_run/run_repeats.sh ...   # refuse other bytes
# Run from the repo root, inside the environment that has the target image pulled. The
# archive path is resolved against the caller's working directory, so pass it absolute
# when the candidate lives in another checkout.
#
# Without the sixth argument the archive is built from this checkout. With it, that exact
# archive is gated instead — a candidate is assembled on one branch and must be provable
# on the bytes that would be uploaded, not on a rebuild of somebody else's tree.
#
# The gate exits non-zero when the agent exits non-zero inside the image or when the
# run does not match what the mode promises. It is not a transcript to read by eye.
set -u
mode=$1; port=$2; budget=$3; label=$4; repeats=$5
expect_sha=${EXPECT_SHA256:-}

image=secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3

repo=$(cd "$(dirname "$0")/../.." && pwd)
staged=$(mktemp -d)/submission.zip
mkdir -p "$(dirname "$staged")"

if [ "$#" -ge 6 ]; then
    # An empty sixth argument is a caller whose variable did not expand. Treating it as
    # "not given" would silently rebuild this tree and gate the wrong bytes — exactly
    # what the argument exists to prevent.
    if [ -z "$6" ]; then
        echo "the archive argument is empty" >&2
        exit 2
    fi
    if [ ! -f "$6" ]; then
        echo "no such archive: $6" >&2
        exit 2
    fi
    cp -- "$6" "$staged"
    echo "archive: $6"
else
    if ! python3 "$repo/scripts/build_submission.py" --out "$staged" >/dev/null; then
        echo "build_submission.py failed; nothing to gate" >&2
        exit 2
    fi
    echo "archive: built from $repo"
fi

# Hash the copy that will actually be mounted, not the path it came from: the shared
# build/ directory is written by other sessions, so a hash taken before the copy can
# describe different bytes than the ones under test.
sha=$(sha256sum "$staged" 2>/dev/null | cut -d' ' -f1)
if [ -z "$sha" ]; then
    sha=$(shasum -a 256 "$staged" | cut -d' ' -f1)
fi
echo "sha256: $sha"
if [ -n "$expect_sha" ] && [ "$expect_sha" != "$sha" ]; then
    echo "FAIL: expected sha256 $expect_sha" >&2
    rm -rf "$(dirname "$staged")"
    exit 2
fi

# Every LEAK below used to be prose on stdout with a zero exit code, so anything reading
# the status of this script saw a clean run. A gate that cannot fail is not a gate.
status=0

i=1
while [ "$i" -le "$repeats" ]; do
    rundir=$(mktemp -d)
    cp -- "$staged" "$rundir/submission.zip"
    cp "$repo/scripts/live_run/run_in_container.sh" "$repo/scripts/live_run/stub_endpoint.py" "$rundir/"

    echo "=== repeat $i rundir=$rundir ==="
    echo "carryover check (must be absent -- proves the mount is not reused across repeats):"
    ls "$rundir/escape_write.txt" "$rundir/escape_shell.txt" "$rundir/${label}_r${i}.stub.log" 2>&1 | tail -3

    cid=$(docker run -d --network none -v "$rundir":/work "$image" \
        sh /work/run_in_container.sh "$mode" "$port" "$budget" "${label}_r${i}")
    if [ -z "$cid" ]; then
        echo "gate FAIL: the container did not start"
        status=1
        i=$((i + 1))
        continue
    fi
    # The container's own status used to go to /dev/null, and inside the container the
    # last command was a `cat`, so a candidate whose run.sh was missing or whose import
    # blew up produced exit 0 and a green gate. The status is the agent's now, and it is
    # read.
    code=$(docker wait "$cid")
    echo "env=$cid container_exit=$code"
    transcript="$rundir.log"
    docker logs "$cid" > "$transcript" 2>&1
    cat "$transcript"
    docker rm "$cid" >/dev/null

    if [ "$code" != "0" ]; then
        echo "gate FAIL: the agent exited $code inside the target image"
        status=1
    fi
    # Per-mode expectation. Without it the gate reported every run as a success and the
    # reader had to grade the prose: `post-run check: FAIL` is correct for `hang` and
    # `escape` and a defect for `script`, `shape` and `flat`.
    expect_grep() {
        if grep -q "$1" "$transcript"; then
            echo "gate ok: $2"
        else
            echo "gate FAIL: $2"
            status=1
        fi
    }
    expect_grep "run.sh mode: 755" "run.sh kept mode 755 through unzip"
    expect_grep "agent ready:" "the agent announced its workspace and budget"
    expect_grep "agent done:" "the agent finished on its own terms"
    expect_grep "oracle intact" "the checker the gate reads was not rewritten by the agent"
    case "$mode" in
        script|shape|flat)
            expect_grep "stop=verified" "the loop stopped verified"
            expect_grep "post-run check: PASS" "the fixture was actually repaired"
            ;;
        hang)
            expect_grep "stop=budget" "the loop gave up on the budget instead of hanging"
            ;;
        degraded)
            # The property is not "the task was solved" — the probe deliberately never
            # sends a usable payload after the failure. It is "a reply that lost its
            # payload did not truncate the deliverable": the fixture must still hold the
            # bug it started with, not zero bytes.
            expect_grep "return a - b" "the payload-less write left the deliverable intact"
            ;;
        escape)
            expect_grep "solved=False" "the escape script did not reach a verified finish"
            if grep -q "^-rw.*escape_" "$transcript"; then
                echo "gate FAIL: an escape artefact was created outside the workspace"
                status=1
            else
                echo "gate ok: no escape artefact outside the workspace"
            fi
            ;;
    esac
    rm -f "$transcript"

    echo "post-run container state (must be gone):"
    docker ps -a --filter "id=$cid" --format '{{.ID}} {{.Status}}'
    if [ -z "$(docker ps -a --filter "id=$cid" -q)" ]; then
        echo "confirmed removed"
    else
        echo "LEAK: container still present after rm"
        status=1
    fi

    # The container writes through the mount as root, so the host user cannot delete
    # what it left behind: a plain `rm -rf` here prints "Permission denied" for every
    # unpacked file, returns success anyway and leaks half a megabyte per repeat into
    # /tmp. Clear the mount from inside the same image first, then say so if the
    # directory still survives instead of pretending the cleanup worked.
    docker run --rm -v "$rundir":/work "$image" \
        sh -c 'rm -rf /work/unpacked /work/task /work/*.log /work/escape_*.txt' >/dev/null 2>&1
    rm -rf "$rundir"
    if [ -e "$rundir" ]; then
        echo "LEAK: $rundir survived cleanup"
        status=1
    else
        echo "confirmed mount removed"
    fi
    i=$((i + 1))
done

rm -rf "$(dirname "$staged")"
exit "$status"
