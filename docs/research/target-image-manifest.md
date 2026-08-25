# Фактический манифест `secureintelligent/acp`

**Срез реестра:** 2026-08-25T01:16:32+00:00. **Проверенная платформа:** `linux/amd64`. Образ существует публично в Docker Hub. В песочнице отсутствовали `docker`, `skopeo`, `crane` и `oras`; поэтому результат получен через **Docker Registry HTTP API v2**: OCI-index, manifest, config и все семь сжатых слоёв были скачаны, хеши файлов слоёв повторно сверены с digest’ами манифеста, а файловая система применена с OCI-whiteout. Проверки команд исполнялись в `chroot` распакованной amd64 ФС, с `PATH` из `config.Env`.

> Инвентаризация ниже не экстраполируется на `linux/arm64`. В OCI-index есть arm64-дескриптор, но его слои не скачивались, не распаковывались и не исполнялись. Все прикладные утверждения относятся к `linux/amd64`.

## Публичные теги и digest’ы

| Тег | OCI index digest | Дата публикации тега (UTC) | amd64 digest | arm64 digest | Сжатые amd64-слои, байт |
|---|---|---|---|---|---:|
| `latest` | `sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3` | `2026-07-02T14:57:25.825344Z` | `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132` | `sha256:60f32b3e722fcfdd6dc3a5f42f8e2a1860cc5c390583616cbfaa45785bf530a3` | 633,598,038 |
| `harbor-0.16.1` | `sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3` | `2026-07-02T15:00:01.357552Z` | `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132` | `sha256:60f32b3e722fcfdd6dc3a5f42f8e2a1860cc5c390583616cbfaa45785bf530a3` | 633,598,038 |


Оба опубликованных тега указывают на один OCI index. У amd64-конфигурации дата сборки `2026-07-02T14:47:59.5222338Z`; digest конфигурации — `sha256:301b78fe9962517ba85f8ff102760393f284ff7f7a150a7be235736a1eca7647`.

## Конфигурация и размеры amd64

| Поле | Проверенное значение |
|---|---|
| OS | Debian GNU/Linux 12 (bookworm), `amd64` |
| OCI index | `sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3` |
| Manifest | `sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132` |
| Config | `sha256:301b78fe9962517ba85f8ff102760393f284ff7f7a150a7be235736a1eca7647` |
| `ENTRYPOINT` | отсутствует в config (`null`) |
| `CMD` | `['harbor', '--help']` |
| `WORKDIR` | `/app` |
| `USER` | поле `User` отсутствует; chroot-проверка выдала `uid=0(root)` |
| Размер сжатых слоёв | 633,598,038 байт |
| Размер manifest: слои + config | 633,605,585 байт |
| Распакованная ФС (`du -sb`) | 1,944,379,708 байт |

**`Env` из image config, дословно:**

```text
PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LANG=en_US.UTF-8
GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305
PYTHON_VERSION=3.12.13
PYTHON_SHA256=c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
LC_ALL=en_US.UTF-8
```

## Инструменты: проверка из ФС образа

| Инструмент | Есть/нет | Версия | Путь |
|---|---|---|---|
| git | есть | 2.39.5 | /usr/bin/git |
| curl | есть | 7.88.1 | /usr/bin/curl |
| wget | есть | 1.21.3 | /usr/bin/wget |
| rg (ripgrep) | есть | 13.0.0 | /usr/bin/rg |
| grep | есть | 3.8 | /usr/bin/grep |
| jq | есть | 1.6 | /usr/bin/jq |
| sqlite3 | нет | — | — |
| unzip | есть | 6.00 | /usr/bin/unzip |
| zip | есть | 3.0 | /usr/bin/zip |
| tar | есть | 1.34 | /usr/bin/tar |
| patch | есть | 2.7.6 | /usr/bin/patch |
| gcc | есть | 12.2.0-14+deb12u1 | /usr/bin/gcc |
| make | есть | 4.3 | /usr/bin/make |
| gdb | нет | — | — |
| objdump | есть | 2.40 | /usr/bin/objdump |
| readelf | есть | 2.40 | /usr/bin/readelf |
| strings | есть | 2.40 | /usr/bin/strings |
| file | есть | 5.44 | /usr/bin/file |
| nmap | нет | — | — |
| nc | есть | OpenBSD netcat Debian patchlevel 1.219-1 | /usr/bin/nc |
| netcat | есть | OpenBSD netcat Debian patchlevel 1.219-1 | /usr/bin/netcat |
| tcpdump | есть | 4.99.3 | /usr/bin/tcpdump |
| tshark | нет | — | — |
| openssl | есть | 3.0.20 | /usr/bin/openssl |
| node | нет | — | — |
| java | нет | — | — |
| php | нет | — | — |
| docker | нет | — | — |
| podman | нет | — | — |

## Agent-фреймворки и SDK

Версия и путь ниже получены из metadata distribution в `/app/.venv/lib/python3.12/site-packages`; полный перечень всех пакетов — в разделе `pip list`.

| Инструмент/SDK | Есть/нет | Версия | Путь |
|---|---|---|---|
| harbor | есть | 0.16.1 | /app/.venv/lib/python3.12/site-packages |
| openai | есть | 2.44.0 | /app/.venv/lib/python3.12/site-packages |
| anthropic | есть | 0.115.1 | /app/.venv/lib/python3.12/site-packages |
| claude-agent-sdk | есть | 0.2.110 | /app/.venv/lib/python3.12/site-packages |
| litellm | есть | 1.90.2 | /app/.venv/lib/python3.12/site-packages |
| langchain | есть | 1.3.11 | /app/.venv/lib/python3.12/site-packages |
| langchain-anthropic | есть | 1.4.8 | /app/.venv/lib/python3.12/site-packages |
| langchain-classic | есть | 1.0.8 | /app/.venv/lib/python3.12/site-packages |
| langchain-community | есть | 0.4.2 | /app/.venv/lib/python3.12/site-packages |
| langchain-core | есть | 1.4.8 | /app/.venv/lib/python3.12/site-packages |
| langchain-openai | есть | 1.3.3 | /app/.venv/lib/python3.12/site-packages |
| langchain-protocol | есть | 0.0.18 | /app/.venv/lib/python3.12/site-packages |
| langchain-text-splitters | есть | 1.1.2 | /app/.venv/lib/python3.12/site-packages |
| langgraph | есть | 1.2.7 | /app/.venv/lib/python3.12/site-packages |
| langgraph-checkpoint | есть | 4.1.1 | /app/.venv/lib/python3.12/site-packages |
| langgraph-prebuilt | есть | 1.1.0 | /app/.venv/lib/python3.12/site-packages |
| langgraph-sdk | есть | 0.4.2 | /app/.venv/lib/python3.12/site-packages |
| langsmith | есть | 0.9.6 | /app/.venv/lib/python3.12/site-packages |
| llama-index | есть | 0.14.23 | /app/.venv/lib/python3.12/site-packages |
| llama-index-core | есть | 0.14.23 | /app/.venv/lib/python3.12/site-packages |
| llama-index-embeddings-openai | есть | 0.6.0 | /app/.venv/lib/python3.12/site-packages |
| llama-index-llms-openai | есть | 0.7.9 | /app/.venv/lib/python3.12/site-packages |
| llama-index-workflows | есть | 2.22.2 | /app/.venv/lib/python3.12/site-packages |
| openai-agents | есть | 0.17.7 | /app/.venv/lib/python3.12/site-packages |
| crewai | есть | 1.6.1 | /app/.venv/lib/python3.12/site-packages |
| pydantic-ai | есть | 2.3.0 | /app/.venv/lib/python3.12/site-packages |
| pydantic-ai-slim | есть | 2.3.0 | /app/.venv/lib/python3.12/site-packages |
| pydantic-evals | есть | 2.3.0 | /app/.venv/lib/python3.12/site-packages |
| pydantic-graph | есть | 2.3.0 | /app/.venv/lib/python3.12/site-packages |
| mcp | есть | 1.28.1 | /app/.venv/lib/python3.12/site-packages |
| fastmcp-slim | есть | 3.4.2 | /app/.venv/lib/python3.12/site-packages |
| instructor | есть | 1.15.4 | /app/.venv/lib/python3.12/site-packages |
| google-genai | есть | 2.10.0 | /app/.venv/lib/python3.12/site-packages |
| groq | есть | 1.5.0 | /app/.venv/lib/python3.12/site-packages |
| cohere | есть | 7.0.5 | /app/.venv/lib/python3.12/site-packages |
| mistralai | есть | 2.5.1 | /app/.venv/lib/python3.12/site-packages |
| requests | есть | 2.34.2 | /app/.venv/lib/python3.12/site-packages |
| httpx | есть | 0.28.1 | /app/.venv/lib/python3.12/site-packages |
| pydantic | есть | 2.13.4 | /app/.venv/lib/python3.12/site-packages |
| chromadb | есть | 1.1.1 | /app/.venv/lib/python3.12/site-packages |
| huggingface_hub | есть | 1.16.1 | /app/.venv/lib/python3.12/site-packages |
| uv | есть | 0.11.26 | /app/.venv/lib/python3.12/site-packages |

## Python и `pip`

Найдены только один базовый интерпретатор и один venv, оба используют CPython **3.12.13**. `/usr/local/bin/python`, `/usr/local/bin/python3` и `/usr/local/bin/python3.12` указывают на один базовый interpreter. `/app/.venv/bin/python`, `python3` и `python3.12` указывают на тот же binary, но с префиксом `/app/.venv` и отдельным `site-packages`.

Базовый Python содержит `pip 25.0.1`. В venv `python -m pip list` и `python -m pip freeze` завершаются с `No module named pip`, поэтому буквальный `pip list` этого venv недоступен. Чтобы всё же снять полный список фактически размещённых в нём дистрибутивов без установки зависимостей, выполнено `base-python -m pip list --path /app/.venv/lib/python3.12/site-packages` и соответствующий `freeze --path`. Вывод ниже дословный; он включает оба нативных вызова и path-вариант.

```text
=== base interpreter: python -m pip list ===
WARNING: The directory '/root/.cache/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.
Package Version
------- -------
pip     25.0.1
=== base interpreter: python -m pip freeze ===
WARNING: The directory '/root/.cache/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.
=== venv interpreter: python -m pip list ===
/app/.venv/bin/python: No module named pip
venv-python-pip-list-exit=1
=== venv interpreter: python -m pip freeze ===
/app/.venv/bin/python: No module named pip
venv-python-pip-freeze-exit=1
=== base pip, explicit venv site-packages: pip list --path ===
WARNING: The directory '/root/.cache/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.
Package                                  Version
---------------------------------------- ---------------
aiofile                                  3.11.1
aiohappyeyeballs                         2.7.1
aiohttp                                  3.14.1
aiosignal                                1.4.0
aiosqlite                                0.22.1
annotated-doc                            0.0.4
annotated-types                          0.7.0
anthropic                                0.115.1
anyio                                    4.14.1
appdirs                                  1.4.4
argcomplete                              3.7.0
attrs                                    26.1.0
Authlib                                  1.7.2
backoff                                  2.2.1
banks                                    2.4.4
bcrypt                                   5.0.0
beartype                                 0.22.9
build                                    1.5.0
cachetools                               7.1.4
caio                                     0.9.25
certifi                                  2026.6.17
cffi                                     2.0.0
charset-normalizer                       3.4.7
chromadb                                 1.1.1
claude-agent-sdk                         0.2.110
click                                    8.4.2
cohere                                   7.0.5
colorama                                 0.4.6
crewai                                   1.6.1
cryptography                             49.0.0
dataclasses-json                         0.6.7
datasets                                 5.0.0
Deprecated                               1.3.1
deprecation                              2.1.0
dill                                     0.4.1
dirhash                                  0.5.0
dirtyjson                                1.0.8
distro                                   1.9.0
dnspython                                2.8.0
docstring_parser                         0.18.0
durationpy                               0.10
email-validator                          2.3.0
et_xmlfile                               2.0.0
eval_type_backport                       0.4.0
exceptiongroup                           1.3.1
executing                                2.2.1
fastapi                                  0.139.0
fastavro                                 1.12.2
fastmcp-slim                             3.4.2
fastuuid                                 0.14.0
filelock                                 3.29.4
filetype                                 1.2.0
flatbuffers                              25.12.19
frozenlist                               1.8.0
fsspec                                   2026.4.0
genai-prices                             0.0.69
google-auth                              2.55.1
google-genai                             2.10.0
googleapis-common-protos                 1.75.0
greenlet                                 3.5.3
griffe                                   2.1.0
griffecli                                2.1.0
griffelib                                2.1.0
groq                                     1.5.0
grpcio                                   1.81.1
h11                                      0.16.0
h2                                       4.3.0
harbor                                   0.16.1
hf-xet                                   1.5.1
hpack                                    4.2.0
httpcore                                 1.0.9
httpcore2                                2.5.0
httptools                                0.8.0
httpx                                    0.28.1
httpx-sse                                0.4.3
httpx2                                   2.5.0
huggingface_hub                          1.16.1
hyperframe                               6.1.0
idna                                     3.18
importlib_metadata                       8.7.1
importlib_resources                      7.1.0
instructor                               1.15.4
jaraco.classes                           3.4.0
jaraco.context                           6.1.2
jaraco.functools                         4.5.0
jeepney                                  0.9.0
Jinja2                                   3.1.6
jiter                                    0.14.0
joblib                                   1.5.3
joserfc                                  1.7.2
json_repair                              0.25.2
json5                                    0.15.0
jsonpatch                                1.33
jsonpath-python                          1.1.6
jsonpointer                              3.1.1
jsonref                                  1.1.0
jsonschema                               4.26.0
jsonschema-specifications                2025.9.1
keyring                                  25.7.0
kubernetes                               36.0.2
langchain                                1.3.11
langchain-anthropic                      1.4.8
langchain-classic                        1.0.8
langchain-community                      0.4.2
langchain-core                           1.4.8
langchain-openai                         1.3.3
langchain-protocol                       0.0.18
langchain-text-splitters                 1.1.2
langgraph                                1.2.7
langgraph-checkpoint                     4.1.1
langgraph-prebuilt                       1.1.0
langgraph-sdk                            0.4.2
langsmith                                0.9.6
litellm                                  1.90.2
llama-index                              0.14.23
llama-index-core                         0.14.23
llama-index-embeddings-openai            0.6.0
llama-index-instrumentation              0.5.0
llama-index-llms-openai                  0.7.9
llama-index-workflows                    2.22.2
logfire                                  4.37.0
logfire-api                              4.37.0
markdown-it-py                           4.2.0
MarkupSafe                               3.0.3
marshmallow                              3.26.2
mcp                                      1.28.1
mdurl                                    0.1.2
mistralai                                2.5.1
mmh3                                     5.2.1
more-itertools                           11.1.0
multidict                                6.7.1
multiprocess                             0.70.19
mypy_extensions                          1.1.0
nest-asyncio                             1.6.0
networkx                                 3.6.1
nltk                                     3.9.4
numpy                                    2.5.0
oauthlib                                 3.3.1
onnxruntime                              1.27.0
openai                                   2.44.0
openai-agents                            0.17.7
openpyxl                                 3.1.5
opentelemetry-api                        1.39.1
opentelemetry-exporter-otlp-proto-common 1.39.1
opentelemetry-exporter-otlp-proto-grpc   1.39.1
opentelemetry-exporter-otlp-proto-http   1.39.1
opentelemetry-instrumentation            0.60b1
opentelemetry-instrumentation-httpx      0.60b1
opentelemetry-proto                      1.39.1
opentelemetry-sdk                        1.39.1
opentelemetry-semantic-conventions       0.60b1
opentelemetry-util-http                  0.60b1
orjson                                   3.11.9
ormsgpack                                1.12.2
overrides                                7.7.0
packaging                                26.2
pandas                                   3.0.3
pathspec                                 1.1.1
pdfminer.six                             20260107
pdfplumber                               0.11.10
pillow                                   12.3.0
platformdirs                             4.10.0
portalocker                              2.7.0
postgrest                                2.31.0
posthog                                  5.4.0
prompt_toolkit                           3.0.52
propcache                                0.5.2
protobuf                                 6.33.6
py-key-value-aio                         0.4.5
pyarrow                                  24.0.0
pyasn1                                   0.6.3
pyasn1_modules                           0.4.2
pybase64                                 1.4.3
pycparser                                3.0
pydantic                                 2.13.4
pydantic-ai                              2.3.0
pydantic-ai-slim                         2.3.0
pydantic_core                            2.46.4
pydantic-evals                           2.3.0
pydantic-graph                           2.3.0
pydantic-settings                        2.14.2
Pygments                                 2.20.0
PyJWT                                    2.13.0
pypdfium2                                5.11.0
pyperclip                                1.11.0
PyPika                                   0.51.1
pyproject_hooks                          1.2.0
python-dateutil                          2.9.0.post0
python-dotenv                            1.2.2
python-multipart                         0.0.32
PyYAML                                   6.0.3
realtime                                 2.31.0
referencing                              0.37.0
regex                                    2026.6.28
requests                                 2.34.2
requests-oauthlib                        2.0.0
requests-toolbelt                        1.0.0
rich                                     14.3.4
rpds-py                                  2026.6.3
scantree                                 0.0.4
SecretStorage                            3.5.0
setuptools                               82.0.1
shellingham                              1.5.4
shortuuid                                1.0.13
six                                      1.17.0
sniffio                                  1.3.1
SQLAlchemy                               2.0.51
sse-starlette                            3.4.5
starlette                                1.3.1
storage3                                 2.31.0
StrEnum                                  0.4.15
supabase                                 2.31.0
supabase-auth                            2.31.0
supabase-functions                       2.31.0
tenacity                                 9.1.4
tiktoken                                 0.13.0
tinytag                                  2.2.1
tokenizers                               0.23.1
toml                                     0.10.2
tomli                                    2.4.1
tomli_w                                  1.2.0
tqdm                                     4.68.3
truststore                               0.10.4
typer                                    0.26.8
types-requests                           2.33.0.20260518
typing_extensions                        4.16.0
typing-inspect                           0.9.0
typing-inspection                        0.4.2
urllib3                                  2.7.0
uuid_utils                               0.16.2
uv                                       0.11.26
uvicorn                                  0.49.0
uvloop                                   0.22.1
watchfiles                               1.2.0
wcwidth                                  0.8.2
websocket-client                         1.9.0
websockets                               15.0.1
wrapt                                    1.17.3
xxhash                                   3.8.0
yarl                                     1.24.2
zipp                                     4.1.0
zstandard                                0.25.0
venv-path-pip-list-exit=0
=== base pip, explicit venv site-packages: pip freeze --path ===
WARNING: The directory '/root/.cache/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.
aiofile==3.11.1
aiohappyeyeballs==2.7.1
aiohttp==3.14.1
aiosignal==1.4.0
aiosqlite==0.22.1
annotated-doc==0.0.4
annotated-types==0.7.0
anthropic==0.115.1
anyio==4.14.1
appdirs==1.4.4
argcomplete==3.7.0
attrs==26.1.0
Authlib==1.7.2
backoff==2.2.1
banks==2.4.4
bcrypt==5.0.0
beartype==0.22.9
build==1.5.0
cachetools==7.1.4
caio==0.9.25
certifi==2026.6.17
cffi==2.0.0
charset-normalizer==3.4.7
chromadb==1.1.1
claude-agent-sdk==0.2.110
click==8.4.2
cohere==7.0.5
colorama==0.4.6
crewai==1.6.1
cryptography==49.0.0
dataclasses-json==0.6.7
datasets==5.0.0
Deprecated==1.3.1
deprecation==2.1.0
dill==0.4.1
dirhash==0.5.0
dirtyjson==1.0.8
distro==1.9.0
dnspython==2.8.0
docstring_parser==0.18.0
durationpy==0.10
email-validator==2.3.0
et_xmlfile==2.0.0
eval_type_backport==0.4.0
exceptiongroup==1.3.1
executing==2.2.1
fastapi==0.139.0
fastavro==1.12.2
fastmcp-slim==3.4.2
fastuuid==0.14.0
filelock==3.29.4
filetype==1.2.0
flatbuffers==25.12.19
frozenlist==1.8.0
fsspec==2026.4.0
genai-prices==0.0.69
google-auth==2.55.1
google-genai==2.10.0
googleapis-common-protos==1.75.0
greenlet==3.5.3
griffe==2.1.0
griffecli==2.1.0
griffelib==2.1.0
groq==1.5.0
grpcio==1.81.1
h11==0.16.0
h2==4.3.0
harbor==0.16.1
hf-xet==1.5.1
hpack==4.2.0
httpcore==1.0.9
httpcore2==2.5.0
httptools==0.8.0
httpx==0.28.1
httpx-sse==0.4.3
httpx2==2.5.0
huggingface_hub==1.16.1
hyperframe==6.1.0
idna==3.18
importlib_metadata==8.7.1
importlib_resources==7.1.0
instructor==1.15.4
jaraco.classes==3.4.0
jaraco.context==6.1.2
jaraco.functools==4.5.0
jeepney==0.9.0
Jinja2==3.1.6
jiter==0.14.0
joblib==1.5.3
joserfc==1.7.2
json5==0.15.0
json_repair==0.25.2
jsonpatch==1.33
jsonpath-python==1.1.6
jsonpointer==3.1.1
jsonref==1.1.0
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
keyring==25.7.0
kubernetes==36.0.2
langchain==1.3.11
langchain-anthropic==1.4.8
langchain-classic==1.0.8
langchain-community==0.4.2
langchain-core==1.4.8
langchain-openai==1.3.3
langchain-protocol==0.0.18
langchain-text-splitters==1.1.2
langgraph==1.2.7
langgraph-checkpoint==4.1.1
langgraph-prebuilt==1.1.0
langgraph-sdk==0.4.2
langsmith==0.9.6
litellm==1.90.2
llama-index==0.14.23
llama-index-core==0.14.23
llama-index-embeddings-openai==0.6.0
llama-index-instrumentation==0.5.0
llama-index-llms-openai==0.7.9
llama-index-workflows==2.22.2
logfire==4.37.0
logfire-api==4.37.0
markdown-it-py==4.2.0
MarkupSafe==3.0.3
marshmallow==3.26.2
mcp==1.28.1
mdurl==0.1.2
mistralai==2.5.1
mmh3==5.2.1
more-itertools==11.1.0
multidict==6.7.1
multiprocess==0.70.19
mypy_extensions==1.1.0
nest-asyncio==1.6.0
networkx==3.6.1
nltk==3.9.4
numpy==2.5.0
oauthlib==3.3.1
onnxruntime==1.27.0
openai==2.44.0
openai-agents==0.17.7
openpyxl==3.1.5
opentelemetry-api==1.39.1
opentelemetry-exporter-otlp-proto-common==1.39.1
opentelemetry-exporter-otlp-proto-grpc==1.39.1
opentelemetry-exporter-otlp-proto-http==1.39.1
opentelemetry-instrumentation==0.60b1
opentelemetry-instrumentation-httpx==0.60b1
opentelemetry-proto==1.39.1
opentelemetry-sdk==1.39.1
opentelemetry-semantic-conventions==0.60b1
opentelemetry-util-http==0.60b1
orjson==3.11.9
ormsgpack==1.12.2
overrides==7.7.0
packaging==26.2
pandas==3.0.3
pathspec==1.1.1
pdfminer.six==20260107
pdfplumber==0.11.10
pillow==12.3.0
platformdirs==4.10.0
portalocker==2.7.0
postgrest==2.31.0
posthog==5.4.0
prompt_toolkit==3.0.52
propcache==0.5.2
protobuf==6.33.6
py-key-value-aio==0.4.5
pyarrow==24.0.0
pyasn1==0.6.3
pyasn1_modules==0.4.2
pybase64==1.4.3
pycparser==3.0
pydantic==2.13.4
pydantic-ai==2.3.0
pydantic-ai-slim==2.3.0
pydantic-evals==2.3.0
pydantic-graph==2.3.0
pydantic-settings==2.14.2
pydantic_core==2.46.4
Pygments==2.20.0
PyJWT==2.13.0
pypdfium2==5.11.0
pyperclip==1.11.0
PyPika==0.51.1
pyproject_hooks==1.2.0
python-dateutil==2.9.0.post0
python-dotenv==1.2.2
python-multipart==0.0.32
PyYAML==6.0.3
realtime==2.31.0
referencing==0.37.0
regex==2026.6.28
requests==2.34.2
requests-oauthlib==2.0.0
requests-toolbelt==1.0.0
rich==14.3.4
rpds-py==2026.6.3
scantree==0.0.4
SecretStorage==3.5.0
setuptools==82.0.1
shellingham==1.5.4
shortuuid==1.0.13
six==1.17.0
sniffio==1.3.1
SQLAlchemy==2.0.51
sse-starlette==3.4.5
starlette==1.3.1
storage3==2.31.0
StrEnum==0.4.15
supabase==2.31.0
supabase-auth==2.31.0
supabase-functions==2.31.0
tenacity==9.1.4
tiktoken==0.13.0
tinytag==2.2.1
tokenizers==0.23.1
toml==0.10.2
tomli==2.4.1
tomli_w==1.2.0
tqdm==4.68.3
truststore==0.10.4
typer==0.26.8
types-requests==2.33.0.20260518
typing-inspect==0.9.0
typing-inspection==0.4.2
typing_extensions==4.16.0
urllib3==2.7.0
uuid_utils==0.16.2
uv==0.11.26
uvicorn==0.49.0
uvloop==0.22.1
watchfiles==1.2.0
wcwidth==0.8.2
websocket-client==1.9.0
websockets==15.0.1
wrapt==1.17.3
xxhash==3.8.0
yarl==1.24.2
zipp==4.1.0
zstandard==0.25.0
venv-path-pip-freeze-exit=0
```

## Отдельные ответы

### Harbor

Установлен **Harbor 0.16.1**. Проверены `harbor --version` и metadata пакета. CLI находится в `/app/.venv/bin/harbor`, исходники пакета — в `/app/.venv/lib/python3.12/site-packages/harbor/`; import origin — `/app/.venv/lib/python3.12/site-packages/harbor/__init__.py`.

### Пример агента или шаблон запуска

В `/app` вне `.venv` нет ни одного обычного файла: самостоятельного предустановленного user-agent, `run.sh` либо launch template в рабочем каталоге **нет**. Однако это не означает, что в образе нет шаблонов Harbor вообще. В установленном пакете имеются его собственные шаблоны, включая `/app/.venv/lib/python3.12/site-packages/harbor/cli/template-adapter/` и `/app/.venv/lib/python3.12/site-packages/harbor/cli/template-task/`; также присутствует framework-wrapper ACP `/app/.venv/lib/python3.12/site-packages/harbor/agents/installed/acp.py`. Это код/шаблоны Harbor, а не готовый пользовательский агент в `/app`.

### Форензика и реверс

**Присутствуют:** `strings`, `objdump`, `readelf`, `file`, `nm`, `ar`, `od`, `ldd`, `tcpdump`, `dig`, `nslookup`, `ip`, `ss`, `ping`, `traceroute`, `sha256sum`, `md5sum`, `dd`, `gzip`, `xz`, `tar`, `zip`, `unzip`, `jq`, а также `nc`/`netcat`.

**Отсутствуют (каждое имя было проверено через `command -v`):** `gdb`, `strace`, `ltrace`, `radare2`/`r2`, `binwalk`, `foremost`, `volatility`/`vol`/`vol.py`, `bulk_extractor`, `exiftool`, `yara`, `tshark`, `wireshark`, `capinfos`, `editcap`, `mergecap`, `tcpreplay`, `nmap`, `socat`, `xxd`, `hexdump`, `sqlite3`, `psql`, `mysql`, `redis-cli`, `yq`, `xmlstarlet`, `llvm-objdump`, `dwarfdump`.

## Чего в образе нет

По исходному обязательному списку отсутствуют: `sqlite3`, `gdb`, `nmap`, `tshark`, `node`, `java`, `php`, `docker`, `podman`. Также отсутствует `pip` в `/app/.venv` как импортируемый модуль и исполняемый файл. Пользовательский пример агента/`run.sh` в `/app` отсутствует.

## Точные команды и повторение

Рабочие скрипты приложены рядом с отчётом. Они являются точными командами, которыми получены артефакты этой проверки; порядок запуска был следующим:

```bash
# 1. Проверка доступности OCI-клиентов и точного публичного репозитория
mkdir -p /home/ubuntu/acp_audit/raw
{ printf 'docker='; command -v docker || true; printf 'skopeo='; command -v skopeo || true; printf 'crane='; command -v crane || true; printf 'oras='; command -v oras || true; } | tee /home/ubuntu/acp_audit/raw/oci_clients.txt
curl --fail --silent --show-error --location \
  'https://hub.docker.com/v2/repositories/secureintelligent/acp/tags?page_size=100' \
  --output /home/ubuntu/acp_audit/raw/dockerhub_tags_page1.json

# 2. Registry v2: bearer token, OCI-index, amd64 manifest, config и все blobs
chmod 700 /home/ubuntu/acp_audit/fetch_registry.sh
/home/ubuntu/acp_audit/fetch_registry.sh

# 3. Применение слоёв с OCI whiteout
chmod 700 /home/ubuntu/acp_audit/unpack_layers.sh
/home/ubuntu/acp_audit/unpack_layers.sh

# 4. Исполнение проверок внутри распакованной ФС
chmod 700 /home/ubuntu/acp_audit/run_base_inventory.sh
/home/ubuntu/acp_audit/run_base_inventory.sh
chmod 700 /home/ubuntu/acp_audit/run_pip_path_probe.sh
/home/ubuntu/acp_audit/run_pip_path_probe.sh
chmod 700 /home/ubuntu/acp_audit/run_harbor_forensics_probe.sh
/home/ubuntu/acp_audit/run_harbor_forensics_probe.sh
chmod 700 /home/ubuntu/acp_audit/run_exact_versions.sh
/home/ubuntu/acp_audit/run_exact_versions.sh
```

> `fetch_registry.sh` получает краткоживущий anonymous bearer-token автоматически и не содержит статических секретов. `unpack_layers.sh` применяет whiteout-маркеры до распаковки каждого слоя. Для исполнения использован `sudo chroot /home/ubuntu/acp_audit/rootfs_amd64`; Docker-демон не использовался.

## Исходные данные и ограничения

Docker Hub tag API и Docker Registry v2 — первичные источники для tag/index/manifest/config. Пакеты и инструменты инвентаризованы только после получения и распаковки blob-слоёв, а не по README, Dockerfile или предположениям. В архиве с результатом отсутствуют скачанные blobs (~634 MB) и распакованная ФС (~1.94 GB); сохранены raw JSON-манифесты, журналы и воспроизводимые скрипты.

[1]: https://hub.docker.com/v2/repositories/secureintelligent/acp/tags?page_size=100 "Docker Hub Tags API: secureintelligent/acp"
[2]: https://registry-1.docker.io/v2/ "Docker Registry HTTP API v2"
