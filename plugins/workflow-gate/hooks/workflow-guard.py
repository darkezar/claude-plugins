#!/usr/bin/env python3
"""Ворота для инструмента Workflow.

Фан-аут агентов — основная статья расхода лимита, поэтому инструмент закрыт по
умолчанию, а каждый запуск проходит проверку.

Решает судья — отдельная дешёвая сессия, которая смотрит на задачу и паспорт
предлагаемого запуска. Его зовут на **каждый** запуск, и его вердикт окончателен:
явная просьба и слэш-команда идут судье как сведения, а не как разрешение. Раньше
любой из этих признаков открывал ворота сразу, но признак ловился по последним трём
сообщениям — сказанное для одной задачи открывало ворота для следующей.

Единственный обход — временный пропуск ~/.claude/workflow-allow (`/workflow-pass
<часов>`): при нём судью не зовут вовсе. Судья недоступен или ответил невнятно — отказ.

Отказ не блокирует работу: Клод просто делает задачу обычным способом, поэтому ночные
прогоны не встают из-за вопроса.

Переменные окружения:
  WORKFLOW_GATE_JUDGE=off   не звать судью вовсе; тогда ворота отказывают всем, и
                            единственным проходом остаётся /workflow-pass
  WORKFLOW_JUDGE_MODEL      модель судьи, по умолчанию claude-sonnet-5
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workflow_judge as judge

HOME = os.path.expanduser('~')
PASS_FILE = os.path.join(HOME, '.claude', 'workflow-allow')
LOG_FILE = os.path.join(HOME, '.claude', 'workflow-guard.log')

# Явная просьба об оркестрации. Список намеренно узкий: обсуждение агентов
# («агентов стало больше») не должно открывать ворота.
ASK = re.compile(
    r'workflow|воркфлоу|ultracode|ультракод|ultrareview|фан-?аут|fan-?out|оркестр|мультиагент'
    r'|(?:запусти|разверни|разворачивай|используй|включи|подключи|давай)\s+\S{0,12}\s*'
    r'(?:агент|субагент|помощник|параллел)',
    re.IGNORECASE)
SLASH = re.compile(r'<command-name>')
# Текст, который приходит в роли пользователя, но написан не им.
INJECTED = ('<system-reminder>', '<task-notification>', '<local-command',
            'Caveat: The messages below', 'Base directory for this skill',
            'is already loaded above', '[Workflow harness',
            'This session is being continued')
LOOKBACK_MESSAGES = 3
LOOKBACK_RECORDS = 400


def decide(reason, allow):
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow' if allow else 'deny',
        'permissionDecisionReason': reason,
    }}, ensure_ascii=False))


def pass_valid():
    try:
        until = float(open(PASS_FILE).read().strip())
    except Exception:
        return False
    return time.time() < until


def user_texts(path):
    """Последние сообщения, написанные человеком, от свежих к старым."""
    try:
        with open(path, 'r', errors='replace') as fh:
            lines = fh.readlines()[-LOOKBACK_RECORDS:]
    except Exception:
        return None
    asked = set()
    for line in lines:
        if '"AskUserQuestion"' in line:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            content = (rec.get('message') or {}).get('content')
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('name') == 'AskUserQuestion':
                        asked.add(block.get('id'))
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get('type') != 'user' or rec.get('isSidechain'):
            continue
        content = (rec.get('message') or {}).get('content')
        text = ''
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text':
                    text += ' ' + block.get('text', '')
                elif block.get('type') == 'tool_result' and block.get('tool_use_id') in asked:
                    text += ' ' + json.dumps(block.get('content'), ensure_ascii=False)
        text = text.strip()
        if not text:
            continue
        if not SLASH.search(text) and any(mark in text for mark in INJECTED):
            continue
        out.append(text)
        if len(out) >= LOOKBACK_MESSAGES:
            break
    return out


def log(decision, why, payload):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a') as fh:
            fh.write('%s\t%s\t%s\t%s\n' % (
                time.strftime('%Y-%m-%d %H:%M:%S'), decision, why,
                (payload.get('tool_input') or {}).get('name') or 'inline-script'))
    except Exception:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get('tool_name') != 'Workflow':
        return 0

    # Судья сам работает через `claude -p`; из его сессии фан-аут запрещён наглухо,
    # иначе ворота могли бы звать сами себя.
    if os.environ.get('CLAUDE_WORKFLOW_GUARD_JUDGE'):
        decide('Вложенный запуск workflow из сессии судьи запрещён.', False)
        return 0

    if pass_valid():
        log('allow', 'временный пропуск', payload)
        decide('Открыт временный пропуск на workflow (~/.claude/workflow-allow).', True)
        return 0

    texts = user_texts(payload.get('transcript_path') or '')
    if texts is None:
        log('deny', 'транскрипт не прочитан', payload)
        decide('Не удалось прочитать историю сессии, поэтому фан-аут не разрешён. '
               'Сделай задачу обычным способом.', False)
        return 0

    asked = any(ASK.search(text) for text in texts)
    slash = any(SLASH.search(text) for text in texts)
    mark = ('просьба' if asked else '') + ('+команда' if slash else '') or 'без просьбы'

    if os.environ.get('WORKFLOW_GATE_JUDGE', '').lower() in ('0', 'off', 'no', 'false'):
        log('deny', 'судья выключен (%s)' % mark, payload)
        decide('Судья выключен настройкой, а без него ворота отказывают всем. '
               'Сделай задачу обычным способом; открыть проход — /workflow-pass.', False)
        return 0

    allowed, why = judge.ask(texts, payload.get('tool_input') or {}, asked, slash)
    if allowed is None:
        log('deny', 'судья недоступен: %s (%s)' % (why, mark), payload)
        decide('Фан-аут отклонён: %s. Сделай задачу обычным способом. Если фан-аут нужен '
               'прямо сейчас, скажи пользователю, что судья не отвечает, и предложи '
               'открыть пропуск: /workflow-pass.' % why, False)
        return 0
    if allowed:
        log('allow', 'судья: %s (%s)' % (why, mark), payload)
        decide('Судья счёл фан-аут оправданным: %s. Скажи пользователю одной строкой, '
               'зачем разворачиваешь агентов.' % why, True)
        return 0

    log('deny', 'судья: %s (%s)' % (why, mark), payload)
    if asked or slash:
        decide(
            'Фан-аут отклонён судьёй, хотя просьба была: %s. Так решил пользователь — '
            'судья теперь решает всё, включая прямые просьбы. Сделай задачу обычным '
            'способом и **обязательно скажи пользователю первой строкой**, что его '
            'просьбу отклонили и по какой причине: иначе он не поймёт, почему агентов '
            'нет. Продавить можно только пропуском: /workflow-pass.' % why, False)
        return 0
    decide(
        'Фан-аут отклонён судьёй: %s. Сделай задачу обычным способом. Если уверен, что '
        'здесь нужен именно фан-аут, объясни пользователю зачем и какой ценой и предложи '
        'открыть пропуск: /workflow-pass.' % why, False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
