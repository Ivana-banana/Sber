# Preflight ZIP-архива агента для Linux runner

Ниже принят строгий контракт: архив имеет размер не более **10 MiB = 10 485 760 байт**, содержит `run.sh` именно в корне, а runner после распаковки выполняет `./run.sh "<текст задачи>"`. ZIP сам по себе является контейнером, а не спецификацией поведения runner: значение Unix-режима хранится в OS-зависимом поле external file attributes, поэтому его необходимо проверять в центральном каталоге и затем подтверждать реальным extractor'ом runner'а [1] [2].

## Таблица причин отказа

| Причина | Как проявляется в контейнере | Как обнаружить до отправки |
|---|---|---|
| Нет execute-бита у `run.sh` | `Permission denied` при `./run.sh`; runner получает 0 баллов до запуска логики | Проверить Unix mode в `ZipInfo.external_attr`: для `run.sh` нужен тип regular file и `0755` (как минимум все `ugo+x`), `create_system=3`. На Windows бит на исходном NTFS не является доказательством: важна метаинформация ZIP. |
| CRLF или одиночный CR | `/bin/sh^M: bad interpreter`, `not found`, либо синтаксические ошибки | Читать `run.sh` как bytes; запретить `0d`, разрешить только LF. |
| UTF-8 BOM в начале | ядро видит не `#!` в byte zero; `Exec format error` или оболочка воспринимает BOM как команду | Проверить первые три байта `EF BB BF` и требовать начало `23 21` (`#!`). |
| Неверный или отсутствующий shebang | `Exec format error`, либо запуск не тем интерпретатором; `/bin/bash` может отсутствовать в минимальном образе | Требовать `#!/bin/sh` либо `#!/usr/bin/env sh`; наличие интерпретатора и фактическую совместимость проверить в контейнере. Linux требует исполняемый текстовый файл с первой строкой вида `#!interpreter [optional-arg]` [3]. |
| Обратные слеши в именах | Linux получает имя `agent\\main.py` как один файл, а ожидаемый `agent/main.py` отсутствует | Просмотреть все имена из central directory; запретить `\\`. |
| Абсолютные имена, `C:\...`, `..` | файл извлекается не туда, пропадает из ожидаемого дерева или extractor применяет sanitization | Запретить начальный `/`, drive prefix и компонент `..`; после распаковки сверить фактическое дерево. |
| Символическая ссылка | ссылку могут не создать, создать как ссылку на несуществующий путь или разрешить выход из дерева; entrypoint/импорт ломается | Распознать `S_IFLNK` в Unix mode; запретить symlink и специальные файлы. Не полагаться только на расширение имени. |
| Жёсткая ссылка | стандартный ZIP обычно не хранит inode-связь; extractor создаёт независимую копию или особый extra field трактуется иначе, нарушая ожидаемую связь | До отправки искать `st_nlink > 1` после распаковки на Unix и анализировать нестандартные Unix extra fields. Это эвристика: окончательное поведение видно только у extractor runner'а. |
| Явные пустые каталоги | обычно не ломают Linux, но отдельные runner'ы валидируют allowlist/структуру и считают их мусором | Запретить directory entries вида `name/`, если политика требует только файлы; либо отдельно разрешить непустые каталоги. Скрипт применяет строгий запрет. |
| Не-UTF-8 имена / отсутствует EFS | Windows-кодовая страница декодируется иначе; имя не совпадает побайтно, импорт или lookup не находится | Для каждого не-ASCII имени требовать bit 11 (EFS, UTF-8). ZIP допускает IBM code page по умолчанию и UTF-8 по флагу; Python поддерживает `metadata_encoding` как workaround для legacy archives [2]. |
| Конфликт регистра | `Agent.py` и `agent.py` сливаются/заменяют друг друга на case-insensitive Windows, но различаются на Linux; `Run.sh` не равен `run.sh` | Требовать точное имя `run.sh`; выявлять case-fold collisions (`name.casefold()`) и не допускать их. |
| Вложенный верхний каталог | runner ищет `./run.sh`, а файл лежит `submission/run.sh` | `run.sh` должен быть ровно одним member name `run.sh`; top-level layout проверяется до распаковки. |
| `.git`, `.venv`, `venv`, `__pycache__`, `.pytest_cache` и прочий кэш | размер/лимит, случайные файлы, платформенные бинарники, секреты, изменение воспроизводимости | Проверять компоненты пути и суффиксы, а не только список известных каталогов; безопаснее allowlist при сборке. |
| Логи, временные файлы, `.env`, ключи | утечка секретов, неправильный конфиг или переполнение лимита | Имена и содержимое текстовых файлов проверяются регулярными выражениями; результат `FAIL` требует ручного просмотра. |
| Архив > 10 MiB | submission отвергается до запуска | Проверить `stat().st_size` именно готового ZIP, после финального копирования/загрузки; вывести SHA-256. |
| Отдельный распакованный member слишком велик | extractor/runner исчерпывает диск или память; ZIP-bomb может быть малым по compressed size | Проверять `ZipInfo.file_size` и не распаковывать oversized members в preflight. Скрипт ограничивает каждый member 10 MiB. |
| ZIP64 | старый/ограниченный runner не понимает ZIP64 либо отвергает формат; при лимите 10 MiB ZIP64 не нужен | Проверить ZIP64 EOCD/locator и saturated classic EOCD fields. Python `zipfile` по умолчанию умеет ZIP64 и поэтому его надо явно исключать политикой [2]. |
| Нулевая длина или пустой ZIP | `BadZipFile`, EOF, либо нечего запускать | Проверить размер > 0, `ZipFile` открывается, есть members и `testzip()` не возвращает повреждённый entry. Спецификация требует хотя бы один файл для практически пригодного архива [1]. |
| CRC/центральный каталог повреждены | распаковка падает, entry обрывается, runner получает неполный код | `ZipFile.testzip()`, чтение и распаковка во временный каталог. |
| Дубликаты имён | разные extractor'ы выбирают первый/последний entry; возможен подменённый `run.sh` | Запретить duplicate central-directory names. |
| Неподдерживаемый compression/encryption | `compression method not supported`, пароль, отсутствие `zlib`/другого модуля | Ограничить метод DEFLATE или STORE, запретить encrypted entries; target smoke должен проверить фактический runner. Python документирует дополнительные модули для BZIP2/LZMA/Zstandard и различия совместимости [2]. |
| Неразрешимый импорт/бинарная зависимость | `ModuleNotFoundError`, `GLIBC... not found`, `Exec format error` | Статически просмотреть imports/ELF нельзя считать достаточным; использовать offline container smoke. |
| Сетевой вызов или установка пакетов | timeout/connection refused, зависание, запрет сети; установка невозможна | Искать URL, `requests`, `urllib`, `socket`, `curl`, `wget`, `pip install`, `apt`, `npm install` и аналоги. Это намеренно conservative scan: возможны false positive, но при бинарной оценке лучше ручное исключение, чем пропуск. |
| Неправильный cwd/относительные пути | скрипт работает локально, но не из каталога распаковки runner'а | В container smoke запускать из корня распаковки с пустым/реалистичным cwd; в коде строить пути от `__file__`, а не от текущего каталога. |
| Аргумент задачи теряется | агент запускается, но получает пустую/обрезанную инструкцию | Smoke с аргументом, содержащим пробелы, кавычки, Unicode и перевод строки; проверять `sys.argv`/`$1` и exit code. |
| Неверный exit status, timeout, stdout/stderr contract | бинарный evaluator считает crash/timeout/невалидный ответ нулём | Container-only smoke с timeout, проверкой exit code, формата вывода и отсутствия зависания. |

## Различия способов упаковки

| Инструмент | Типичный результат для Unix mode | Структура и риски |
|---|---|---|
| `python zipfile` с обычным `write()` | зависит от `ZipInfo.from_file()` и ОС; на Windows нельзя считать execute-bit сохранённым | Полностью управляемый вариант: создать `ZipInfo`, задать `create_system=3`, `external_attr=(0o100755 << 16)`, записать bytes; имена кодируются UTF-8 при необходимости. |
| `Compress-Archive` | API .NET `ZipArchive`; документация ограничивает размер одного файла 2 GB и отдельно говорит, что hidden files/folders игнорируются [4]. Не следует рассчитывать на перенос Unix execute-bit из Windows | `-Path dir` включает корневой каталог, `-Path dir\*` исключает его; это частая причина вложенного верхнего каталога. |
| Проводник Windows | обычно хранит DOS/Windows-ориентированные атрибуты; NTFS ACL и Unix `0755` не являются переносимым ZIP mode | Удобен для документов, но непригоден как доказательство исполняемого Unix entrypoint; возможны скрытые/служебные файлы и неоднозначная структура выделения. |
| 7-Zip | ZIP поддерживается, Unicode имена поддерживаются в самом 7z-формате, но ZIP-совместимость и Unix attributes зависят от версии, платформы и исходных metadata; Windows не получает execute-bit «из воздуха» [5] | Использовать только с явной проверкой `zipinfo`/Python после создания; не путать `.7z` с требуемым `.zip`. |
| `zip` в Linux/Info-ZIP | обычно переносит Unix type/mode в external attributes и поэтому естественно подходит для Linux | Сохраняет больше Unix metadata; следить за symlink (`-y/--symlinks`), extra fields, именами и тем, не попали ли кэши. |

### Рекомендуемая упаковка через `python zipfile`

```python
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

root = Path(".").resolve()
with ZipFile("submission.zip", "w", compression=ZIP_DEFLATED, allowZip64=False) as z:
    for name in ["run.sh", "agent/main.py"]:
        data = (root / name).read_bytes()
        info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3                 # Unix
        mode = 0o100755 if name == "run.sh" else 0o100644
        info.external_attr = mode << 16        # Unix mode in high 16 bits
        info.compress_type = ZIP_DEFLATED
        z.writestr(info, data)


Ключевой принцип из приложенного `build_submission.py` верен: **allowlist, сортировка имён, фиксированные timestamp и явные external attributes**. Для воспроизводимости следует также фиксировать compression level и проверять SHA-256 уже построенного файла.

## Скрипт проверки

Сохраните следующий код как `preflight_submission.py` и запускайте одинаково на Windows и Linux:

```python
#!/usr/bin/env python3
"""Deterministic preflight checker for a Linux-run ZIP submission.

Usage: python preflight_submission.py submission.zip
Exit status 0 means PASS; 1 means FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import stat
import struct
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_MEMBER_UNCOMPRESSED = MAX_ARCHIVE_BYTES
TEXT_SUFFIXES = {
    ".py", ".pyw", ".sh", ".bash", ".zsh", ".fish", ".js", ".ts", ".tsx",
    ".jsx", ".java", ".c", ".h", ".cc", ".cpp", ".cxx", ".go", ".rs",
    ".rb", ".php", ".pl", ".pm", ".lua", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".json", ".xml", ".md", ".txt", ".env",
}
CACHE_DIRS = {".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", "node_modules"}
CACHE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".swp", ".swo"}
SECRET_NAMES = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "authorized_keys"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password|passwd|private[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]
NETWORK_PATTERNS = [
    re.compile(r"(?i)\b(?:curl|wget|nc|netcat|telnet|ssh|scp|ftp|ping)\b"),
    re.compile(r"(?i)\b(?:requests|urllib\.request|httpx|aiohttp|socket|subprocess)\b"),
    re.compile(r"(?i)https?://|ftp://|git@[^\s:]+:"),
]
INSTALL_PATTERNS = [
    re.compile(r"(?i)\b(?:pip|pip3|python\s+-m\s+pip|uv|poetry|pipenv|npm|pnpm|yarn|apt(?:-get)?|apk|dnf|yum|brew)\s+(?:install|add|upgrade)\b"),
    re.compile(r"(?i)\b(?:apt-get|apt|apk|dnf|yum)\s+update\b"),
]


def add(errors: list[str], message: str) -> None:
    errors.append(message)


def zip64_markers(data: bytes) -> bool:
    """Detect ZIP64 EOCD/locator or saturated 32-bit EOCD fields."""
    tail = data[-(22 + 65535 + 20):]
    if b"PK\x06\x06" in tail or b"PK\x06\x07" in tail:
        return True
    pos = tail.rfind(b"PK\x05\x06")
    if pos >= 0 and len(tail) >= pos + 22:
        fields = struct.unpack_from("<4s4H2LH", tail, pos)
        return fields[3] == 0xFFFF or fields[4] == 0xFFFF or fields[5] == 0xFFFFFFFF or fields[6] == 0xFFFFFFFF
    return False


def safe_member_name(name: str) -> tuple[bool, str]:
    if not name or "\x00" in name:
        return False, "empty/NUL-containing member name"
    if "\\" in name:
        return False, "backslash in member name"
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False, "absolute or drive-qualified member name"
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return False, "parent traversal component '..'"
    return True, ""


def is_directory(info: zipfile.ZipInfo) -> bool:
    return info.is_dir() or info.filename.endswith("/")


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict ZIP submission preflight")
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    archive_path = args.archive
    errors: list[str] = []
    warnings: list[str] = []

    if not archive_path.is_file():
        print(f"FAIL: archive does not exist or is not a regular file: {archive_path}")
        return 1
    size = archive_path.stat().st_size
    digest = hashlib.sha256()
    with archive_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    print(f"archive: {archive_path}")
    print(f"size: {size} bytes ({size / 1024 / 1024:.2f} MiB)")
    print(f"sha256: {digest.hexdigest()}")
    if size == 0:
        add(errors, "archive has zero length")
    if size > MAX_ARCHIVE_BYTES:
        add(errors, f"archive exceeds {MAX_ARCHIVE_BYTES} bytes (10 MiB)")

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            infos = zf.infolist()
            if not infos:
                add(errors, "archive contains no members")
            with archive_path.open("rb") as raw_file:
                raw_file.seek(max(0, size - (22 + 65535 + 20)))
                raw = raw_file.read()
            if zip64_markers(raw):
                add(errors, "ZIP64 structures are present; submission policy requires classic ZIP")
            if all(info.file_size <= MAX_MEMBER_UNCOMPRESSED for info in infos):
                try:
                    bad = zf.testzip()
                    if bad is not None:
                        add(errors, f"CRC/integrity failure in member {bad!r}")
                except Exception as exc:
                    add(errors, f"integrity test failed: {type(exc).__name__}: {exc}")
            else:
                add(errors, "CRC test skipped for oversized member(s); archive is already FAIL")

            names: list[str] = []
            files: list[zipfile.ZipInfo] = []
            run_infos: list[zipfile.ZipInfo] = []
            for info in infos:
                name = info.filename
                names.append(name)
                okay, why = safe_member_name(name)
                if not okay:
                    add(errors, f"unsafe member {name!r}: {why}")
                if "\\" in name:
                    add(errors, f"member {name!r} uses backslashes; ZIP paths must be POSIX-style")
                if not (info.flag_bits & 0x800) and any(ord(ch) > 127 for ch in name):
                    add(errors, f"non-ASCII member {name!r} lacks the UTF-8 (EFS) flag")
                if info.file_size > MAX_MEMBER_UNCOMPRESSED:
                    add(errors, f"member {name!r} uncompressed size exceeds 10 MiB")
                mode = (info.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if kind == stat.S_IFLNK:
                    add(errors, f"symbolic link member is forbidden: {name!r}")
                if kind == stat.S_IFREG:
                    files.append(info)
                elif not is_directory(info) and kind not in (0, stat.S_IFREG):
                    add(errors, f"unsupported special-file type in member {name!r}")
                if name == "run.sh":
                    run_infos.append(info)
                lower_parts = {p.lower() for p in PurePosixPath(name.rstrip("/")).parts}
                lower_base = PurePosixPath(name.rstrip("/")).name.lower()
                if lower_parts & {x.lower() for x in CACHE_DIRS} or lower_base in {x.lower() for x in SECRET_NAMES} or lower_base.endswith(".env"):
                    add(errors, f"forbidden cache/environment/credential path: {name!r}")
                if lower_base.endswith(tuple(CACHE_SUFFIXES)):
                    add(errors, f"forbidden cache/log/temp file: {name!r}")

            if len(names) != len(set(names)):
                duplicates = sorted({n for n in names if names.count(n) > 1})
                add(errors, f"duplicate member names: {duplicates}")
            top_levels = {PurePosixPath(n.rstrip("/")).parts[0] for n in names if n.rstrip("/")}
            if "run.sh" not in names:
                add(errors, "run.sh is not at the archive root")
            if "run.sh" not in top_levels:
                add(errors, f"unexpected top-level layout; top-level names are {sorted(top_levels)!r}")
            if any(is_directory(i) for i in infos):
                add(errors, "explicit empty-directory entries are forbidden; omit empty directories")
            if len(run_infos) != 1:
                add(errors, f"expected exactly one root run.sh, found {len(run_infos)}")
            elif not is_directory(run_infos[0]):
                run = run_infos[0]
                mode = (run.external_attr >> 16) & 0xFFFF
                if (mode & 0o111) != 0o111:
                    add(errors, f"run.sh lacks owner/group/other execute bits in ZIP metadata: mode {mode & 0o777:o}")
                if run.create_system != 3:
                    add(errors, f"run.sh metadata was not created as Unix (create_system={run.create_system}); executable mode is not portable")
                try:
                    run_bytes = zf.read(run)
                    if run_bytes.startswith(b"\xef\xbb\xbf"):
                        add(errors, "run.sh begins with UTF-8 BOM")
                    if b"\r\n" in run_bytes or b"\r" in run_bytes:
                        add(errors, "run.sh contains CRLF or bare CR line endings")
                    if not run_bytes.startswith(b"#!"):
                        add(errors, "run.sh has no shebang at byte zero")
                    elif not (run_bytes.startswith(b"#!/bin/sh\n") or run_bytes.startswith(b"#!/usr/bin/env sh\n")):
                        add(errors, "run.sh shebang is not an allowed /bin/sh or env sh shebang")
                    run_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    add(errors, "run.sh is not valid UTF-8")
                except Exception as exc:
                    add(errors, f"cannot inspect run.sh: {type(exc).__name__}: {exc}")

            with tempfile.TemporaryDirectory(prefix="submission-preflight-") as tmp:
                root = Path(tmp)
                try:
                    zf.extractall(root)
                except Exception as exc:
                    add(errors, f"extraction failed: {type(exc).__name__}: {exc}")
                extracted_run = root / "run.sh"
                if not extracted_run.is_file():
                    add(errors, "run.sh was not produced at extraction root")
                # Do not use os.access() here: Python's zipfile.extractall() does not
                # restore Unix mode bits. The authoritative preflight check is the
                # Unix external-attributes field inspected above; the runner's extractor
                # must still be verified in the target container.
                for info in files:
                    if info.filename == "run.sh":
                        continue
                    path = root.joinpath(*PurePosixPath(info.filename).parts)
                    if path.is_file() and path.stat().st_nlink > 1:
                        add(errors, f"hard-linked extracted file detected: {info.filename!r}")
                    suffix = path.suffix.lower()
                    if suffix in TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile"}:
                        try:
                            text = path.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            warnings.append(f"non-UTF-8 text-like file (not automatically rejected): {info.filename!r}")
                            continue
                        for pattern in SECRET_PATTERNS:
                            if pattern.search(text):
                                add(errors, f"possible secret/private key in {info.filename!r}")
                                break
                        for pattern in NETWORK_PATTERNS:
                            if pattern.search(text):
                                add(errors, f"possible network call/import in {info.filename!r}")
                                break
                        for pattern in INSTALL_PATTERNS:
                            if pattern.search(text):
                                add(errors, f"possible dependency installation in {info.filename!r}")
                                break
    except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
        add(errors, f"cannot open/read ZIP: {type(exc).__name__}: {exc}")

    for warning in warnings:
        print(f"WARN: {warning}")
    if errors:
        print("\nFAIL")
        for index, reason in enumerate(errors, 1):
            print(f"{index}. {reason}")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Полная версия приложена отдельно, чтобы её можно было сохранить без риска искажений Markdown. Она использует только стандартную библиотеку, возвращает `0` для `PASS` и `1` для `FAIL`, печатает размер и SHA-256, проверяет CRC, ZIP64, root layout, имена, UTF-8/EFS, режим `run.sh`, BOM/CRLF/shebang, symlink/special files, кэши/секреты/логи, дубликаты, распаковку во временный каталог и conservative-признаки сети/установки зависимостей.

Проверено: синтаксис Python компилируется; на тестовом корректном архиве скрипт печатает `PASS`, на архиве с `agent/.venv` — `FAIL` и ненулевой exit status.

## Что невозможно доказать без целевого Linux-контейнера

Preflight не доказывает, что конкретный сторонний runner действительно восстанавливает external attributes как `0755`: разные extractors могут игнорировать mode, менять владельца, создавать ссылки иначе или извлекать с umask. Поэтому нужен smoke именно тем runner'ом, тем образом и тем способом распаковки, который используется на оценке.

Нельзя заранее доказать наличие `/bin/sh`, `/usr/bin/env`, Python нужной версии, динамического loader'а, системных библиотек, CLI-программ, правильного `PATH`, locale, cwd, пользователя, umask, прав на запись и mount flags вроде `noexec`. Нельзя также статически доказать отсутствие всех сетевых действий: вызов может быть собран динамически, спрятан в байткоде/бинарнике или реализован через системный resolver. Регулярный поиск — только защитный индикатор.

Отдельно необходимы проверки поведения: запуск с пустым, Unicode- и сложным quoted-аргументом; соблюдение timeout и memory/CPU limits; корректный exit code; отсутствие фоновых процессов; отсутствие обращения к DNS/сокетам; работа при полностью отключённом интернете; отсутствие попытки установить зависимости; корректность локального LLM endpoint через переданные переменные; отсутствие записи за пределы разрешённого каталога; и фактический формат ответа, ожидаемый evaluator'ом.

Наконец, только container smoke обнаружит расхождения версии Python/SDK, импортируемые модули, несовместимый shebang, ошибку case-sensitive lookup, реальный nested-root контракт и поведение организаторской обёртки `agent.py`. Поэтому `PASS` означает «архив соответствует проверяемому статическому контракту», а не «решение гарантированно получит ненулевой балл».

## References

[1]: https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT "PKWARE APPNOTE.TXT, ZIP File Format Specification"
[2]: https://docs.python.org/3/library/zipfile.html "Python 3 zipfile documentation"
[3]: https://man7.org/linux/man-pages/man2/execve.2.html "Linux execve(2) manual page"
[4]: https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.archive/compress-archive?view=powershell-7.6 "Microsoft Learn: Compress-Archive"
[5]: https://www.7-zip.org/7z.html "7-Zip: 7z format and capabilities"
