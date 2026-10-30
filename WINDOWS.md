# Windows + Vulkan

Автоматическая сборка и установка: `.\setup.ps1` или `python setup.py`.
[Инструкция setup](SETUP.md). Команды ниже остаются доступны для ручного управления.

Поддерживается Windows 10/11 x64 с Python 3.11+, Git, CMake, Visual Studio 2022
Build Tools (Desktop development with C++) и Vulkan SDK от LunarG.
Нужен актуальный драйвер видеокарты с Vulkan. MoltenVK на Windows не используется.
Сборку запускайте из Developer PowerShell for VS 2022.

## Установка и запуск

```powershell
git clone https://github.com/loudmantrade/radeon-llm.git
cd radeon-llm
python server/bootstrap.py build
python server/bootstrap.py download-model
python server/windows.py configure
python server/windows.py start
python server/windows.py check-api
```

Сборка получает закреплённую ревизию llama.cpp и применяет патч проекта.
Загрузка модели проверяет размер и SHA-256; оборванная загрузка продолжается из
`.gguf.part`. В репозитории нет весов модели, API-ключей, драйверов или SDK.
Для запуска скачанного движка вместо сборки укажите его путь:

```powershell
python server/windows.py configure --binary C:\llama\llama-server.exe --model C:\models\model.gguf
```

Для сторонней сборки совместимость её аргументов и KV API нужно проверить через
`check-api`; основной поддерживаемый вариант — сборка закреплённого исходника.

API доступен по `http://127.0.0.1:8080/v1`. Второй порт, 8081, используется
внутренним сервером. Python-прокси выбирает KV-снимок по точному префиксу и
передаёт chat/tool calls/SSE в llama-server.

Альтернативный интерфейс: `./windows.ps1 build`, `download-model`, `configure`,
`start`, `stop`, `restart`, `status`, `logs`, `check-api`, `warm`, `opencode`.
Он автоматически сохраняет полный вывод и статус в `logs/sessions`. Если политика
PowerShell запрещает `.ps1`, используйте прямые Python-команды выше.

## Фоновый процесс и автозапуск

```powershell
python server/windows.py install
python server/windows.py status
python server/windows.py logs
python server/windows.py restart
python server/windows.py stop
python server/windows.py uninstall
```

`install` создаёт пользовательскую задачу `RadeonLLM` в Планировщике заданий
с запуском при входе текущего пользователя и сразу запускает сервер. Задача
работает без сохранения пароля и без повышения прав. Она привязана к текущему
пути репозитория и Python: после переноса папки повторите `install`.
Это фоновый процесс пользователя, а не системная служба до входа в Windows.
Правила организации могут запрещать создание таких задач.

Настройки: `%LOCALAPPDATA%\radeon-llm\config.json`; ключ: `api-key` рядом с ним;
KV/SQLite: `cache\kv`; объединённый журнал Python и native: `logs\server.log`.
Путь можно переопределить через `--runtime C:\path\runtime` во всех командах.
Не публикуйте каталог runtime. Доступ к нему регулируется ACL Windows, а не
POSIX-правами `chmod`.

`stop` передаёт запрос завершения супервизору через файл, затем ждёт выхода
native-процесса и освобождения блокировки. На Windows используется байтовая
блокировка `msvcrt`, на macOS — `flock`. Снимки сохраняются во время простоя;
при остановке занятой генерации её незавершённое продолжение не сохраняется.

## Модель, GPU и контекст

По умолчанию — Qwen3-4B-Instruct-2507 Q4_K_M и KV Q8_0, окно 16384 токена,
один слот. GPU выбирается как первое устройство Vulkan; можно указать
`"gpu_device": "Vulkan1"` в config.json. Список устройств:

```powershell
.\llama.cpp\build-vulkan\bin\Release\llama-server.exe --list-devices
```

При недостатке VRAM уменьшите `context`, например до 8192, и перезапустите
процесс. Реальный размер окна сверяйте с журналом: `fit: on` разрешает движку
подгонять параметры под доступную память. Новое окно переносится в конфигурацию
OpenCode при следующем запуске через наш launcher.
CPU-диагностика: `python server/windows.py run --cpu`.

Обходы ошибок AMD/MoltenVK отключены. Приблизительное перемещение KV-фрагментов
также отключено: Windows использует точное повторное использование префиксов.
Старые снимки с Mac переносить не нужно: модель и DLL/настройки участвуют в
идентификаторе совместимости; на новой системе создаются новые снимки.

## OpenCode

Установите нативный OpenCode для Windows и убедитесь, что `opencode.exe` доступен
через PATH. Затем из папки проекта:

```powershell
python C:\path\radeon-llm\server\windows.py opencode --project C:\path\my-project
python C:\path\radeon-llm\server\windows.py warm --project C:\path\my-project --verify
```

Настройки провайдера создаются в runtime\opencode.json с локальным endpoint,
путём к ключу и лимитами модели. Запускается приватный сервер OpenCode
`--standalone`, чтобы не конфликтовать с его общей фоновой службой.
Прогрев не меняет порядок системного промпта; экспериментальное разбиение
системного сообщения из предыдущих измерений в рабочий протокол не включено.

## Проверки и ограничения проверки

`python -m unittest discover -s server/tests -v` проверяет индекс, вытеснение,
потоковый HTTP-прокси, блокировки между процессами, освобождение после аварии,
выбор GPU, инвалидацию кеша при изменении DLL и цикл save/restart/restore.
Интеграционный тест процессов использует маленький HTTP-стенд, а не модель.

CI запускает переносимые тесты на Windows и macOS, проверяет синтаксис PowerShell.
Отдельный ручной workflow собирает настоящий Vulkan backend на Windows.
Проверка GPU-вычислений и скорости требует реальной видеокарты пользователя;
успех unit-тестов и сборки не подтверждает численную корректность её драйвера.
