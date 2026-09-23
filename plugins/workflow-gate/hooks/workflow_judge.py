#!/usr/bin/env python3
"""Судья для ворот Workflow.

Вызывается на каждый запуск workflow. Показывает дешёвой модели последние сообщения
пользователя и паспорт предлагаемого workflow, и получает ответ, оправдан ли запуск.
Вердикт окончателен, в том числе против прямой просьбы: так решил владелец. Асимметрия
зашита в промпт — отказ дёшев (Клод сделает работу сам), ошибочное разрешение стоит
пользователю сотен тысяч токенов.
"""
import json
import os
import re
import subprocess

MODEL = os.environ.get('WORKFLOW_JUDGE_MODEL', 'claude-sonnet-5')
TIMEOUT_S = int(os.environ.get('WORKFLOW_JUDGE_TIMEOUT', '45'))
BLOCKED_TOOLS = 'Workflow Task Agent Bash Edit Write NotebookEdit WebFetch WebSearch Read'

PROMPT = """Ты привратник. Реши, оправдан ли запуск многоагентного workflow (фан-аут) \
для текущей задачи, или ассистент должен сделать её обычным способом, сам.

ПОСЛЕДНИЕ СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ (свежее сверху):
{messages}

ПРИЗНАКИ ПРОСЬБЫ: явная просьба развернуть агентов — {asked}; вызвана слэш-команда — {slash}.
Это сведения, а не разрешение. Раньше любой из них открывал ворота автоматически, и владелец
от этого отказался: признак ловится по трём последним сообщениям, поэтому сказанное для одной
задачи открывало ворота для следующей. Решаешь ты, и по существу задачи. Явная просьба —
весомый довод за запуск, но если работа мелкая, отказывай и ей: владелец знает, что продавить
можно пропуском `/workflow-pass`, и предпочитает лишний отказ лишней трате.

ПРЕДЛАГАЕМЫЙ WORKFLOW:
имя: {name}
описание: {description}
фаз: {phases}, вызовов agent(): {agents}, есть циклы по списку: {loops}

КРИТЕРИИ. Фан-аут оправдан только если верно хотя бы одно:
- объём физически не помещается в один контекст: правки в десятках мест, миграция по \
всему проекту, аудит большого куска кода, разбор многих независимых источников;
- нужна независимая перепроверка несколькими взглядами, и цена ошибки высока: деньги, \
права доступа, необратимые операции, релиз;
- пользователь просит исчерпывающий разбор, сравнение нескольких вариантов решения или \
проверку «на совесть».

Фан-аут НЕ нужен для: ответа на вопрос, поиска файла или причины ошибки, починки одного \
бага, слияния веток, прогона тестов, правки в пределах нескольких файлов, разбора одного \
модуля, рутинной операции с гитом или окружением.

ЭКОНОМИКА (замеры по реальным сессиям одного пользователя; порядок величин, не догма). \
Шаг агента впятеро дешевле шага ассистента, но запусков много: медианный workflow стоит \
как 97 шагов ассистента, даже самый мелкий на 2-3 агента — как 48. А медианная задача \
решается ассистентом за 9 шагов, и лишь 5% задач доходят до 97. Поэтому фан-аут окупается \
только на действительно крупной работе: прикинь, сколько шагов заняла бы задача обычным \
способом, и разрешай, если счёт идёт на сотню и больше, либо если нужна независимая \
перепроверка при высокой цене ошибки.

АСИММЕТРИЯ: ошибочный отказ стоит немного — ассистент сделает работу сам, чуть дольше. \
Ошибочное разрешение стоит примерно вдесятеро дороже, потому что большинство задач \
мелкие. Сомневаешься — отказывай.

Ответь строго одной строкой JSON, без пояснений и без markdown:
{{"verdict":"allow","reason":"<до 12 слов по-русски>"}}
или
{{"verdict":"deny","reason":"<до 12 слов по-русски>"}}"""


def passport(tool_input):
    """Паспорт предлагаемого запуска: что именно собираются развернуть."""
    script = tool_input.get('script') or ''
    name = tool_input.get('name') or ''
    description = ''
    phases = 0
    meta = re.search(r'export\s+const\s+meta\s*=\s*\{(.*?)\n\}', script, re.S)
    if meta:
        body = meta.group(1)
        got = re.search(r"name\s*:\s*['\"](.*?)['\"]", body)
        if got and not name:
            name = got.group(1)
        got = re.search(r"description\s*:\s*['\"](.*?)['\"]", body)
        if got:
            description = got.group(1)
        phases = len(re.findall(r"title\s*:", body))
    return {
        'name': name or '(без имени)',
        'description': description or '(описания нет)',
        'phases': phases or 1,
        'agents': len(re.findall(r'\bagent\s*\(', script)) or '?',
        'loops': 'да' if re.search(r'\b(while|for)\b|\.map\(', script) else 'нет',
    }


def ask(messages, tool_input, asked=False, slash=False):
    card = passport(tool_input)
    prompt = PROMPT.format(
        messages='\n'.join('- ' + m[:900].replace('\n', ' ') for m in messages) or '(нет)',
        asked='да' if asked else 'нет',
        slash='да' if slash else 'нет',
        **card)
    env = dict(os.environ, CLAUDE_WORKFLOW_GUARD_JUDGE='1')
    try:
        proc = subprocess.run(
            ['claude', '-p', prompt, '--model', MODEL, '--strict-mcp-config',
             '--disallowedTools', BLOCKED_TOOLS, '--output-format', 'json'],
            capture_output=True, text=True, timeout=TIMEOUT_S, cwd='/tmp', env=env)
    except subprocess.TimeoutExpired:
        return None, 'судья не ответил вовремя'
    except FileNotFoundError:
        return None, 'команда claude недоступна из хука'
    except Exception as err:
        return None, 'судью не удалось вызвать: %s' % type(err).__name__
    if proc.returncode != 0:
        return None, 'судья вернул ошибку'
    try:
        answer = json.loads(proc.stdout).get('result', '')
        found = re.search(r'\{.*?\}', answer, re.S)
        verdict = json.loads(found.group(0))
    except Exception:
        return None, 'ответ судьи не разобран'
    if verdict.get('verdict') not in ('allow', 'deny'):
        return None, 'судья ответил невнятно'
    return verdict['verdict'] == 'allow', (verdict.get('reason') or '').strip()[:120]
