# live_run — прогон настоящего `run.sh` против отвечающего endpoint

Средство воспроизведения эксперимента E005. Не импортируется из `agent/` и не
попадает в submission: `scripts/build_submission.py` кладёт в архив только
`run.sh`, `THIRD_PARTY_NOTICES.md` и дерево `agent/`.

Зачем нужен. До E005 петля ни разу не доходила до конца при отвечающем endpoint:
в E004 endpoint был недостижим, а всё остальное проверялось юнит-тестами с
подменённым транспортом. Заглушка даёт настоящий HTTP-сокет, поэтому на стороне
агента исполняется продакшн-путь целиком — сокеты, таймауты, повторы, лестница
форматов запроса, инструменты и верификатор.

**Заглушка не является моделью.** Её ответы записаны заранее. Прогон доказывает,
что механика работает, и ничего не говорит о том, справится ли настоящая малая
модель. Решаемость измеряется только против реального endpoint.

## Запуск

Нужен Linux (`/bin/sh`, права из zip). На этой машине использовался WSL2.

```sh
python scripts/build_submission.py
mkdir -p /root/airi && cp build/submission.zip /root/airi/
cd /root/airi && mkdir unpacked && unzip -q submission.zip -d unpacked
cp <repo>/scripts/live_run/*.sh <repo>/scripts/live_run/stub_endpoint.py /root/airi/
sh /root/airi/run_case.sh script 8731 120 caseA   # полный путь до verified
sh /root/airi/run_case.sh shape  8732 120 caseC   # endpoint отвергает native tools
sh /root/airi/run_case.sh hang   8733 600 caseB   # endpoint не отвечает никогда
sh /root/airi/run_case.sh escape 8734 120 caseD   # попытка выйти из workspace
sh /root/airi/run_case.sh flat   8735 120 caseE   # плоский tool call
sh /root/airi/run_case.sh degraded 8736 120 caseF # 500 ×2 и ответ без нагрузки
```

Режим `flat` отвечает в плоской форме — `finish_reason: "tool"` и
`tool_calls[].{name, arguments}` без обёртки `function`. Он нужен, чтобы проверять
`_read_tool_call` на боевом пути, а не только юнит-тестом: без поддержки этой формы
клиент не падает, а молча уходит с native tools на `json_schema` и остаётся там до
конца задачи.

Эту форму документирует llama.cpp, но сервер, собранный на том самом коммите,
отдаёт каноническую обёртку `function` (E010). То есть режим проверяет стойкость к
**неизвестному** endpoint, а не совместимость с llama.cpp.

Режим `degraded` повторяет форму отказа, давшую реальный ноль в `REVIEW-public-q36`:
два HTTP 500 подряд (после второго клиент спускается на `json_schema`), затем
`write_file` с путём и **без ключа `content`** — ответ, потерявший нагрузку. Проверяемое
свойство — не «задача решена», а «такой ответ не обнулил артефакт»: `run_repeats.sh`
сравнивает md5 файла до и после и требует, чтобы оба отказа и испорченное действие
действительно дошли до агента. Прогон различает состояния по одному фактору: с
`201c164` файл цел, без него обнуляется.

`run_case.sh` жёстко привязан к `/root/airi` — это каталог прогона, а не часть
контракта. Он вызывает `run.sh` из распакованного архива с `cwd`, равным каталогу
агента, как это делает Harbor, и передаёт задачу через `LOCAL_AGENT_WORKDIR`.

Результаты и границы — `docs/experiments.md`, карточка E005.
