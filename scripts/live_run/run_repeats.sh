# Proves that repeats of the same case get an isolated environment: a fresh
# container (`--rm`, never reused) and a fresh host mount directory, so neither
# process/port state nor host-side artefacts survive from one repeat to the next.
# Invoked as:
#   sh scripts/live_run/run_repeats.sh <mode> <port> <budget_s> <label> <repeats>
# Run from the repo root, inside the environment that has the target image pulled.
set -u
mode=$1; port=$2; budget=$3; label=$4; repeats=$5

image=secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3

repo=$(cd "$(dirname "$0")/../.." && pwd)
python3 "$repo/scripts/build_submission.py" >/dev/null

# Every LEAK below used to be prose on stdout with a zero exit code, so anything reading
# the status of this script saw a clean run. A gate that cannot fail is not a gate.
status=0

i=1
while [ "$i" -le "$repeats" ]; do
    rundir=$(mktemp -d)
    cp "$repo/build/submission.zip" "$rundir/submission.zip"
    cp "$repo/scripts/live_run/run_in_container.sh" "$repo/scripts/live_run/stub_endpoint.py" "$rundir/"

    echo "=== repeat $i rundir=$rundir ==="
    echo "carryover check (must be absent -- proves the mount is not reused across repeats):"
    ls "$rundir/escape_write.txt" "$rundir/escape_shell.txt" "$rundir/${label}_r${i}.stub.log" 2>&1 | tail -3

    cid=$(docker run -d --network none -v "$rundir":/work "$image" \
        sh /work/run_in_container.sh "$mode" "$port" "$budget" "${label}_r${i}")
    docker wait "$cid" >/dev/null
    echo "env=$cid"
    docker logs "$cid" 2>&1
    docker rm "$cid" >/dev/null

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

exit "$status"
