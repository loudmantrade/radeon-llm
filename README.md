# Radeon LLM

Локальный сервер Qwen3 с OpenAI-совместимым API для OpenCode, Vulkan и дисковым
индексом KV-контекстов. Проект вырос из эксперимента на Intel MacBook Pro с Radeon
Pro 560X 4 GB; добавлен отдельный Windows launcher для нативного Vulkan-драйвера.

## Windows

Для сборки и установки на текущей машине используйте `./setup.command` на macOS
или `.\setup.ps1` на Windows. Если Python уже установлен: `python setup.py`.
Setup собирает исходники, проверяет модель, устанавливает автозапуск и проверяет
API. [Установка одной командой и параметры](SETUP.md).

Нужны Python 3.11+, Git, CMake, Visual Studio 2022 C++ Build Tools, Vulkan SDK и
драйвер видеокарты с Vulkan. Выполняйте сборку из Developer PowerShell for VS 2022:

```powershell
git clone https://github.com/loudmantrade/radeon-llm.git
cd radeon-llm
python server/bootstrap.py build
python server/bootstrap.py download-model
python server/windows.py configure
python server/windows.py start
python server/windows.py check-api
```

Автозапуск при входе: `python server/windows.py install`.
Остановка: `python server/windows.py stop`.
Логи: `python server/windows.py logs`.
Все команды доступны и через `windows.ps1`, с записью полного вывода.
[Полная инструкция Windows](WINDOWS.md).

После установки нативного OpenCode:

```powershell
python C:\path\radeon-llm\server\windows.py opencode --project C:\path\project
```

API: `http://127.0.0.1:8080/v1`, ключ генерируется локально в
`%LOCALAPPDATA%\radeon-llm\api-key`. Windows использует точное повторное
использование префиксов; обходы ошибок MoltenVK и экспериментальный перенос
KV-фрагментов выключены. Выбор GPU и размер контекста меняются в runtime config.

## Модель и воспроизводимость

- Qwen3-4B-Instruct-2507, GGUF Q4_K_M от Unsloth, около 2.5 GB.
- Источник, ревизия, размер и SHA-256: [models/qwen3-source.json](models/qwen3-source.json).
- llama.cpp закреплён на `2280f0fd163b03d4331d49e08460f66502e35854`.
- Патч численных обходов MoltenVK и активного диапазона KV:
  [patches/llama.cpp.patch](patches/llama.cpp.patch).
- `bootstrap.py source` получает исходники и применяет патч. Существующие
  checkout другой ревизии или конфликтующие изменения автоматически не сбрасываются.

Веса, SDK, драйверы, бинарные сборки, ключи, пользовательские конфиги, логи и
KV-снимки не включены в репозиторий. Модель загружается отдельной командой с
проверкой контрольной суммы. [Лицензии сторонних компонентов](THIRD_PARTY.md).

## Кеш и OpenCode

Python-прокси последовательно обслуживает один GPU-слот, выбирает снимок по
максимальному точному совпадению входных токенов и передаёт SSE/tool calls без
изменения ответа. Снимки сохраняются в простое, индексируются SQLite и
восстанавливаются после перезапуска. Дефолтные лимиты: 8 GiB на диске, 16 вариантов,
до 1 GiB горячих file-backed mmap, TTL 30 дней; вытеснение учитывает частоту и давность.

Прогрев OpenCode: `python server/windows.py warm --project C:\path\project --verify`.
Индекс общий для проектов. [Устройство кеша](INDEXED-CACHE.md),
[прогрев и границы повторного использования](OPENCODE-PREFIXES.md).

## macOS

`./setup.command` выбирает Vulkan/MoltenVK на Intel и Metal на Apple Silicon,
собирает движок и устанавливает LaunchAgent. Для Intel скачиваются проверенные
SDK/MoltenVK; для Metal они не нужны. [Новая инструкция setup](SETUP.md).
Исходные экспериментальные `.command`-скрипты и
[журнал Intel macOS](MACOS-LAB.md) сохранены отдельно.

## Проверки

```console
python -m unittest discover -s server/tests -v
```

Тесты покрывают HTTP/SSE, индекс и вытеснение, блокировки между процессами,
освобождение блокировки после аварии, save/restart/restore, выбор Windows GPU и
изменение совместимости при замене DLL. Тестовый HTTP-стенд не вычисляет модель.
GitHub Actions проверяет переносимый слой на Windows и macOS. Отдельный ручной
workflow собирает Vulkan backend на Windows и сохраняет бинарные артефакты.
Вычисления на настоящем Windows GPU требуют отдельной проверки драйвера и модели.

В исторических документах описаны также опыты с приблизительным KV shifting:
для него найден контрпример качества. Он не включён в Windows-профиль и в
опубликованный пример macOS-конфига. Перестановка системных сообщений OpenCode
была измерена отдельно и также не включена в рабочий протокол.
