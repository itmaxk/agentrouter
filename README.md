# agentrouter

Фоновый монитор доступности моделей [AgentRouter](https://agentrouter.org). Регулярно проверяет каждую модель реальным запросом в 1 токен, хранит историю «когда была доступна и сколько» и держит **opencode** и **Hermes** на рабочей модели. Переключает их заранее, если по накопленной истории модель обычно падает в ближайшие полчаса.

Только Python stdlib (для Hermes нужен PyYAML, он уже есть в системном Python).

## Установка

```sh
gh repo clone itmaxk/agentrouter ~/Projects/agentrouter
~/Projects/agentrouter/install.sh            # ставит ~/.local/bin/agentrouter и systemd user unit
agentrouter check                            # разовая проверка всех моделей
systemctl --user enable --now agentrouter    # запуск в фоне
```

Ключ берётся из `AGENTROUTER_API_KEY` или `~/.local/share/opencode/auth.json` (`opencode auth login`).

## Команды

| Команда | Что делает |
|---|---|
| `agentrouter run [--dry-run] [--no-web] [--simulate-down M...]` | демон + дашборд; `--dry-run` только пишет в лог, что бы переключил |
| `agentrouter status` | на какой модели opencode и Hermes, статус каждой модели и как давно он держится, доступность за 24ч, прогноз на 30 мин |
| `agentrouter stats [--period 7d]` | % доступности, сколько была доступна и недоступна, число падений, средний аптайм и простой |
| `agentrouter history [MODEL] [--since 2d]` | интервалы UP/DOWN |
| `agentrouter predict [--hours 24]` | прогноз доступности по часам |
| `agentrouter check [MODEL...]` | проверить сейчас |
| `agentrouter switch MODEL [--target opencode\|hermes]` | переключить вручную |
| `agentrouter pin MODEL` / `unpin` | закрепить модель (авто-переключение выключено) / вернуть авто |
| `agentrouter init` | записать конфиг по умолчанию в `~/.config/agentrouter/config.json` |

У `status`, `stats`, `history` и `predict` есть флаг `--json`.

Дашборд: <http://127.0.0.1:18778>. На нём таймлайн доступности, статистика, карта доступности по часам недели и журнал переключений. JSON-API: `/api/status`, `/api/stats?period=7d`, `/api/history?model=&period=2d`, `/api/predict?hours=24`, `/api/switches`.

## Как это работает

**Проверки.** Список моделей обновляется из `GET /v1/models` раз в 30 минут. Каждую модель проверяет запрос `chat/completions` с `max_tokens=1`. Если модель отвечает, что chat ей не поддерживается, используется `/v1/responses` (так работают GPT-модели), и выбранный API запоминается. Частота проверок зависит от роли модели: та, на которой сейчас сидит opencode или Hermes, проверяется раз в минуту, ближайшие кандидаты раз в 5 минут, остальные раз в 20 минут. Смена статуса засчитывается после двух одинаковых результатов подряд, повторная проверка идёт через 20 секунд. 429 и 402 (лимит или исчерпан бюджет) считаются недоступностью. 401/403 говорят о проблеме с ключом, а не с моделью, и в историю не попадают.

AgentRouter принимает запросы только от известных клиентов, поэтому проверки идут с `User-Agent: opencode/<версия установленного opencode>`. Значение можно задать параметром `user_agent`.

**История** хранится в `~/.local/share/agentrouter/state.db` (SQLite) в виде непрерывных интервалов UP/DOWN. Сырые результаты проверок хранятся 30 дней. Если демон долго не работал, в истории остаётся разрыв без данных, а не выдуманная доступность.

**Прогноз.** Для каждой модели считается вероятность доступности по часу суток и по часу недели. Каждые 5 минут истории добавляют вес в свою корзину, старые данные затухают с полураспадом 14 дней. Час недели сглаживается к часу суток, а тот к общей доступности модели. Поэтому суточный паттерн появляется уже через несколько дней, а недельный уточняет его. На ближайшие минуты прогноз опирается на текущий статус, дальше переходит к выученному паттерну.

**Выбор модели.** Оценка модели — вес приоритета (порядок в `priority`), умноженный на худшую ожидаемую доступность в окне `lookahead_min` (30 мин).
- Текущая модель упала → сразу переходим на лучшую доступную, кулдаун при этом не действует.
- У другой модели оценка выше на `margin` (15%) → переходим. Если причина в том, что у текущей впереди прогнозируемое падение, в журнале будет `predicted`: это переключение заранее.
- На модель, которая только что восстановилась, возвращаемся, только когда она проработает `min_stable` (10 мин).
- Между переключениями выдерживается `cooldown` (5 мин), кроме случая падения.

**Куда пишет.**
- *opencode* (`~/.config/opencode/opencode.jsonc`): меняется только значение `"model"`, и только если там уже стоит провайдер `agentrouter/…`. Комментарии и форматирование сохраняются. Если модели нет в каталоге models.dev, она добавляется в `provider.agentrouter.models`. opencode читает конфиг при старте, поэтому новая модель действует для новых сессий. В opencode попадают только модели с chat API.
- *Hermes* (`~/.hermes/config.yaml`): первым элементом `fallback_providers` ставится `{provider: agentrouter, model: …, api_mode: …}`. Остальные элементы цепочки сохраняются после него. При первом запуске добавляется блок `providers.agentrouter`: локальный прокси `client_base_url`, `key_cmd: agentrouter key` и `extra_headers.User-Agent`. Остальной файл не меняется. Hermes перечитывает цепочку fallback на каждом ходе, перезапуск не нужен.

Перед каждой записью делается копия в `~/.local/share/agentrouter/backups/` (хранятся последние 20), запись атомарная.

## Конфиг

`agentrouter init` создаёт `~/.config/agentrouter/config.json` со всеми параметрами. Основные:

```json
{
  "priority": ["claude-opus-5-5", "gpt-6.1-sol", "deepseek-v4-flash", "gpt-6-astra"],
  "exclude": [],
  "intervals": {"current": 60, "candidate": 300, "other": 1200},
  "selector": {"lookahead_min": 30, "proactive_threshold": 0.5, "cooldown": 300, "min_stable": 600},
  "targets": {"opencode": {"enabled": true}, "hermes": {"enabled": true, "keep_existing": true}}
}
```

## Разработка

```sh
python3 -m unittest discover -s tests -t .
agentrouter --db /tmp/t.db run --dry-run --simulate-down claude-opus-5-5
journalctl --user -u agentrouter -f
```
