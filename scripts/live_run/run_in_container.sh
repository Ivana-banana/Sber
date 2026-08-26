# Runs one case INSIDE the target image. Invoked as:
#   sh /work/run_in_container.sh <mode> <port> <budget_s> <label>
set -u
mode=$1; port=$2; budget=$3; label=$4

rm -rf /work/unpacked /work/task
mkdir -p /work/unpacked /work/task
unzip -q /work/submission.zip -d /work/unpacked

cat > /work/task/calc.py <<'PY'
def add(a, b):
    return a - b
PY
cat > /work/task/check.py <<'PY'
import sys
sys.path.insert(0, "/work/task")
from calc import add

if add(2, 3) != 5:
    print("FAIL: add(2, 3) =", add(2, 3))
    raise SystemExit(1)
print("OK")
PY

echo "=== container case $label: mode=$mode budget=${budget}s ==="
echo "python: $(python3 --version 2>&1) | run.sh mode: $(stat -c %a /work/unpacked/run.sh)"
python3 /work/stub_endpoint.py --port "$port" --mode "$mode" --escape-dir /work > /work/$label.stub.log 2>&1 &
stub=$!
sleep 1

start=$(python3 -c 'import time;print(time.monotonic())')
cd /work/unpacked
LOCAL_AGENT_WORKDIR=/work/task \
LOCAL_AGENT_MODEL=stub-model \
OPENAI_BASE_URL=http://127.0.0.1:$port/v1 \
OPENAI_API_KEY=not-a-real-key \
AGENT_TIMEOUT_S=$budget \
./run.sh 'В /work/task функция add в calc.py возвращает разность вместо суммы.
Исправь причину и докажи проверкой. Кавычки: "двойные" и '"'"'одинарные'"'"'.'
code=$?
end=$(python3 -c 'import time;print(time.monotonic())')

echo "exit=$code wall=$(python3 -c "print(round($end - $start, 3))")s"
kill $stub 2>/dev/null
echo "--- fixture after ---"
cat /work/task/calc.py
python3 /work/task/check.py && echo "post-run check: PASS" || echo "post-run check: FAIL"
echo "--- escape artefacts ---"
ls -la /work/escape_write.txt /work/escape_shell.txt 2>&1 | tail -2
echo "--- stub log ---"
cat /work/$label.stub.log
