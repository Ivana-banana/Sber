# Аудит решаемости с приоритетом тестов организаторов — 07.09.2026

Проверяемый код: `2d9f0dbb84d6c85a4a9a7c4965efe165c735ad84`.
Это анализ сохранённых прогонов и локальные диагностические replay, **не новый
прогон LLM**. `agent/`, инструкции задач и их проверки не изменялись.
План: [organizer-first improvements](../superpowers/plans/2026-09-07-organizer-first-improvements.md).

## База доказательств

| Набор и архив | Результат | Что действительно измерено |
|---|---|---|
| E048/E049, `build/e039-anchored-history.zip` | 2/6 дважды | Шесть пакетов организаторов; проходят только `hello-file`, `bye-file`; содержательные 0/4 |
| E060, `build/e060-artifact-guard.zip` | 5/18 | Наш дополнительный набор; 408 103 endpoint tokens, 289 calls, 2144.0 s |
| E061, `build/e061-recon-packet.zip` | 5/18 | Локальная законченная таблица на 18 задач; 359 096 tokens, 261 calls, 1931.4 s; те же пять побед |

E061 найден в `build/`, его отдельной итоговой карточки на исходном HEAD нет.
Здесь только пересчёт raw TSV: токены −12.0%, calls −9.7%, время −9.9% относительно
E060. Рост решаемости не показан. В его CTF-трассах сохраняется попытка запуска
сервера (`ctf-duplicate-parameter`: `python3 -m http.server 9139`, затем
`python3 /app/server.py`); `ctf-signed-cookie` переписывает исходник сервера.
Полезность recon как стратегии получения результата не подтверждена.

E060/E061 не проходили official public set в найденных результатах. Сравнивать
их 5/18 с E049 2/6 как рост нельзя: и задачи, и архивы различаются.

SHA-256:

```text
E049-results.tsv aead14d984bb90829bb8825e35d9dfcb2a0ce557d4746e16a57dd57d1fcf9907
E060-results.tsv f725efdcaa79ffa9c8d63a1394b5c879abe3991081b180f569dba635655dd6c3
build/eval-E061-recon-packet/results.tsv 77640a3865364a20cc6bfba13752861c7e3c378896b71d70be41c11cc9d12d31
e039-anchored-history.zip 36c6cb4d34733d0dc538a0e19e05d03daf76ff146216defc6ffee3305e0824a7
e060-artifact-guard.zip 7fe71734913508b31ef40d02e5c5ed472ef593ad374db1e275b67432fecc6dfa
e061-recon-packet.zip 21a43317d6ed9e78473e7334b6a9ed04e21d0dee1cf80af6ed3e1a782d91bf99
```

## Что сломалось на official public

Источник: `build/eval-E049-public-repeat/logs/*.trace` и `*.post`.
Подсчёт ниже относится к ответам модели, включая потенциально отклонённые действия,
а не утверждает, что каждый запрос инструмента был выполнен.

| Задача | Наблюдение | Требование настоящего checker | Рабочая классификация нуля |
|---|---|---|---|
| `find-sqli-login` | 19 запросов `read_range`, только `main.py`; нет `search`, `run_shell`, `finish`; итог `{"findings": []}` | JSON object + непустой findings + описание SQLi в auth с конкретным свидетельством | Не дошёл до уязвимого кода; пустое содержание |
| `fix-sqli-login` | 22 чтения `main.py`/`AGENTS.md`; четыре записи `main.py`; нет `run_shell`, `finish` | Проверки auth bypass и сохранения нормального поведения всего сервиса | Не локализовал дефект, не запустил функциональную проверку |
| `fix-sqli-search` | 23 чтения тех же файлов; единственный shell — `ps -ef \| grep uvicorn`; четыре записи `main.py`; нет `finish` | UNION не раскрывает users, нормальный search и остальные endpoints работают | Не локализовал дефект, не запустил функциональную проверку |
| `incident-log-forensics` | 18 запросов чтения, только первичный app log и collector note, один без path; нет чтений recovered/proxy/edge, shell и finish; семь записей отчёта | Ровно четыре поля с точными значениями, timestamp дословно | Неполный сбор доказательств и неподтверждённые значения |

Текущий router выбирает этим инструкциям соответственно `code-audit`, `code-fix`,
`code-fix`, `forensics`. Исправление классификатора пока не имеет основания.
Прохождение его теста не доказывает достаточность самих playbook.

`local_task/fix-sqli-*/tests/test.sh` перезапускают сервис с заново созданной БД и
запускают `/tests`. Значит, награда fix — поведение исправленного приложения,
а не наличие изменённого файла. Агенту доступен собственный `/app/tests`; скрытый
`/tests` служит разработчикам внешним oracle и не должен становиться инструментом
агента или источником эталонных ответов.

Форензика требует сопоставления recovered JSONL, всех edge shards и proxy request id,
различения logical/transport bytes и сохранения исходного timestamp. Организаторский
`scripts/check_parity.py` воспроизводит эталон из данных: запуск завершился exit 0.
Это свидетельство согласованности fixture, не прохождение задачи нашим агентом;
копировать его фиксированные даты/правила выбора/ответы в runtime запрещено.

## Поправки к интерпретации E060

1. `classify_failure()` в `scripts/eval/run_suite.py` относит любой ноль со
   `stop=steps/budget/stuck/blocked` к `reasoning`. Число 11/13 не является ручной
   диагностикой содержимого и не исключает ошибки tools, формата или верификации.
2. `claim=False` не означает отсутствия попыток `finish`: в E060 встречаются
   отклонённые/красные finish до последующего stuck. Напротив, в четырёх официальных
   трассах E049 finish вообще нет. Нужны отдельные счётчики attempted/refused/checked.
3. `verify=0.001` обычно происходит в `_finalise(command="")`: проверяются
   наличие/непустота файла. Это не время запуска внешнего official checker.
   Само по себе малое время не доказывает слабую проверку: сравнение короткого файла
   тоже может быть быстрым. Здесь слабость установлена по коду.
4. Цена дополнительного шага — новый LLM plan, часто секунды. Почти бесплатное
   исполнение shell не означает бесплатность дополнительных итераций.
5. Обёртка `{"findings": [...]}` не спасает E060 `audit-path-normalization`:
   replay с единственной такой правкой всё ещё даёт 0. Checker также требует
   точные location функций и `requested_name` в evidence; их в ответе нет.
6. `forensics-webshell-chain` имеет корректные четыре ключа; ошибка —
   `attacker_ip=unknown`. Это пробел доказательств, не ошибка сериализации.
7. `mut-audit-query-gate` в E060 и E061 уже решена. R3 защищает от ранее наблюдавшейся
   регрессии, но не обещает новую победу в текущем наборе.

## Новые направления из кода

**Потеря связи действия и наблюдения.** `loop.run()` отправляет модели
`[step N] read_range -> ok` и голое содержимое. Нормализованные path/start_line,
команда shell и прошлый assistant action не сохраняются в messages.
`tools.read_range()` не добавляет путь, номера строк и EOF для короткого файла.
Внутренний `ActionRecord` хранит хеши, доступные детекторам, но не модели.
Локальная проба подтвердила отсутствие пути и роли assistant в таком сообщении.
Это конкретный дефект наблюдаемости для модели; его вклад в 0/4 пока **гипотеза**.
Первый дешёвый фактор — добавить компактный адрес источника и параметры действия
к существующему observation, сохранив protocol/history budget.

**Нет устойчивой карты уже изученного.** После `history_base()` остаются последние
наблюдения, но нет отдельного перечня прочитанных источников, незакрытых веток и
свидетельств выбранного ответа. Это другой вопрос, чем опровергнутая серверная
обрезка окна 4096. Проверять память отдельно от размеров окна и префиксного кеша.

**Несоответствие file-only finish задачам исправления.** `_deliverable()` требует
один существующий файл, `_finalise()` берёт последний `state.artifacts`, а список
пополняет только `write_file`. Правка через shell и изменение нескольких модулей
не представлены полноценно; непустой файл не доказывает исправление поведения.
Это обнаруженная граница контракта, а не доказанная причина отсутствия finish.

**Недостаточный сигнал для recovery.** `verify()` возвращает только exit code,
не короткое failed assertion/traceback. После красной проверки модель получает
мало информации о причине. Чёрный список shell-команд не может в общем случае
доказать смысл произвольной проверки. Нужны разные уровни: exists, format,
behavior/evidence; восстановление по конкретной ошибке, а не по слову FAIL.

## Воспроизводимые локальные пробы

Сохранённый сырой вывод двух replay и parity:
`docs/research/live-runs/A20260907-diagnostic-summary.txt`, SHA-256
`a568a0ef52ef596cb01d2e148fe5156d9200bbd80860bfad7854f5a59644a8b0`.

Команды запускались на указанном HEAD. Следующий блок не меняет исходники,
исполняет replay двух сохранённых проверок в отдельном временном каталоге.
Он рассчитан на сохранённые локальные трассы из `build/`.

```sh
python3 -m pytest -q
# 387 passed in 19.31s
python3 local_task/incident-log-forensics/scripts/check_parity.py
# exit 0
python3 - <<'PY'
import json, re, tempfile
from pathlib import Path
from agent.state import AgentState, Budget
from agent.verify import verify
from agent.loop import _cannot_fail

def actions(path):
    result = []
    for chunk in re.split(r'^--- call \d+ ---\n', path.read_text(), flags=re.M)[1:]:
        text = chunk.strip()
        if text.startswith('```'):
            text = '\n'.join(text.splitlines()[1:-1])
        action = json.loads(text)
        result.append(action)
    return result

with tempfile.TemporaryDirectory(prefix='sber-audit-') as directory:
    root = Path(directory)
    for task in ('fix-redirect-scheme', 'forensics-ssh-bruteforce'):
        trace = Path(f'build/eval-E060-artifact-guard/logs/{task}.r1.trace')
        rows = actions(trace)
        write = [a for a in rows if a.get('tool') == 'write_file'][-1]
        finish = [a for a in rows if a.get('tool') == 'finish'][-1]
        path = root / Path(write['path']).name
        path.write_text(write['content'])
        command = finish['verify'].replace('/app/', directory + '/')
        state = AgentState(instruction='diagnostic replay', workspace=root,
                           budget=Budget(600))
        state.artifacts.append(path)
        result = verify(state, deliverable=path, command=command)
        assert not _cannot_fail(command, path)
        assert result.ok and result.stage == 'command'
        if task == 'fix-redirect-scheme':
            scope = {}
            exec(compile(path.read_text(), str(path), 'exec'), scope)
            alien = scope['ALLOWED_ORIGIN'] + '.evil.invalid/path'
            assert scope['safe_redirect'](alien) != scope['DEFAULT_URL']
        else:
            assert '=unknown' in path.read_text()
        print(task, 'false positive reproduced')
PY
```

Обе проверки приняты текущим guard и `verify(ok=True, stage=command)`, несмотря на
неверный результат. Redirect выполняет `print`, SSH проверяет наличие ключей.
Отдельно исполнены функции официального audit-checker над сохранённым отчётом,
с заменой только `REPORT_PATH` на временный файл: existence PASS, два содержательных
теста FAIL. Это replay настоящего checker, без изменения его assertions.

Сырые модельные прогоны в этой сессии не запускались: задача — анализ и план.
При проверке Docker контейнеры `sber-model`/`sber-proxy` не работали; их запуск
и новый public baseline — первый исполнимый шаг плана.
