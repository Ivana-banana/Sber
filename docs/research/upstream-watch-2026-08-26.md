# Аудит публичных условий и комплекта соревнования

**Дата проверки:** 26.08.2026, GMT+3.  
**База сравнения:** `71f90810d7a608677bbcf44f409f0aefded38340` от 09.07.2026.  
**Нормализация для сверки:** приложенный `task.md`, датированный 24.08.2026.

## Вердикт

**Изменилось: публичный комплект получил техническое изменение локального verifier для `incident-log-forensics` и правила нормализации концов строк; при этом контракт финальной сдачи, набор task packages, `agent/`, `sample_submission.zip` и публично заявленные runtime-требования не изменились.** [1] [2] [3]

## Подтверждённые изменения

| Что изменилось | Дата | Ссылка | Влияние на нас | Срочность |
|---|---:|---|---|---|
| На `main` после базового SHA появился один merge-коммит `b95c62f`; он применяет изменения PR №4. В публичной ветке PR также остаётся исходный commit `163b365`, но функционально это тот же набор изменений до merge. | 26.08.2026; author date 12:28:02 UTC, merge в `main` в 15:41:24 GMT+3 | [Commit `b95c62f`][2]; [PR №4][4]; [compare][3] | Это единственное изменение default branch после исходной фиксации. | Низкая |
| Добавлен `.gitattributes`: `*.sh text eol=lf` и `*.txt text eol=lf`. | 26.08.2026 | [Commit][2] | Исходники public-kit теперь нормализуются в LF. Для submission это не добавляет нового условия, но следует поставлять `run.sh` и текстовые артефакты в LF, чтобы локальная разработка совпадала с public-kit. | Низкая |
| В `local_task/incident-log-forensics/tests/test.sh` verifier перед сравнением удаляет завершающий `\r` каждой строки и сортирует в `LC_ALL=C`. Ранее он сортировал файлы без CRLF-нормализации. | 26.08.2026 | [Commit][2]; [PR №4][4] | Единственный публичный критерий проверки, который стал более допускающим: файл `/app/incident_report.txt` с CRLF теперь проходит при прочем совпадении содержимого. Это относится только к **public local task** и не является доказательством изменения закрытого evaluator. | Низкая |
| В `README.md` добавлена запись в разделе `Updates`, описывающая LF и CRLF-tolerance. Текст о `run.sh`, Harbor wrapper, offline microVM и доступных переменных окружения не изменён. | 26.08.2026 | [Commit][2]; [README на HEAD][5] | Контракт сдачи не меняется; README лишь документирует два технических изменения выше. | Низкая |

> Публичный README прямо определяет `local_task/` как набор для локальной разработки и валидации, тогда как официальный scoring проводится на закрытом наборе задач. Поэтому изменение локального verifier нельзя экстраполировать на hidden tasks. [5]

## Контроль неизменившихся частей комплекта

| Объект проверки | Результат на HEAD `b95c62f` | Вывод для контракта сдачи |
|---|---|---|
| `local_task/` и task packages | Список каталогов с `task.toml` неизменен: `bye-file`, `hello-file`, `find-sqli-login`, `fix-sqli-login`, `fix-sqli-search`, `incident-log-forensics`. Новых и удалённых packages нет. [3] | В приложении корректно зафиксированы шесть public task packages. |
| `agent/` | В диапазоне от базового SHA до HEAD изменений нет. [3] | Нет новой версии Harbor wrapper, entrypoint или example-agent, меняющей сдачу. |
| `sample_submission.zip` | Не менялся; SHA-256 до и после: `72c9b57f1627cfb72255be6a90311bc6908b83e1c36869c67edbb6146a1e0522`. Внутри по-прежнему есть верхний каталог `sample_submission/`, в котором лежит `run.sh`. [3] [5] | Зафиксированное в приложении противоречие примера и текста README **сохраняется**; организаторы его публично не разъяснили. |
| README: submission/runtime | В diff изменена только запись Updates. Текущая README по-прежнему указывает minimum `run.sh`, заменяемый `agent.py`, запуск `./run.sh "<task instruction>"`, microVM без интернета и передачу `LOCAL_AGENT_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`. [2] [5] | Публичный контракт сдачи не ужесточён и не смягчён. |
| Branches, releases | В публичном репозитории четыре ветки; из них после baseline новая функциональная ветка — `fix/efl`/PR №4. Releases отсутствуют. [6] [7] | Отдельного release-канала с изменением условий нет. |

## Issues, pull requests и discussions

Официальная страница Issues содержит **0 открытых и 0 закрытых issues**. Следовательно, опубликованных вопросов или ответов организаторов — в том числе о лимитах, layout архива или критериях проверки — нет. [8]

На странице pull requests размещены четыре merged PR и нет открытых. Проверка их метаданных, conversation, issue-comments и reviews показывает: у каждого из PR №1–№4 пустое описание и ноль комментариев и reviews. PR №4, единственный после baseline, не содержит ни объяснения изменения, ни обсуждения условий. [4] [9]

GitHub Discussions для репозитория не включены: URL `/discussions` возвращает 404, а GitHub API фиксирует `has_discussions=false`. [10] [6]

| Канал | Результат | Ответы по лимитам, архиву или проверке |
|---|---|---|
| GitHub Issues | 0 open / 0 closed | Отсутствуют. [8] |
| Pull requests | 4 merged / 0 open; comments и reviews отсутствуют | Отсутствуют. [9] |
| GitHub Discussions | Не включены | Отсутствуют. [10] [6] |
| GitHub Releases | Отсутствуют | Отсутствуют. [7] |

## Правила, FAQ, объявления и календарь

В доступной публичной поверхности первоисточником условий остаётся README единственного публичного репозитория организации. Профиль `SecureIntelligent` показывает один public repository и не содержит ссылки на сайт соревнования, FAQ, новости или календарь. [11] В README присутствуют ссылки на MITRE ATT&CK, OWASP и Docker Hub, но нет отдельной официальной страницы правил, FAQ либо контрольных точек. [5]

Поиск по названию соревнования и датам из приложенного календаря не дал отдельной официальной страницы, которую можно связать с организаторами. Это означает не «календарь неизменен», а более узкий вывод: **публичного первоисточника для подтверждения или опровержения изменения календаря обнаружить не удалось**. Публичный GitHub не фиксирует изменения календаря и оформления после baseline.

## Образ `secureintelligent/acp`

Docker Hub сейчас публикует ровно два тега: `latest` и `harbor-0.16.1`. Оба указывают на один multi-architecture manifest list `sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3`; digest образа `linux/amd64` — `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132`. [12] [13]

| Тег | Последний push (UTC) | Linux/amd64 image digest | Manifest-list digest | Вывод |
|---|---:|---|---|---|
| `latest` | 02.07.2026 14:57:25 | `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132` | `sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3` | Не менялся после 09.07.2026 и 24.08.2026: дата последнего push раньше обеих контрольных дат. |
| `harbor-0.16.1` | 02.07.2026 15:00:01 | `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132` | `sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3` | Не менялся после 09.07.2026 и 24.08.2026: дата последнего push раньше обеих контрольных дат. |

В приложенном файле сказано, что digest был «зафиксирован» 25.08.2026, но само значение там не приведено. Поэтому буквальное сопоставление с внутренней фиксацией 25.08 выполнить нельзя. Впрочем, timestamps последнего push дают сильное первичное подтверждение: после этой даты и после baseline Docker Hub не перепривязывал ни один из опубликованных тегов. [13]

## Расхождения с приложенным `task.md`

### Существенное новое расхождение

1. **Не отражено изменение public verifier `incident-log-forensics` от 26.08.2026.** Приложение нормализовано 24.08.2026 и, естественно, не содержит правила о CRLF-tolerance. Текущий public verifier удаляет конечный `\r` из строк обоих сравниваемых файлов. Это изменение локальной проверки, а не условий hidden evaluation. [2] [4]

### Неполнота, а не противоречие

2. **Digest указан как «зафиксированный», но его значение отсутствует.** Для воспроизводимой сверки в рабочий документ нужно добавить текущие digest из таблицы выше; иначе нельзя доказуемо сопоставить состояние от 25.08 с новым наблюдением. [12] [13]

3. **Статус layout архива остаётся нерешённым.** В приложении правильно отмечено расхождение: README требует `run.sh`, а неизменившийся sample ZIP по-прежнему содержит его в `sample_submission/run.sh`. Публичных organizer answers не появилось: issues пусты, comments/reviews во всех PR отсутствуют, Discussions отключены. Поэтому это не исправленное условие и не повод менять выбранную безопасную стратегию без smoke-test/подтверждения организаторов. [5] [8] [9] [10]

4. **Календарь КТ и численные лимиты не получили публичной верификации.** Это не опровергает приложенный календарь или указание «конкретные значения не приведены», но GitHub/Docker Hub не дают независимого первоисточника для подтверждения актуальности календаря, лимитов закрытого набора и модели. В отчёте это следует продолжать маркировать как неподтверждённое публичной страницей, а не как неизменившийся факт. [5] [6] [11]

### Подтверждено без расхождений

Остальные проверяемые положения приложения соответствуют HEAD: шесть public packages, отсутствие `task.toml` у `insecure-api-app` как отдельной package, неизменность `agent/` и `sample_submission.zip`, offline microVM, отсутствие runtime-install, три передаваемые переменные LLM и изложенный README запуск через `run.sh`. [3] [5]

## Непроверенные слухи

Непроверенных слухов, которые можно было бы отделить от первоисточников, в ходе проверки не найдено. В этот отчёт включены только GitHub и Docker Hub; результаты стороннего поиска не использовались как фактическое основание.

## Рекомендованное действие

Срочных изменений submission не требуется. Следует внести в внутренний справочник commit `b95c62f`, пометить `incident-log-forensics` как CRLF-tolerant только для local public verifier и добавить точные digests образа. Вопрос корневого layout ZIP, скрытых лимитов, модели и календаря остаётся открытым: публичных разъяснений организаторов нет.

## Источники

[1]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/commit/71f90810d7a608677bbcf44f409f0aefded38340 "Baseline commit 71f90810"
[2]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/commit/b95c62fec81656e338af54045efa688fd4979615 "Current HEAD commit b95c62f"
[3]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/compare/71f90810d7a608677bbcf44f409f0aefded38340...b95c62fec81656e338af54045efa688fd4979615 "Comparison from baseline to HEAD"
[4]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/pull/4 "Pull request 4"
[5]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/blob/b95c62fec81656e338af54045efa688fd4979615/README.md "README at HEAD"
[6]: https://api.github.com/repos/SecureIntelligent/UniversalAgenticCompetitionPublic "Repository metadata API"
[7]: https://api.github.com/repos/SecureIntelligent/UniversalAgenticCompetitionPublic/releases?per_page=100 "Releases API"
[8]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/issues?q=is%3Aissue "Repository issues"
[9]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/pulls?q=is%3Apr "Repository pull requests"
[10]: https://github.com/SecureIntelligent/UniversalAgenticCompetitionPublic/discussions "Repository Discussions URL"
[11]: https://github.com/SecureIntelligent "SecureIntelligent organisation profile"
[12]: https://hub.docker.com/r/secureintelligent/acp/tags "Docker Hub tags"
[13]: https://hub.docker.com/v2/namespaces/secureintelligent/repositories/acp/tags?page_size=100 "Docker Hub tags API"
