---
name: stack-laravel
description: |
  Картридж стека Laravel для ревью-агентов: команды тулинга (Pint, PHPStan, artisan test),
  пути и ключевые слова для триггеров auth/performance, и привязки универсального чек-листа
  к конкретике фреймворка — Form Requests, API Resources, policies/gates, Eloquent, миграции.
---

# Картридж: Laravel

Подключается, когда рядом с `composer.json` есть `artisan` либо в `require` присутствует
`laravel/framework`. Версию фреймворка возьми из `composer.json` — не полагайся на память.

## Тулинг

```bash
php artisan test                 # feature/unit тесты
./vendor/bin/pint --test         # стиль PSR-12, без правки файлов
./vendor/bin/phpstan analyse     # статанализ (если настроен larastan/phpstan)
```

Запускать из каталога с `artisan`. Если инструмент не установлен — не выдумывай результат,
напиши «не настроен» в отчёте.

## Триггеры условных ревьюеров

**auth** — пути: `app/Http/Controllers/**` (особенно Auth/Session/Payment/Order), `app/Policies/**`,
`app/Http/Middleware/**`, `routes/api.php`, `routes/web.php`, `config/auth.php`, `config/sanctum.php`,
миграции и модели сущностей, принадлежащих пользователю.
Слова в дифе: `Sanctum`, `Passport`, `auth:`, `Gate::`, `->authorize(`, `Policy`, `middleware(`,
`abort(403`, `Auth::`, `token`, `password`, `otp`.

**performance** — слова в дифе: `->get()`, `->all()`, `->first()` внутри `foreach`, отсутствие
`with(`/`load(`, `paginate(`, `chunk(`, `cursor(`, `Cache::`, `DB::table`, `index(` в миграциях,
`broadcast(`, новые связи в моделях, ресурсы списков.

## Привязки чек-листа

### Безопасность
- Сырой SQL: `DB::raw`, `whereRaw`, `havingRaw`, `orderByRaw` с пользовательским вводом →
  только биндинги/Eloquent.
- Mass assignment: `Model::create($request->all())`, `->fill($request->all())`, отсутствие
  `$fillable`/`$guarded`, приём служебных полей (`user_id`, `role`, `status`, суммы) из тела.
- Утечка в ответе: `return $model` / `return $collection` без API Resource; чувствительные поля
  в Resource; `APP_DEBUG=true` в проде.
- SSRF: `Http::get($userUrl)`, `file_get_contents` по вводу без allowlist хоста и схемы.
- Редирект: `redirect($request->input(...))` без whitelist.
- Секреты: только `.env`, ключи без значений в `.env.example`; ничего в коде, миграциях, сидах.
- Логи: `Log::` / `dd()` / `dump()` с токенами, паролями, платёжными данными, PII.
- Десериализация: `unserialize()` над недоверенными данными.

### Авторизация и доступ
- Каждый роут за `auth:sanctum` / `auth:api` / `auth` — проверь `routes/*.php` и группы middleware.
- IDOR: ресурс по `id` из URL без policy или без скоупа по владельцу
  (`->where('user_id', $request->user()->id)`, `$this->authorize('view', $model)`).
- Route model binding без scoping — `Route::get('/orders/{order}')` отдаёт чужой заказ,
  если нет policy или `->scopeBindings()`.
- «Залогинен» проверено, а «имеет право» — нет.
- Rate limit: `throttle:` на login/OTP/восстановление пароля/привязку платёжки.
- Logout реально отзывает токен на сервере (`tokens()->delete()`), а не только чистит клиент.

### Производительность
- N+1: обращение к связи внутри цикла или в Resource без `with()`/`load()`. Ищи в контроллерах,
  ресурсах и Blade-шаблонах.
- Непагинированные списки: `->get()`/`->all()` там, где данные растут → `paginate()`/`cursorPaginate()`.
- Индексы: внешние ключи и колонки фильтрации/сортировки — проверь миграции на `index()`/`unique()`.
- Кэш: горячие read-эндпоинты через `Cache::remember` с внятной инвалидацией.
- Realtime вместо поллинга: broadcasting (Reverb/Pusher) на часто меняющиеся данные.
- Массовые операции: `insert()`/`upsert()` батчем вместо запроса на строку; `chunkById` на больших выборках.
- Очереди: тяжёлая работа (почта, экспорт, вебхуки) в jobs, а не в цикле запроса.

### Качество и упрощение
- Валидация — Form Request, не инлайн в контроллере; повторяющиеся правила → общий Request.
- Ответы — API Resource, не ручная сборка массива.
- Авторизация — policies/gates, не гирлянда `if` в контроллере.
- Толстый контроллер → сервис/экшен; повторяющиеся выборки → Eloquent scopes.
- Каждое изменение схемы — миграция; каждый эндпоинт — feature-тест (успех, валидация, 403).
- Деньги — целые в минорных единицах, никаких float.
- PSR-12; тонкие контроллеры; нет мёртвого кода и отладочных вызовов.

### Дизайн-система
Применима только если в проекте есть Blade/Livewire/Inertia-вёрстка. Для чистого API-бэкенда
дизайн-ревьюер не запускается.
