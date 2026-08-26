# usage: sh run_case.sh <mode> <port> <timeout_s> <label>
set -u
mode=$1; port=$2; budget=$3; label=$4
sh /root/airi/make_task.sh >/root/airi/$label.fixture.log 2>&1
python3 /root/airi/stub_endpoint.py --port "$port" --mode "$mode" >/root/airi/$label.stub.log 2>&1 &
stub=$!
sleep 1
echo "=== case $label: mode=$mode budget=${budget}s ==="
start=$(date +%s.%N)
cd /root/airi/unpacked
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/root \
    LOCAL_AGENT_WORKDIR=/root/airi/task \
    LOCAL_AGENT_MODEL=stub-model \
    OPENAI_BASE_URL=http://127.0.0.1:$port/v1 \
    OPENAI_API_KEY=not-a-real-key \
    AGENT_TIMEOUT_S=$budget \
    ./run.sh 'В /root/airi/task функция add в calc.py возвращает разность вместо суммы.
Исправь причину и докажи проверкой. Кавычки: "двойные" и '"'"'одинарные'"'"'.'
code=$?
end=$(date +%s.%N)
echo "exit=$code wall=$(echo "$end - $start" | bc)s"
kill $stub 2>/dev/null
wait $stub 2>/dev/null
echo "--- fixture after ---"
cat /root/airi/task/calc.py
python3 /root/airi/task/check.py && echo "post-run check: PASS" || echo "post-run check: FAIL"
echo "--- stub log ---"
cat /root/airi/$label.stub.log
