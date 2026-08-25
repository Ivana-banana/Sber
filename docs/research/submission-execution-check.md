Стартует.

# Исполнительная проверка `submission.zip` в `secureintelligent/acp` (linux/amd64)

Проверка выполнена фактическим запуском в восстановленной файловой системе manifest `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132`. Каждый агентский прогон запускался от `uid=0` через `chroot` в отдельном network namespace (`unshare --net`); сеть в этом namespace отсутствовала. Архив развернут в `/opt/harbor/local-agent-root`, рабочий каталог процесса до `./run.sh` — этот каталог.

> Вердикт относится к **старту entrypoint**: все 15 непустых вариантов запуска завершились кодом `0`; пустой и отсутствующий аргументы контролируемо завершились кодом `2`. Внутреннего цикла решения задачи агент в этих прогонах не наблюдалось: stdout ограничился его статусной строкой.

## Восстановление и проверка образа

| Проверка | Фактический результат |
|---|---|
| Запрошенный manifest digest | `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132` |
| SHA-256 скачанного manifest body | `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132` — совпадает |
| Docker-Content-Digest из ответа Registry API | `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132` — совпадает |
| Config digest | `sha256:301b78fe9962517ba85f8ff102760393f284ff7f7a150a7be235736a1eca7647` — совпадает с descriptor |
| Платформа из config | `linux/amd64` |
| Итоговый размер восстановленного rootfs | `1944379708` байт |
| Базовый smoke-test chroot | `uid=0`, `Python 3.12.13`, `/bin/sh` завершился `0` |

### Сверка слоёв

| № | Digest из manifest | Размер по manifest | Фактический размер | Фактический SHA-256 | Совпало |
|---:|---|---:|---:|---|---|
| 1 | `sha256:68629629b516c3cd6f5e71ffbe18e32afb1ae5b4926c92d058c0f11ef1fd58a3` | 28237639 | 28237639 | `sha256:68629629b516c3cd6f5e71ffbe18e32afb1ae5b4926c92d058c0f11ef1fd58a3` | yes |
| 2 | `sha256:03952ff66d70d39109eda6ac3d7b42f9e2cbce89debbfdc3cc784ead9d9e39a3` | 3520782 | 3520782 | `sha256:03952ff66d70d39109eda6ac3d7b42f9e2cbce89debbfdc3cc784ead9d9e39a3` | yes |
| 3 | `sha256:524401bfa9eb171bc5a9ead7585085969746b64cc95623aed9b32072978d931e` | 13666970 | 13666970 | `sha256:524401bfa9eb171bc5a9ead7585085969746b64cc95623aed9b32072978d931e` | yes |
| 4 | `sha256:a056f49ed4fbbe340a316423216b7ea38b1ea463b9cbeb08b392ff69957fe03e` | 250 | 250 | `sha256:a056f49ed4fbbe340a316423216b7ea38b1ea463b9cbeb08b392ff69957fe03e` | yes |
| 5 | `sha256:cafd42ecb7445cb4047a83d585408fe5082e2e2c6b43cbbe94a79a679ec005a0` | 93 | 93 | `sha256:cafd42ecb7445cb4047a83d585408fe5082e2e2c6b43cbbe94a79a679ec005a0` | yes |
| 6 | `sha256:9ccfeca6d68be5e8919551a2cfa833596c182baa1bf0ed9e68d96763ecc5e861` | 162608749 | 162608749 | `sha256:9ccfeca6d68be5e8919551a2cfa833596c182baa1bf0ed9e68d96763ecc5e861` | yes |
| 7 | `sha256:310cf31a05594456a7c321d0bd4f926ab6769d1fe7868c796e75f6ac6dd06093` | 425563555 | 425563555 | `sha256:310cf31a05594456a7c321d0bd4f926ab6769d1fe7868c796e75f6ac6dd06093` | yes |

## Прогоны `run.sh`

| Инструкция | Окружение | Exit code | Первые 200 символов stdout | Первые 200 символов stderr | Длительность |
|---|---|---:|---|---|---:|
| обычный однострочный текст | переменные не заданы | 0 | agent ready: workspace=/app (image workdir /app) instruction=22:b49696009556 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL  | — | 413 мс |
| двойные/одинарные кавычки, обратный слэш и $ | переменные не заданы | 0 | agent ready: workspace=/app (image workdir /app) instruction=60:7338dda07767 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL  | — | 373 мс |
| внутренний перевод строки | переменные не заданы | 0 | agent ready: workspace=/app (image workdir /app) instruction=22:73621482ff08 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL  | — | 375 мс |
| русский текст и emoji | переменные не заданы | 0 | agent ready: workspace=/app (image workdir /app) instruction=26:4a952a7cfaa9 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL  | — | 373 мс |
| начинается с дефиса | переменные не заданы | 0 | agent ready: workspace=/app (image workdir /app) instruction=19:2a4cded0cb73 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL  | — | 387 мс |
| пустая строка | переменные не заданы | 2 | — | usage: run.sh "<task instruction>"  | 426 мс |
| аргумент не передан | переменные не заданы | 2 | — | usage: run.sh "<task instruction>"  | 377 мс |
| обычный однострочный текст | 127.0.0.1:1/v1; test key/model | 0 | agent ready: workspace=/app (image workdir /app) instruction=22:b49696009556 budget=600s reserve=120s steps=30 endpoint=configured  | — | 384 мс |
| двойные/одинарные кавычки, обратный слэш и $ | 127.0.0.1:1/v1; test key/model | 0 | agent ready: workspace=/app (image workdir /app) instruction=60:7338dda07767 budget=600s reserve=120s steps=30 endpoint=configured  | — | 401 мс |
| внутренний перевод строки | 127.0.0.1:1/v1; test key/model | 0 | agent ready: workspace=/app (image workdir /app) instruction=22:73621482ff08 budget=600s reserve=120s steps=30 endpoint=configured  | — | 392 мс |
| русский текст и emoji | 127.0.0.1:1/v1; test key/model | 0 | agent ready: workspace=/app (image workdir /app) instruction=26:4a952a7cfaa9 budget=600s reserve=120s steps=30 endpoint=configured  | — | 416 мс |
| начинается с дефиса | 127.0.0.1:1/v1; test key/model | 0 | agent ready: workspace=/app (image workdir /app) instruction=19:2a4cded0cb73 budget=600s reserve=120s steps=30 endpoint=configured  | — | 387 мс |
| пустая строка | 127.0.0.1:1/v1; test key/model | 2 | — | usage: run.sh "<task instruction>"  | 370 мс |
| аргумент не передан | 127.0.0.1:1/v1; test key/model | 2 | — | usage: run.sh "<task instruction>"  | 396 мс |
| обычный однострочный текст | синтаксически неверные значения | 0 | agent ready: workspace=/app (image workdir /app) instruction=22:b49696009556 budget=600s reserve=120s steps=30 endpoint=configured  | — | 379 мс |
| двойные/одинарные кавычки, обратный слэш и $ | синтаксически неверные значения | 0 | agent ready: workspace=/app (image workdir /app) instruction=60:7338dda07767 budget=600s reserve=120s steps=30 endpoint=configured  | — | 374 мс |
| внутренний перевод строки | синтаксически неверные значения | 0 | agent ready: workspace=/app (image workdir /app) instruction=22:73621482ff08 budget=600s reserve=120s steps=30 endpoint=configured  | — | 367 мс |
| русский текст и emoji | синтаксически неверные значения | 0 | agent ready: workspace=/app (image workdir /app) instruction=26:4a952a7cfaa9 budget=600s reserve=120s steps=30 endpoint=configured  | — | 379 мс |
| начинается с дефиса | синтаксически неверные значения | 0 | agent ready: workspace=/app (image workdir /app) instruction=19:2a4cded0cb73 budget=600s reserve=120s steps=30 endpoint=configured  | — | 388 мс |
| пустая строка | синтаксически неверные значения | 2 | — | usage: run.sh "<task instruction>"  | 386 мс |
| аргумент не передан | синтаксически неверные значения | 2 | — | usage: run.sh "<task instruction>"  | 365 мс |

Во всех трёх окружениях непустые инструкции дали контролируемый статус и код `0` за 367–416 мс. При незаданных переменных status содержит `endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL`; при недостижимом и синтаксически неверном значениях он содержит только `endpoint=configured` и также завершается `0`. То есть фактический entrypoint **не валидирует** эти два конфигурационных случая и не сообщает ошибку endpoint. Зависания и traceback не зафиксированы.

## Layout распаковки

Исходный архив имеет файлы в корне. Вариант `one-top-level` ниже создан из тех же файлов с единственной искусственной оболочкой `payload/`, чтобы проверить требуемую альтернативную раскладку без изменения содержимого payload. «Принят» означает, что `run.sh` является непосредственным файлом каталога агента, то есть его находит контракт `./run.sh`.

| Вариант распаковки | Путь до `run.sh` | Прямой файл в каталоге агента | Принят контрактом |
|---|---|---|---|
| archive-root | `/opt/harbor/local-agent-root/run.sh` | yes | yes |
| one-top-level | `/opt/harbor/local-agent-one-top/payload/run.sh` | no | no |

## Утилиты и Python

| Команда | Есть или нет | Версия / фактический первый вывод |
|---|---|---|
| `sh` | yes | `sh: 0: Illegal option --` |
| `bash` | yes | `GNU bash, version 5.2.15(1)-release (x86_64-pc-linux-gnu)` |
| `git` | yes | `git version 2.39.5` |
| `curl` | yes | `curl 7.88.1 (x86_64-pc-linux-gnu) libcurl/7.88.1 OpenSSL/3.0.20 zlib/1.2.13 brotli/1.0.9 zstd/1.5.4 libidn2/2.3.3 libpsl/0.21.2 (+libidn2/2.3.3) libssh2/1.10.0 nghttp2/1.52.0 librtmp/2.3 OpenLDAP/2.5.13` |
| `wget` | yes | `GNU Wget 1.21.3 built on linux-gnu.` |
| `tar` | yes | `tar (GNU tar) 1.34` |
| `unzip` | yes | `UnZip 6.00 of 20 April 2009, by Debian. Original by Info-ZIP.` |
| `grep` | yes | `grep (GNU grep) 3.8` |
| `sed` | yes | `sed (GNU sed) 4.9` |
| `awk` | yes | `mawk 1.3.4 20200120` |
| `find` | yes | `find (GNU findutils) 4.9.0` |
| `xargs` | yes | `xargs (GNU findutils) 4.9.0` |
| `file` | yes | `file-5.44` |
| `strings` | yes | `GNU strings (GNU Binutils for Debian) 2.40` |
| `xxd` | no | — |
| `jq` | yes | `jq-1.6` |
| `rg` | yes | `ripgrep 13.0.0` |
| `sqlite3` | no | — |
| `tcpdump` | yes | `tcpdump version 4.99.3` |
| `nc` | yes | `OpenBSD netcat (Debian patchlevel 1.219-1)` |
| `ss` | yes | `ss utility, iproute2-6.1.0` |
| `ps` | yes | `Error, do this: mount -t proc proc /proc` |
| `lsof` | no | — |
| `gdb` | no | — |
| `objdump` | yes | `GNU objdump (GNU Binutils for Debian) 2.40` |
| `readelf` | yes | `GNU readelf (GNU Binutils for Debian) 2.40` |
| `nm` | yes | `GNU nm (GNU Binutils for Debian) 2.40` |
| `binwalk` | no | — |
| `openssl` | yes | `OpenSSL 3.0.20 7 Apr 2026 (Library: OpenSSL 3.0.20 7 Apr 2026)` |
| `base64` | yes | `base64 (GNU coreutils) 9.1` |
| `sha256sum` | yes | `sha256sum (GNU coreutils) 9.1` |
| `diff` | yes | `diff (GNU diffutils) 3.8` |
| `patch` | yes | `GNU patch 2.7.6` |
| `timeout` | yes | `timeout (GNU coreutils) 9.1` |
| `nproc` | yes | `nproc (GNU coreutils) 9.1` |

Фактические проверки Python: `python3 -V` → `Python 3.12.13`; импорт `ssl`, `sqlite3`, `lzma`, `zlib`, `bz2` завершился `0`; `python3 -m venv /tmp/acp-venv-probe` завершился `0`, а `/tmp/acp-venv-probe/bin/python` существует и сообщает `Python 3.12.13`.

Команда `pip` существует как `/usr/local/bin/pip`, версия — `pip 25.0.1`. Однако Python, выбранный image PATH (`/app/.venv/bin/python3`), не содержит модуля pip: `python3 -m pip --version` завершился `1` с `/app/.venv/bin/python3: No module named pip`. Установка `filelock` базовым pip в изолированном network namespace завершилась `1`; её дословный вывод приведён ниже.

```text
ERROR: Could not find a version that satisfies the requirement filelock (from versions: none)
ERROR: No matching distribution found for filelock
```

## Ограничения среды

| Параметр | Фактически внутри chroot |
|---|---|
| UID/GID | `uid=0(root) gid=0(root) groups=0(root)` |
| PATH | `/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` |
| Сеть в запусках | отдельный `unshare --net`; network namespace создан успешно |
| `/proc` | пустой каталог; `filesystem_type=ext2/ext3`; `/proc/self` отсутствует |
| `/sys` | пустой каталог; `filesystem_type=ext2/ext3`; sysfs не смонтирован |
| Image WORKDIR | `/app`; запись успешна (probe exit `0`) |
| Запись в `/tmp` | успешна (probe exit `0`) |
| Запись в `/` | успешна в отдельном диагностическом процессе (probe exit `0`) |
| CPU | `nproc=6`; `os.cpu_count()=6` |
| Память, видимая Python через sysconf | `25,604,468,736` байт |
| Последствие отсутствия `/proc` | `ps --version` найден, но завершился `47`: `Error, do this: mount -t proc proc /proc` |

## Приложение A. Дословные выводы всех прогонов

### `unset__ordinary`

| Поле | Значение |
|---|---|
| Инструкция | обычный однострочный текст |
| Окружение | переменные не заданы |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 413 мс |

**Переданный текст (bytes, как передан shell):**

```text
ordinary one-line text
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=22:b49696009556 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL
```

**stderr:**

```text
[пусто]
```

### `unset__quotes`

| Поле | Значение |
|---|---|
| Инструкция | двойные/одинарные кавычки, обратный слэш и $ |
| Окружение | переменные не заданы |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 373 мс |

**Переданный текст (bytes, как передан shell):**

```text
double "quotes", single 'quotes', backslash \\, dollar $HOME
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=60:7338dda07767 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL
```

**stderr:**

```text
[пусто]
```

### `unset__multiline`

| Поле | Значение |
|---|---|
| Инструкция | внутренний перевод строки |
| Окружение | переменные не заданы |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 375 мс |

**Переданный текст (bytes, как передан shell):**

```text
first line
second line
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=22:73621482ff08 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL
```

**stderr:**

```text
[пусто]
```

### `unset__russian_emoji`

| Поле | Значение |
|---|---|
| Инструкция | русский текст и emoji |
| Окружение | переменные не заданы |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 373 мс |

**Переданный текст (bytes, как передан shell):**

```text
Текст по-русски и emoji: ✅
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=26:4a952a7cfaa9 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL
```

**stderr:**

```text
[пусто]
```

### `unset__leading_dash`

| Поле | Значение |
|---|---|
| Инструкция | начинается с дефиса |
| Окружение | переменные не заданы |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 387 мс |

**Переданный текст (bytes, как передан shell):**

```text
-starts-with-a-dash
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=19:2a4cded0cb73 budget=600s reserve=120s steps=30 endpoint=missing:LOCAL_AGENT_MODEL,OPENAI_BASE_URL
```

**stderr:**

```text
[пусто]
```

### `unset__empty_string`

| Поле | Значение |
|---|---|
| Инструкция | пустая строка |
| Окружение | переменные не заданы |
| Аргумент | passed |
| Exit code | 2 |
| Длительность | 426 мс |

**Переданный текст (bytes, как передан shell):**

```text
[пусто]

```

**stdout:**

```text
[пусто]
```

**stderr:**

```text
usage: run.sh "<task instruction>"
```

### `unset__no_argument`

| Поле | Значение |
|---|---|
| Инструкция | аргумент не передан |
| Окружение | переменные не заданы |
| Аргумент | omitted |
| Exit code | 2 |
| Длительность | 377 мс |

**Переданный текст (bytes, как передан shell):**

```text
[пусто]

```

**stdout:**

```text
[пусто]
```

**stderr:**

```text
usage: run.sh "<task instruction>"
```

### `unreachable__ordinary`

| Поле | Значение |
|---|---|
| Инструкция | обычный однострочный текст |
| Окружение | 127.0.0.1:1/v1; test key/model |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 384 мс |

**Переданный текст (bytes, как передан shell):**

```text
ordinary one-line text
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=22:b49696009556 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `unreachable__quotes`

| Поле | Значение |
|---|---|
| Инструкция | двойные/одинарные кавычки, обратный слэш и $ |
| Окружение | 127.0.0.1:1/v1; test key/model |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 401 мс |

**Переданный текст (bytes, как передан shell):**

```text
double "quotes", single 'quotes', backslash \\, dollar $HOME
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=60:7338dda07767 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `unreachable__multiline`

| Поле | Значение |
|---|---|
| Инструкция | внутренний перевод строки |
| Окружение | 127.0.0.1:1/v1; test key/model |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 392 мс |

**Переданный текст (bytes, как передан shell):**

```text
first line
second line
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=22:73621482ff08 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `unreachable__russian_emoji`

| Поле | Значение |
|---|---|
| Инструкция | русский текст и emoji |
| Окружение | 127.0.0.1:1/v1; test key/model |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 416 мс |

**Переданный текст (bytes, как передан shell):**

```text
Текст по-русски и emoji: ✅
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=26:4a952a7cfaa9 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `unreachable__leading_dash`

| Поле | Значение |
|---|---|
| Инструкция | начинается с дефиса |
| Окружение | 127.0.0.1:1/v1; test key/model |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 387 мс |

**Переданный текст (bytes, как передан shell):**

```text
-starts-with-a-dash
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=19:2a4cded0cb73 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `unreachable__empty_string`

| Поле | Значение |
|---|---|
| Инструкция | пустая строка |
| Окружение | 127.0.0.1:1/v1; test key/model |
| Аргумент | passed |
| Exit code | 2 |
| Длительность | 370 мс |

**Переданный текст (bytes, как передан shell):**

```text
[пусто]

```

**stdout:**

```text
[пусто]
```

**stderr:**

```text
usage: run.sh "<task instruction>"
```

### `unreachable__no_argument`

| Поле | Значение |
|---|---|
| Инструкция | аргумент не передан |
| Окружение | 127.0.0.1:1/v1; test key/model |
| Аргумент | omitted |
| Exit code | 2 |
| Длительность | 396 мс |

**Переданный текст (bytes, как передан shell):**

```text
[пусто]

```

**stdout:**

```text
[пусто]
```

**stderr:**

```text
usage: run.sh "<task instruction>"
```

### `malformed__ordinary`

| Поле | Значение |
|---|---|
| Инструкция | обычный однострочный текст |
| Окружение | синтаксически неверные значения |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 379 мс |

**Переданный текст (bytes, как передан shell):**

```text
ordinary one-line text
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=22:b49696009556 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `malformed__quotes`

| Поле | Значение |
|---|---|
| Инструкция | двойные/одинарные кавычки, обратный слэш и $ |
| Окружение | синтаксически неверные значения |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 374 мс |

**Переданный текст (bytes, как передан shell):**

```text
double "quotes", single 'quotes', backslash \\, dollar $HOME
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=60:7338dda07767 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `malformed__multiline`

| Поле | Значение |
|---|---|
| Инструкция | внутренний перевод строки |
| Окружение | синтаксически неверные значения |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 367 мс |

**Переданный текст (bytes, как передан shell):**

```text
first line
second line
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=22:73621482ff08 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `malformed__russian_emoji`

| Поле | Значение |
|---|---|
| Инструкция | русский текст и emoji |
| Окружение | синтаксически неверные значения |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 379 мс |

**Переданный текст (bytes, как передан shell):**

```text
Текст по-русски и emoji: ✅
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=26:4a952a7cfaa9 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `malformed__leading_dash`

| Поле | Значение |
|---|---|
| Инструкция | начинается с дефиса |
| Окружение | синтаксически неверные значения |
| Аргумент | passed |
| Exit code | 0 |
| Длительность | 388 мс |

**Переданный текст (bytes, как передан shell):**

```text
-starts-with-a-dash
```

**stdout:**

```text
agent ready: workspace=/app (image workdir /app) instruction=19:2a4cded0cb73 budget=600s reserve=120s steps=30 endpoint=configured
```

**stderr:**

```text
[пусто]
```

### `malformed__empty_string`

| Поле | Значение |
|---|---|
| Инструкция | пустая строка |
| Окружение | синтаксически неверные значения |
| Аргумент | passed |
| Exit code | 2 |
| Длительность | 386 мс |

**Переданный текст (bytes, как передан shell):**

```text
[пусто]

```

**stdout:**

```text
[пусто]
```

**stderr:**

```text
usage: run.sh "<task instruction>"
```

### `malformed__no_argument`

| Поле | Значение |
|---|---|
| Инструкция | аргумент не передан |
| Окружение | синтаксически неверные значения |
| Аргумент | omitted |
| Exit code | 2 |
| Длительность | 365 мс |

**Переданный текст (bytes, как передан shell):**

```text
[пусто]

```

**stdout:**

```text
[пусто]
```

**stderr:**

```text
usage: run.sh "<task instruction>"
```

## Приложение B. Исходные артефакты проверки

В accompanying архиве `acp_submission_execution_artifacts.zip` сохранены таблицы с хешами слоёв, manifest/config, raw stdout/stderr каждого прогона, layout, лог возможности chroot, результаты проверки окружения и использованные сценарии. Восстановленный rootfs и скачанные blobs в архив не включены из-за размера.
