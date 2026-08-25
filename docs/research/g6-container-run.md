# G6 — прогон собранного архива в контейнере целевого образа

Сырой протокол к записи `E003` в `docs/experiments.md`. Здесь только то, что было
выполнено и напечатано; выводы и ограничения — в карточке эксперимента.

- Дата: 2026-08-25
- Commit: `ed01732`
- Архив: `build/submission.zip`, 32 823 Б,
  `bf1ee9b995a908c3e3fd416d84227b5ce4174abb2a01197ae2ff56635dedb87f`
- Образ: `secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3`
- Хост прогона: Docker 29.6.1 (Server, overlayfs) внутри WSL2 `ozon`

## Почему архив копировался в нативную ФС

`/mnt/d` — DrvFs и не хранит POSIX-права. Архив был скопирован в `~/g6` внутри WSL и
проверен по SHA-256: `bf1ee9b9…` совпал с виндовой сборкой. Иначе любая проверка
exec-бита была бы бессодержательной; та же ловушка описана в
`docs/local-verification.md`.

## Как запускалось

```sh
IMG=secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3
docker run --rm --network none   -v /root/g6/submission.zip:/opt/submission.zip:ro   -v /root/g6/wrapped.zip:/opt/wrapped.zip:ro   -v /root/g6/in_container.sh:/opt/in_container.sh:ro   --entrypoint /bin/sh "$IMG" /opt/in_container.sh
```

`wrapped.zip` — негативный контроль: тот же архив, перепакованный под одну верхнюю
папку `submission/`, `94e5b211d219fa7b0786446e5404f5c7607cc37db214f8a82472cc8b0f3a1700`.

Эталонные fingerprint'ы считались **отдельно на Windows** через `hashlib`, а не
внутри контейнера, — иначе сверка сравнивала бы прогон сам с собой.

## Вывод прогона

```text
=== identity ===
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
Python 3.12.13 (main, Jun 24 2026, 02:09:08) [GCC 12.2.0]
uid=0(root) gid=0(root) groups=0(root)

=== extraction tool ===
/usr/bin/unzip
/app/.venv/bin/python3

=== network ===
lo 127.0.0.1/8
lo ::1/128
dns: no resolution (expected offline)

=== limits ===
nproc=8
memory.max=max
pids.max=7099
proc: 65 entries
    PID    PPID COMMAND
      1       0 /bin/sh /opt/in_container.sh
     21       1 ps -o pid,ppid,args
     22       1 head -5

=== layout A: archive-root (ours) ===
-rwxr-xr-x 1 root root 1212 Jan  1  1980 run.sh
-- contract launch, no chmod --
agent ready: workspace=/app (image workdir /app) instruction=27:347b6d93607b budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL
exit=0

=== instruction round trip ===
quotes  instruction=27:38460e507ac8
newline instruction=17:b6858b03a6ca
dash    instruction=2:d8156bae0c42
shell   instruction=39:e20e7fb70e27
unicode instruction=31:8e4ef50cfc0c

=== argument contract ===
no arg   exit=2
empty    exit=2
blank    exit=2

=== layout B: one-top-level (negative control) ===
submission
no run.sh at root, as expected
exit=127

=== run from another cwd ===
agent ready: workspace=/app (image workdir /app) instruction=27:347b6d93607b budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL
exit=0
=== container exit=0 ===
```

## Сверка fingerprint'ов

| Кейс | Эталон (Windows) | Контейнер | Совпало |
|---|---|---|---|
| обычная строка | `27:347b6d93607b` | `27:347b6d93607b` | да |
| кавычки `"` и `'` | `27:38460e507ac8` | `27:38460e507ac8` | да |
| перевод строки | `17:b6858b03a6ca` | `17:b6858b03a6ca` | да |
| ведущий `--` | `12:e5f36469c45f` | `2:d8156bae0c42`, после исправления `12:e5f36469c45f` | да, со второго прогона |
| `$HOME`, backtick, `\|`, `;`, `>` | `39:e20e7fb70e27` | `39:e20e7fb70e27` | да |
| UTF-8 (кириллица, тире) | `31:8e4ef50cfc0c` | `31:8e4ef50cfc0c` | да |

### Ошибка первого прогона, исправленная вторым

Кейс с ведущим `--` сначала был вызван как `./run.sh -- '--not-a-flag'`. `run.sh`
передаёт `"$@"` в `agent.main` без разбора, поэтому инструкцией стал сам разделитель
`--`, и fingerprint вышел `2:d8156bae0c42` вместо ожидаемого. Разделитель был моим
собственным изобретением: runner вызывает `./run.sh "<task instruction>"` одним
аргументом. Перезапуск без `--` дал `12:e5f36469c45f`.

Это стоит записать не как «опечатку в тесте», а как факт о контракте: `agent.main`
берёт `argv[1]` как есть и **не** трактует `--` как разделитель.

## Риск экстрактора

Отдельный прогон, тот же образ и тот же архив, два распаковщика:

```text
=== extractor A: unzip ===
-rwxr-xr-x run.sh
exit=0

=== extractor B: python3 -m zipfile ===
-rw-r--r-- run.sh
exit=126
-- same script via sh --
exit=0
```

`zipfile._extract_member` не восстанавливает права ни на одной платформе. Режим и
Unix host-байт в архиве корректны, `unzip` их читает, `zipfile` — игнорирует. Если
оценивающая обвязка распаковывает питоном и зовёт `./run.sh`, старт падает с `126`
на всех 15 задачах, и **со стороны архива это не лечится**: содержимое zip уже
правильное.

Что можно сделать, решать не здесь: это вопрос к организаторам о способе распаковки,
а не задача упаковщика.

## Что этот прогон не доказывает

- `nproc=8`, `memory.max=max` — лимиты хоста WSL2, а не оценочной microVM.
- Контейнер запущен с `--entrypoint /bin/sh`, без Harbor wrapper.
- `/app` в голом контейнере содержит только `.venv`; чем его наполняет реальная
  задача — не проверено.
- Решаемость: цикла нет, exit `0` означает «контракт работает», а не «задача решена».
