---
name: stack-yii2
description: |
  Картридж стека Yii2 для ревью-агентов: команды тулинга (codeception, php-cs-fixer),
  пути и ключевые слова для триггеров auth/performance, и привязки универсального чек-листа
  к конкретике фреймворка — ActiveRecord, scenarios, AccessControl/RBAC, behaviors, миграции.
---

# Картридж: Yii2

Подключается при наличии файла `yii` в корне приложения либо `yiisoft/yii2` в `require`.
Раскладка бывает basic (`controllers/`, `models/`, `views/`) и advanced
(`frontend/`, `backend/`, `common/`, `console/`) — определи по факту, не предполагай.

## Тулинг

```bash
./vendor/bin/codecept run unit          # тесты (если настроен codeception)
php yii migrate/history                 # состояние миграций
./vendor/bin/php-cs-fixer fix --dry-run # стиль (если настроен)
./vendor/bin/phpstan analyse            # статанализ (если настроен)
```

Если тесты/линтеры не настроены — так и напиши в отчёте, не выдумывай прогон.

## Триггеры условных ревьюеров

**auth** — пути: `controllers/**`, `*/controllers/**`, `models/User*`, `models/*Form*`,
`components/**` (особенно `AccessRule`, `AuthManager`), `config/main.php`, `config/web.php`
(секции `user`, `authManager`, `as access`), `rbac/**`, миграции с таблицами `auth_*`.
Слова в дифе: `AccessControl`, `behaviors()`, `Yii::$app->user`, `->identity`, `can(`,
`checkAccess`, `matchCallback`, `accessToken`, `authKey`, `password_hash`, `beforeAction`.

**performance** — слова в дифе: `->all()`, `->one()` внутри цикла, отсутствие `with(`/`joinWith(`,
`asArray()`, `Pagination`, `batch(`/`each(`, `createIndex` в миграциях, `Yii::$app->cache`,
`ActiveDataProvider` без пагинации, `GridView` на больших данных.

## Привязки чек-листа

### Безопасность
- Сырой SQL: `createCommand($sql)` с конкатенацией, `where("col = $val")` строкой →
  только параметризованные (`['col' => $val]` или `:param` + `bindValue`).
- Mass assignment: `$model->load(Yii::$app->request->post())` без корректных `rules()` —
  в Yii2 присваиваются **только** атрибуты, попавшие в `rules()` активного сценария. Отсутствие
  правила = поле молча не сохранится; лишнее правило = дыра. Проверь `scenarios()` отдельно.
- Утечка полей: `$model->toArray()` / `->attributes` без `fields()`/`extraFields()` в API →
  наружу уезжают `password_hash`, `auth_key`, `access_token`.
- XSS во вьюхах: `echo $var` без `Html::encode()`; `Html::raw`/`raw` фильтр на пользовательских
  данных; `GridView` колонка с `'format' => 'raw'`.
- CSRF: `enableCsrfValidation = false` на контроллере/экшене — требуй обоснования.
- Загрузка файлов: `UploadedFile` без проверки расширения **и** MIME, сохранение в webroot.
- SSRF: серверный HTTP-запрос по URL из ввода без allowlist.
- Секреты: только `config/*-local.php` / `.env`; `cookieValidationKey` не в git.
- Логи: `Yii::info`/`Yii::error` с паролями, токенами, PII.

### Авторизация и доступ
- `AccessControl` в `behaviors()`: правила покрывают **все** экшены, а не часть; `'roles' => ['@']`
  означает лишь «залогинен», не «имеет право».
- IDOR: `findModel($id)` без скоупа по владельцу — классическая дыра Yii2-CRUD, сгенерированного
  Gii. Требуй `->andWhere(['user_id' => Yii::$app->user->id])` или проверку через RBAC.
- RBAC: `Yii::$app->user->can('updateOwnPost', ['post' => $post])` с правилом (`Rule`), а не
  голая проверка роли.
- `beforeAction` не подменяет собой проверку прав на конкретный объект.
- Rate limit: `RateLimiter` на login/восстановление пароля/OTP; `attemptsCount` и блокировка.
- Токены API: `access_token` в `User::findIdentityByAccessToken` — со сроком жизни и отзывом.

### Производительность
- N+1: обращение к связи в цикле или во вьюхе без `with()`/`joinWith()`. Особенно в `GridView`
  и `ListView` — там цикл не виден глазами.
- `ActiveDataProvider` без `pagination` (или с `'pagination' => false`) на растущих данных.
- `asArray()` там, где модели не нужны; `batch()`/`each()` вместо `all()` на больших выборках.
- Индексы: внешние ключи и колонки фильтров — `createIndex` в миграциях.
- Кэш: `Yii::$app->cache->getOrSet` на горячих чтениях; кэш зависимостей/фрагментов во вьюхах.
- Массовые вставки — `batchInsert()`, не `save()` в цикле.

### Качество и упрощение
- Валидация — в `rules()` модели/формы, не россыпью в контроллере; сценарии осмысленны.
- Толстый контроллер → сервис/компонент; повторяющиеся выборки → `ActiveQuery`-классы и scopes.
- Повторяющаяся логика моделей → behaviors (`TimestampBehavior`, `BlameableBehavior`) вместо копипаста.
- API-ответы — через `fields()`/`Serializer`, а не ручная сборка массива.
- Каждое изменение схемы — миграция (и `safeDown()`, который реально откатывает).
- Деньги — целые в минорных единицах, не float.
- Нет мёртвого кода, закомментированных блоков и отладочных `var_dump`/`VarDumper`.

### Дизайн-система
Применима, если есть вьюхи/виджеты. Для чистого API-бэкенда дизайн-ревьюер не запускается.
