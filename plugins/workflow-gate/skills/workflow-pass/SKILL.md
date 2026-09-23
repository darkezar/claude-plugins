---
name: workflow-pass
description: Открыть или закрыть временное разрешение на запуск workflow (фан-аут агентов). Использовать, когда пользователь просит на время разрешить разворачивать агентов свободно либо отменить такое разрешение.
---

# Пропуск на workflow

Ворота плагина workflow-gate по умолчанию запрещают инструмент Workflow.
Этот скилл открывает их на заданное время — чтобы длинная работа не упиралась в отказ.

Пропуск живёт в `~/.claude/workflow-allow` и хранит одно число — unix-время,
до которого он действует. Файл общий для всех проектов на машине.

## Открыть

Срок берётся из аргумента: число часов (`/workflow-pass 3`). Без аргумента — 2 часа.
Больше 12 часов не открывать: смысл пропуска в том, что он закрывается сам.
Подставь число часов вместо `2` в первой строке.

```bash
HOURS=2
python3 - "$HOURS" <<'PY'
import os, sys, time
hours = float(sys.argv[1])
path = os.path.expanduser('~/.claude/workflow-allow')
os.makedirs(os.path.dirname(path), exist_ok=True)
until = time.time() + hours * 3600
open(path, 'w').write(str(until))
print('пропуск открыт до', time.strftime('%H:%M', time.localtime(until)))
PY
```

## Закрыть

Если пользователь просит закрыть или отменить:

```bash
rm -f ~/.claude/workflow-allow && echo "пропуск закрыт"
```

## Посмотреть состояние

```bash
python3 - <<'PY'
import os, time
path = os.path.expanduser('~/.claude/workflow-allow')
if not os.path.exists(path):
    print('пропуск закрыт')
else:
    until = float(open(path).read().strip())
    print('открыт до', time.strftime('%H:%M', time.localtime(until))
          if until > time.time() else 'пропуск истёк')
PY
tail -5 ~/.claude/workflow-guard.log 2>/dev/null
```

После действия скажи пользователю одной строкой, что изменилось и до какого времени.
