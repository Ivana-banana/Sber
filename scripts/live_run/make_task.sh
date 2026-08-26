set -eu
rm -rf /root/airi/task
mkdir -p /root/airi/task
cat > /root/airi/task/calc.py <<'PY'
def add(a, b):
    return a - b
PY
cat > /root/airi/task/check.py <<'PY'
import sys
sys.path.insert(0, "/root/airi/task")
from calc import add

if add(2, 3) != 5:
    print("FAIL: add(2, 3) =", add(2, 3))
    raise SystemExit(1)
print("OK")
PY
echo "--- fixture ---"
ls -la /root/airi/task
python3 /root/airi/task/check.py && echo "baseline: PASSES (wrong, fixture broken)" || echo "baseline: fails as expected"
