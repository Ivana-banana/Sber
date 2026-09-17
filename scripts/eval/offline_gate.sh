#!/bin/bash
# Level-4 gate from CLAUDE.md: run a BUILT submission zip inside the target image
# `secureintelligent/acp` with the network off, from the unpacked archive.
#
#   scripts/eval/offline_gate.sh build/afk-treat9.zip treat9
#
# Checks, in one container run: the mode `run.sh` keeps after each of the two
# extraction paths an evaluator may take, the contract call with a hostile
# instruction, the two usage paths, and an import of every `agent.*` module on the
# image's own Python. It needs the image locally; pull it by digest first.
#
# What it does NOT prove: that the agent SOLVES anything. It answers gate A and B
# of the win formula (start and environment), not gate C.
set -uo pipefail
ZIP="$1"; LABEL="$2"
IMG="secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3"
WORK="$(dirname "$0")/work-$LABEL"
rm -rf "$WORK"; mkdir -p "$WORK"
cp "$ZIP" "$WORK/submission.zip"

cat > "$WORK/inside.sh" <<'INSIDE'
#!/bin/sh
# runs inside the container, no network
set -u
echo "### python: $(python3 -VV 2>&1)"
echo "### id: $(id)"
echo "### net: $(ip -o addr show 2>/dev/null | wc -l) addrs"
cd /tmp || exit 9

echo "=== A. unzip (exec bit preserved) ==="
mkdir -p /tmp/a && cd /tmp/a || exit 9
if command -v unzip >/dev/null 2>&1; then
  unzip -q /work/submission.zip -d /tmp/a
else
  echo "UNZIP-MISSING (using python zipfile, exec bit lost)"
  python3 -c "import zipfile,os,sys; z=zipfile.ZipFile('/work/submission.zip'); z.extractall('/tmp/a')"
fi
ls -la /tmp/a | head -20
echo "--- run.sh mode: $(stat -c '%a %n' /tmp/a/run.sh 2>/dev/null || echo MISSING)"

echo "=== A1. contract call, fake endpoint, hostile text ==="
cd /tmp/a || exit 9
LOCAL_AGENT_MODEL=gate-fake-model OPENAI_BASE_URL=http://127.0.0.1:9/v1 OPENAI_API_KEY=gate-not-a-key \
  timeout 120 ./run.sh 'smoke "test" with $pecial \chars and a newline
second line' ; echo "--- exit=$?"

echo "=== A2. no env at all ==="
timeout 120 ./run.sh 'plain instruction'; echo "--- exit=$?"

echo "=== A3. empty instruction ==="
timeout 60 ./run.sh ''; echo "--- exit=$?"

echo "=== A4. no argument ==="
timeout 60 ./run.sh; echo "--- exit=$?"

echo "=== B. python zipfile extraction (exec bit stripped) ==="
mkdir -p /tmp/b && cd /tmp/b || exit 9
python3 -c "import zipfile; zipfile.ZipFile('/work/submission.zip').extractall('/tmp/b')"
echo "--- run.sh mode: $(stat -c '%a %n' /tmp/b/run.sh 2>/dev/null || echo MISSING)"
cd /tmp/b || exit 9
LOCAL_AGENT_MODEL=gate-fake-model OPENAI_BASE_URL=http://127.0.0.1:9/v1 OPENAI_API_KEY=k \
  timeout 120 ./run.sh 'exec-bit stripped run'; echo "--- exit(direct)=$?"
LOCAL_AGENT_MODEL=gate-fake-model OPENAI_BASE_URL=http://127.0.0.1:9/v1 OPENAI_API_KEY=k \
  timeout 120 sh ./run.sh 'exec-bit stripped run via sh'; echo "--- exit(sh)=$?"

echo "=== C. import every agent module on this python ==="
cd /tmp/a || exit 9
python3 - <<'PY'
import importlib, pkgutil, sys, traceback
sys.path.insert(0, '/tmp/a')
import agent
bad = 0
for m in pkgutil.walk_packages(agent.__path__, 'agent.'):
    try:
        importlib.import_module(m.name)
    except Exception:
        bad += 1
        print('IMPORT-FAIL', m.name)
        traceback.print_exc()
print('import check done, failures =', bad)
PY

echo "=== D. proc scan sanity (C3/C4 code path) in this container ==="
python3 - <<'PY'
import sys, traceback
sys.path.insert(0, '/tmp/a')
try:
    from agent import tools
    names = [n for n in dir(tools) if 'proc' in n.lower() or 'serv' in n.lower()]
    print('tools proc-ish names:', names)
except Exception:
    traceback.print_exc()
PY
echo "### INSIDE DONE"
INSIDE
chmod +x "$WORK/inside.sh"

docker run --rm --network none --name "critic-acp-$LABEL" \
  -v "$(cd "$WORK" && pwd)":/work:ro \
  --entrypoint /bin/sh "$IMG" /work/inside.sh 2>&1
echo "### CONTAINER EXIT=$?"
