# Прогноз состава закрытого набора ИБ-агента и приоритетов разработки

**Статус: исследовательский прогноз, а не знание о закрытом наборе.** Документ опирается на открытые бенчмарки и на предоставленный вами разбор публичного комплекта. Он **не** выводит состав закрытых 15 задач из публичных шести и не переводит открытые частоты в вероятности или ожидаемое количество задач.

## Короткий вывод

Открытая литература даёт два устойчивых сигнала. Во-первых, в проверяемых ИБ-средах наиболее естественно автоматизируются четыре жизненных цикла: **найти уязвимость, воспроизвести/эксплуатировать, исправить, извлечь точный forensic-результат**. Во-вторых, у агентов на малых моделях значимая часть нулей возникает не только из-за незнания ИБ, но и из-за неправильной локализации, неумения пользоваться инструментами, потери контекста, повторных циклов и сдачи непроверенного артефакта. Это особенно важно при вашем бинарном `0/1` oracle.

> **Практический прогноз:** вложения прежде всего стоит направить в единый надёжный контур `preflight → компактный tool loop → verifier → один recovery pass`, затем в repair и audit/detection. Форензику следует делать Python-first и потоковой. CTF необходимо покрывать базовыми локальными сценариями, но не ставить глубокий `pwn`/ROP/reverse/crypto во главу плана до независимых измерений на целевой модели.

## 1. Открытые бенчмарки

В таблицу включены наборы, которые либо непосредственно соответствуют одному из четырёх заявленных классов, либо являются сильным аналогом формата «репозиторий/локальная среда/исполняемая проверка». **SWE-bench и CyberSOCEval не считаются частотной выборкой закрытого конкурса:** первый не является security-набором, второй преимущественно QA-оценка, а не автономный sandbox-agent.

| Бенчмарк | Ссылка | Размер и состав | Автоматическая проверка | Опубликованные результаты, релевантные малым/крупным моделям | Переносимость на ваш контур |
|---|---|---|---|---|---|
| **Cybench** | [статья и сайт][1] | 40 профессиональных CTF из 4 соревнований. Точные категории: crypto 16, web 8, reverse 6, forensics 4, misc 4, pwn 2. | Агент действует в Kali; итог — бинарный flag-match / извлечение уникального флага. Subtasks дают диагностику, но не заменяют final 0/1. | Unguided: Claude 3.5 Sonnet 17.5%, GPT-4o 12.5%, Claude 3 Opus и o1-preview по 10.0%, Llama 3.1 405B и Mixtral 8x22B по 7.5%, Llama 3 70B 5.0%. Без subtask guidance никто не решил задачу с human first-solve time свыше 11 минут. | Сильный аналог **CTF**, включая web/forensics; среда существенно богаче целевой: Kali, сеть и security tools. |
| **NYU CTF Bench** | [статья, сайт и код][2] | 200 валидированных CSAW CTF, плюс dev split 55. Crypto 52, rev 51, pwn 39, misc 24, web 19, forensics 15. | Скрытый ground-truth flag; server tasks через Docker Compose, локальные — по файлам. | Baseline: Claude 3 Haiku 4% pass@5, GPT-3.5 4% pass@5, GPT-4 5% pass@5. На leaderboard лучшие агентные результаты Claude 3.5 Sonnet: CRAKEN 22% pass@1, D-CIPHER 19% pass@1. | Сильный аналог **CTF** и частично forensic; предполагает Docker-сервисы и тяжёлые инструменты, которых в target image нет. |
| **InterCode-CTF** | [сайт и код][3] | 100 PicoCTF-задач в шести CTF-категориях; часть семейства InterCode interactive execution environments. | Контейнерный интерактивный environment; `submit` завершает эпизод, Success Rate — доля reward=1. | GPT-4 37.0%, Lemur-70B 22.0%, CodeLlama-34B 16.0%, GPT-3.5 11.0%, Llama-2-70B 9.0%. | Очень хороший аналог **многошагового terminal-loop** и строгого execution-feedback; результаты старые, но разрыв размера модели показателен. |
| **BountyBench** | [статья и сайт][4] | 25 реальных систем, 40 публичных bug bounties, 9 из OWASP Top 10; три фазы Detect, Exploit, Patch — 120 задач. | Detect/Exploit: submitted exploit должен выполнить verifier на vulnerable snapshot и не выполнить его на patched snapshot. Patch: invariants проходят, reference exploit блокируется. | До трёх попыток: лучший Detect 12.5% (Codex CLI/o3-high), Exploit 67.5% (custom Claude 3.7 Sonnet Thinking), Patch 90% (Codex CLI/o3-high и o4-mini). | Наиболее близкий открытый аналог **поиска уязвимостей, exploitation и repair**. Числа — frontier agents, не прогноз малой локальной модели. |
| **SEC-bench** | [статья и код][5] | 200 вручную проверенных CVE-инстансов memory-safety из 29 C/C++ проектов; задачи PoC generation и vulnerability patching. | PoC должен вызвать ожидаемый sanitizer report; patch применяют, собирают и проверяют, что исходный PoC больше не вызывает sanitizer. | Потолок оценённых SOTA agents: 18.0% для PoC и 34.0% для patch по полному набору. | Прямой аналог **security repair в SWE-формате**; требует Docker, C/C++ toolchain и sanitizer. |
| **CVE-Bench** | [NAACL paper][6] | 509 CVE из 120 OSS repositories на 4 языках; доступна чёрно-/бело-ящичная информация и static analysis. | Тестовая среда имитирует vulnerability repair; проверяется исправление в среде. | SWE-agent исправляет не более 21% CVE; авторы отдельно отмечают нехватку навыка использования analysis tools. | Прямой ориентир для **repair** и для того, что один лишь coding-agent без security/tool-use playbooks слаб. |
| **SWE-bench / Verified** | [оригинальный сайт и код][7] | 2,294 issue/PR из 12 Python repositories; Verified — вручную отобранный subset из 500. Не security-specific. | Fail-to-pass tests: patch должен сделать исходно падающие тесты проходящими. | Исходная RAG baseline 1.96%, ранний SWE-agent 12.47%. Современные результаты зависят от обвязки и contamination. | Аналог **формата repair и бинарного oracle**, но не доказательство частоты security-дефектов. |
| **CyberSecEval 3/4** | [статья и документация][8] | Разнородное семейство: CTF-like Canary Exploit, autonomous offensive ops, secure coding, AutoPatch, interpreter abuse, prompt injection и др. | Зависит от трека: Canary Exploit исполняет ответ на рандомизированном бинарнике; secure-code применяет detector; часть треков использует judge. | CSE3: Llama 3 405B была лучше GPT-4 Turbo на малых exploitation tasks на 23%, но авторы не увидели существенного стратегического прогресса в autonomous hacking. | Подтверждает, что **exploitation и autopatch** — самостоятельные классы, но не единый сопоставимый dataset. |
| **CTF-Dojo** | [статья][9] | 658 containerized CTF, шесть категорий; предназначен для execution-grounded agent training. | Runtime flag verification; статические и динамически генерируемые flags. | После обучения на 486 проверенных траекториях лучший Qwen3-32B достигает среднего 31.9% Pass@1 по InterCode-CTF, NYU и Cybench. | Важное исключение из тезиса «малые модели бесполезны»: **32B с релевантным execution training и правильным scaffold** может быть конкурентен на части CTF. |
| **CyberSOCEval** | [статья][10] | 609 malware-analysis QA по sandbox JSON-логам и 588 threat-intel QA по 45 отчётам. | Exact multi-select accuracy; это QA, не автономный агент. | Malware analysis около 23–34%; threat-intel reasoning около 43–53% у протестированных моделей. | Подтверждает, что **длинные forensic logs** — отдельная трудность; не включается в оценку частоты sandbox-задач. |
| **SIR-Bench** | [статья][11] | 794 incident-response cases из 129 паттернов; triage, novel findings и tool usage. | Metrics для triage и нахождения новых forensic findings; часть оценки реализуется adversarial LLM-as-judge. | У авторского SIR agent: 97.1% TP detection, 73.4% FP rejection, 5.67 novel findings/case. | Подтверждает существование отдельного **forensics/IR** класса, но способ оценки менее похож на ваш детерминированный 0/1 verifier. |

### Что нельзя делать с этой таблицей

Cybench и NYU CTF — специально CTF-центричные корпуса, поэтому их состав не является оценкой скрытых 15 задач. BountyBench искусственно раскладывает каждую bounty на Detect, Exploit и Patch; это не означает равного числа таких классов у другого организатора. Наконец, результаты моделей несопоставимы между строками без учёта version, toolchain, бюджета, количества попыток и доступа к сети.

## 2. Ранжированный прогноз типов заданий

Шкала ниже **порядковая**, чтобы не создавать фиктивную точность. «Выше» означает: есть прямое совпадение с объявленным классом и/или публичный пример, плюс аналог встречается в нескольких исполняемых наборах. «Средне» означает: класс явно объявлен, но публичный комплект и ограничения среды не дают сильной причины ожидать его большую долю. «Условно ниже» означает не невозможность, а худшее ожидаемое покрытие малой моделью при известном offline-toolchain.

| Ранг | Тип | Прогнозная встречаемость | На чём основана оценка | Что именно следует уметь |
|---:|---|---|---|---|
| 1 | **Известный security-defect: локализовать и сделать небольшой security-patch** | **Выше** | Прямое совпадение с заявленным repair-классом; в вашем public pack два SQLi-fix; в SEC-bench, CVE-Bench и BountyBench Patch это базовый исполнимый формат. [4] [5] [6] | Find/search, trace source→sink или auth boundary, минимальный diff, targeted test и regression/verifier. |
| 2 | **Поиск уязвимости / security audit с точным finding, JSON или PoC** | **Выше** | Прямое совпадение с заявленным классом; есть `find-sqli-login` в public pack; BountyBench и SEC-bench показывают Detect/PoC как самостоятельные task types. [4] [5] | Поиск опасных API и потоков данных, воспроизводимый finding/PoC, строгое оформление артефакта. |
| 3 | **Локальная цифровая форензика: корреляция логов, файлов и IOC с точным отчётом** | **Средне** | Класс назван организаторами и имеет public пример; в CTF корпусах forensics меньше crypto/rev (Cybench 4/40, NYU 15/200), но самостоятельные SIR-Bench и CyberSOCEval подтверждают предметную зрелость класса. [1] [2] [10] [11] | Потоковое чтение, JSONL/CSV filtering через Python, timeline, exact `key=value`/JSON schema и финальная проверка. |
| 4 | **CTF-lite: web/file/encoding/crypto-lite/rev-lite с локальным артефактом или flag** | **Средне** | CTF преобладают в открытой агентной литературе, но public комплект не включает настоящий CTF и target image беднее Kali. [1] [2] [3] | File triage, base/hex/архивы, безопасные Python scripts, HTTP только если сервис локален, проверка flag/formata. |
| 5 | **Глубокий CTF: pwn/ROP, динамический reverse, тяжёлая crypto-задача, multi-service exploit chain** | **Условно ниже** | Такие задачи есть в NYU/Cybench, однако именно они требуют gdb/Ghidra/Sage/сетевого стека и долгой траектории. В target image, согласно вашему аудиту, многих таких CLI нет; Cybench также показывает резкий difficulty ceiling. [1] [2] | Только облегчённые Python-first playbooks, не ставка на широкое покрытие. |
| — | **Smoke/contract artifact: файл, JSON, ZIP layout, точный ответ** | **Не отдельный класс; сквозной риск** | В public pack уже есть две файловые задачи; в вашем контракте любая ошибка/timeout даёт 0. | Deterministic write/overwrite, schema check, file existence, final diff, нулевой exit code. |

### Описательные внешние частоты, которые не переносятся напрямую

| Корпус | Наблюдаемый состав | Допустимый вывод | Недопустимый вывод |
|---|---|---|---|
| Cybench | Crypto 16/40; web 8/40; rev 6/40; forensics 4/40; misc 4/40; pwn 2/40. [1] | Профессиональный CTF-корпус широк; forensic и pwn не доминируют в этой конкретной выборке. | «В закрытом наборе будет та же пропорция». |
| NYU CTF Bench | Crypto 52/200; rev 51/200; pwn 39/200; misc 24/200; web 19/200; forensics 15/200. [2] | В CSAW-корпусе crypto/rev/pwn встречаются чаще forensic/web. | «Комиссия выберет 15 задач пропорционально CSAW». |
| BountyBench | 40 bounty × Detect/Exploit/Patch = 120 lifecycle tasks. [4] | Detection, exploit и patch обычно проектируют как связанные, но разные механики. | «В другом наборе три механики будут представлены поровну». |

## 3. Что именно проваливают агенты

### Режимы провала и опубликованная квантификация

Честный ответ по долям: **универсального опубликованного процента для большинства режимов нет**. Работы используют разные agents, модели, бюджеты и таксономии. Поэтому в колонке оставлены только действительно опубликованные количественные показатели; прочее помечено как качественное наблюдение, а не превращено в выдуманный процент.

| Режим провала | Опубликованная доля или квантификация | Источник и корректная интерпретация |
|---|---|---|
| **Ошибочная стратегия, cognitive deadlock, повторные действия** | **Большинство agentic failures** — формулировка авторов; точный процент по категории не дан в текстовой форме. | Ручной анализ 150 failed SWE-bench Verified instances: agent-based tools чаще застревают в поздних unproductive loops; авторы называют flawed reasoning/cognitive deadlocks преобладающей причиной. [12] Это сильный qualitative signal, но не глобальная частота. |
| **Неверная локализация дефекта** | Точная общая доля не опубликована в текстовой форме. | В той же работе pipeline tools чаще падают рано на localization. [12] Для security repair это обосновывает отдельный `search/trace` слой, а не надежду на один общий `grep`. |
| **Ошибка инструмента без recovery** | В 3,977 SWE-bench траекториях resolution rate: **54.61%** у попыток хотя бы с одной Python error против **54.42%** без неё. | Само наличие ошибки инструмента не объясняет ноль: сильные агенты могут восстанавливаться. Важны повторение, число ошибок, timeout и качество следующего действия. [13] |
| **Длинный output / потеря контекста** | Общая доля не опубликована. | EnIGMA показывает конкретный механизм: вывод decompiler/`strings` может заполнить context window и немедленно закончить сессию; authors добавляют summarizers. [14] Это особенно переносимо на binaries и многомегабайтные логи. |
| **Нет нужного interactive tool или не найден toolchain** | В controlled NYU study Kali + tool discovery даёт **+9.5 п.п.** к Ubuntu: 52.0% против 42.5% у Gemini 3 Pro. | Это не «доля сбоев», а причинный effect среды в одном конфиге. CTF-Dojo также описывает хрупкие exploitation strategies и failure to discover the right toolchain у слабых моделей. [9] [15] |
| **Неверный формат, синтаксис, tool use и context management у меньшей модели** | Точная доля не опубликована. | SWE-Bench Pro качественно сообщает, что Qwen 3 32B чаще проваливается именно по syntax/formatting, tool use и context management; крупные модели чаще теряют семантическую/алгоритмическую корректность multi-file patch. [16] |
| **Преждевременное завершение без проверки final artifact** | Общего benchmark-процента не найдено. | Это непосредственный риск **вашего** 0/1 контракта: public pack содержит exact-file/JSON/patch cases, а любое неверное представление даёт 0. Его следует закрыть обязательным verifier, но не маркировать как статистически «самый частый» внешний failure mode. |

### Малые модели 7B–32B против крупных: что можно утверждать

Нельзя честно построить одну шкалу для «7B–32B»: в открытых работах смешаны старые и новые модели, MoE, разный agent scaffold, разные environments и budgets. Однако направление разрыва устойчиво. На InterCode-CTF старый CodeLlama-34B имеет 16.0% против 37.0% GPT-4, а Llama-2-70B — 9.0%. На старом Cybench Llama 3 70B — 5.0%, Mixtral 8x22B — 7.5%, тогда как лучший тогда Claude 3.5 Sonnet — 17.5%. [1] [3] Даже Llama 3.1 405B достигает только 7.0% на NYU и 10.0% на Cybench в EnIGMA, что подчёркивает сложность interaction, а не только размер модели. [14]

Для 7B–32B **практически недостижимы без специальной подготовки и удачного задания** следующие подтипы: open-ended zero-day discovery в большом коде, глубокий pwn/ROP, многошаговый dynamic reverse, crypto с нетривиальной математикой, а также C/C++ sanitizer-PoC и multi-file security repair на крупном репозитории. Это не означает, что задача из такого класса всегда нулевая; это означает, что их нельзя делать базовой ставкой, когда даже frontier agents показывают низкие Detect/PoC результаты: 12.5% и 18.0% соответственно. [4] [5]

При правильной обвязке малые модели имеют реалистичный шанс на **ограниченный, но ценный** набор задач: точный файловый артефакт, известный validation/auth/query/path defect, короткий patch с проверяемым тестом, forensic filtering и корреляция локальных логов, а также CTF-lite с подходящим Python toolchain. CTF-Dojo показывает, что execution-grounded fine-tuning и проверенные траектории существенно меняют картину: лучший дообученный Qwen3-32B достиг среднего 31.9% Pass@1 по трём CTF бенчмаркам. [9] Это не повод ожидать того же результата у вашей модели; это доказательство, что **scaffold, интерфейс и обучающие траектории** могут быть не менее важны, чем номинальный размер.

## 4. Рекомендация по распределению усилий

Цель — не симметричное покрытие четырёх ярлыков, а максимум бинарно принятых задач. Поэтому рекомендую следующую последовательность работ.

1. **Сначала создать общий deterministic reliability layer.** Он должен находить workspace и instruction, выделять deliverables и формат, выдавать малой модели компактный tree/search summary, ограничивать step/time/output, распознавать повторяющееся действие и резервировать время на проверку. Обязательный маршрут завершения: `artifact/schema → syntax → targeted test → regression/health → final existence/diff`. Это закрывает cross-class нули и прямо соответствует наблюдениям о loops, context и tool-use. [12] [13] [14]

2. **Главный предметный playbook — security repair.** Для SQLi, auth/authorization, validation, path handling, deserialization и unsafe API нужны source-to-sink/guard tracing, минимальный diff, тест до и после, а также запрет на финализацию без выполнения доступного verifier. Это наиболее обоснованная ставка: repair явно объявлен, представлен двумя public SQLi cases и массово встречается в SEC-bench/CVE-Bench/BountyBench. [4] [5] [6]

3. **Второй playbook — vulnerability audit/detection с контрактным выводом.** Здесь нужен не «ещё один специалист», а набор deterministic search patterns, context slices и templates для finding/JSON/PoC. Для closed binary scoring хороший текст без точной схемы равен нулю; поэтому report serializer и self-check столь же важны, как reasoning.

4. **Третий playbook — Python-first forensics.** Нужны `file_info`, bounded `read_range`, regex/JSONL filtering, сортировка по времени, простая IOC/timeline корреляция и exact-output writer. Нельзя давать модели `cat` больших доказательств и ждать, что она сама заметит нужную строку. Для ограниченного образа Python standard library надёжнее ставки на отсутствующие DFIR CLI.

5. **CTF покрывать каскадом, а не глубоким ансамблем.** Реализуйте дешёвые локальные ветки для files/archives/encoding, web-local, crypto-lite и rev-lite; только затем добавляйте pwn/reverse/crypto heavy, если независимый hold-out показывает реальную отдачу. EnIGMA и controlled NYU study поддерживают улучшение tools и concise interfaces, но не подтверждают, что сложный multi-agent router окупается при малом общем budget. [14] [15]

## Ограничения прогноза

Ваш закрытый набор может намеренно контрбалансировать public cases. Неизвестны модель, token/time budgets, доступные скрытым задачам зависимости и точный output contract. Кроме того, открытые CTF-наборы часто используют сеть, Docker-сервисы, Kali и специализированные бинарные инструменты, тогда как ваш контур offline и минимален. Поэтому прогноз надо обновить после минимального независимого набора мутаций: несколько repair, несколько audit, большой JSONL forensic, CTF-lite и хотя бы одна задача с заведомо неудобным форматом сдачи.

## References

[1]: https://arxiv.org/html/2408.08926v2 "Cybench: A Framework for Evaluating Cybersecurity Capabilities and Risks of Language Models"
[2]: https://arxiv.org/html/2406.05590v3 "NYU CTF Bench: A Scalable Open-Source Benchmark Dataset for Evaluating LLMs in Offensive Security"
[3]: https://intercode-benchmark.github.io/ "InterCode: Standardizing and Benchmarking Interactive Coding with Execution Feedback"
[4]: https://arxiv.org/html/2505.15216v3 "BountyBench: Dollar Impact of AI Agent Attackers and Defenders on Real-World Cybersecurity Systems"
[5]: https://arxiv.org/html/2506.11791 "SEC-bench: Automated Benchmarking of LLM Agents on Real-World Software Security Tasks"
[6]: https://aclanthology.org/2025.naacl-long.212/ "CVE-Bench: Benchmarking LLM-based Software Engineering Agent’s Ability to Repair Real-World CVE Vulnerabilities"
[7]: https://www.swebench.com/original.html "SWE-bench: Can Language Models Resolve Real-world GitHub Issues?"
[8]: https://arxiv.org/html/2408.01605v2 "CyberSecEval 3: Advancing the Evaluation of Cybersecurity Risks and Capabilities in Large Language Models"
[9]: https://arxiv.org/html/2508.18370v2 "Training Language Model Agents to Find Vulnerabilities with CTF-Dojo"
[10]: https://arxiv.org/html/2509.20166v2 "CyberSOCEval: Benchmarking LLMs Capabilities for Malware Analysis and Threat Intelligence Reasoning"
[11]: https://arxiv.org/html/2604.12040v1 "SIR-Bench: Evaluating Investigation Depth in Security Incident Response Agents"
[12]: https://arxiv.org/html/2509.13941v1 "An Empirical Study on Failures in Automated Issue Solving"
[13]: https://arxiv.org/html/2503.12374 "Beyond Final Code: A Process-Oriented Error Analysis of Software Development Agents in Real-World GitHub Scenarios"
[14]: https://arxiv.org/html/2409.16165 "EnIGMA: Interactive Tools Substantially Assist LM Agents in Finding Security Vulnerabilities"
[15]: https://arxiv.org/html/2604.17159v1 "Systematic Capability Benchmarking of Frontier Large Language Models for Offensive Cyber Tasks"
[16]: https://arxiv.org/html/2509.16941v1 "SWE-Bench Pro: Can AI Agents Solve Long-Horizon Software Engineering Tasks?"
