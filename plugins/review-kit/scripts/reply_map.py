#!/usr/bin/env python3
"""Находит и сохраняет приватную карту ответов PR, общую для worktree и новых сессий."""

import argparse
from contextlib import contextmanager
import fcntl
import os
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

from review_storage import check_file, private_directory, read_bytes, read_json, write_bytes, write_json


class RevisionConflict(ValueError):
    """Карта изменилась: нужен load и сведение, а не повтор прежнего save."""


def canonical_pr(value):
    if not isinstance(value, str):
        raise ValueError('PR URL должен быть строкой')
    parsed = urlsplit(value)
    parts = parsed.path.strip('/').split('/')
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port or parsed.query or parsed.fragment or len(parts) != 4
            or parts[2] != 'pull' or not parts[3].isdigit() or int(parts[3]) < 1
            or any(part in {'.', '..'} or '%' in part or not part for part in parts)):
        raise ValueError('Нужен канонический https://HOST/OWNER/REPO/pull/NUMBER')
    return f'https://{parsed.hostname.lower()}/{parts[0]}/{parts[1]}/pull/{int(parts[3])}'


def map_path(repo, pr, create=False):
    pr = canonical_pr(pr)
    common = subprocess.run(
        ['git', '-C', str(repo), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    directory = Path(common) / 'review-kit-replies'
    if create or directory.exists() or directory.is_symlink():
        private_directory(directory, create=create)
    return directory / (hashlib.sha256(pr.encode()).hexdigest() + '.json')


def validate(payload, pr):
    pr = canonical_pr(pr)
    if (not isinstance(payload, dict) or type(payload.get('version')) is not int
            or payload['version'] != 1 or payload.get('pr') != pr
            or not isinstance(payload.get('items'), list)
            or type(payload.get('revision', 0)) is not int or payload.get('revision', 0) < 0):
        raise ValueError('Карта не соответствует версии, PR или структуре items')
    seen = set()
    for item in payload['items']:
        if not isinstance(item, dict):
            raise ValueError('Элемент карты должен быть объектом')
        for key in ('comment_id', 'thread_id'):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f'Нужен непустой {key}')
        if item['comment_id'] in seen:
            raise ValueError('Повтор comment_id в карте')
        seen.add(item['comment_id'])
        if item.get('state') not in ('pending', 'partial', 'local', 'published', 'skipped'):
            raise ValueError('Неизвестное состояние исправления')
        for key in ('source_body', 'source_updated_at', 'fix', 'proof_id', 'reply_id'):
            if key in item and not isinstance(item[key], str):
                raise ValueError(f'{key} должен быть строкой')
        for key in ('resolved', 'reply_verified'):
            if key in item and type(item[key]) is not bool:
                raise ValueError(f'{key} должен быть boolean')
        if 'checks' in item and not isinstance(item['checks'], (str, list)):
            raise ValueError('checks должен быть строкой или массивом')
        for key in ('comment_url', 'reply_url'):
            if item.get(key) is None:
                continue
            url = item[key]
            if not isinstance(url, str) or not url.startswith(pr + '#discussion_r'):
                raise ValueError(f'{key} не относится к этому PR')
            if not url[len(pr + '#discussion_r'):].isdigit():
                raise ValueError(f'Неверная ссылка {key}')
    return payload


def load_map(repo, pr):
    path = map_path(repo, pr)
    if not path.exists() and not path.is_symlink():
        return {'version': 1, 'pr': canonical_pr(pr), 'items': []}
    return validate(read_json(path), pr)


@contextmanager
def locked_map(repo, pr):
    path = map_path(repo, pr, create=True)
    descriptor = os.open(path.with_suffix('.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+', encoding='utf-8') as lock:
        check_file(os.fstat(lock.fileno()), path.with_suffix('.lock'))
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield path


def save_map(repo, pr, payload):
    payload = validate(payload, pr)
    with locked_map(repo, pr) as path:
        current = load_map(repo, pr)
        revision = current.get('revision', 0)
        if payload.get('revision', 0) != revision:
            raise RevisionConflict('Карта изменена другой сессией: перечитай и сведи items по comment_id')
        updated = {**payload, 'revision': revision + 1}
        write_json(path, updated)
        payload['revision'] = updated['revision']
    return path


def repair_map(repo, pr, payload):
    """Ремонт схемы v1: сохраняет исходник и все ID, не подтверждает факты GitHub."""
    validate(payload, pr)
    with locked_map(repo, pr) as path:
        original = read_bytes(path)
        current = json.loads(original.decode('utf-8'))
        if not isinstance(current, dict) or not isinstance(current.get('items'), list):
            raise ValueError('Ремонт требует JSON-объект с исходным массивом items')
        # Проверяем версию, привязку PR и revision даже у карты со старой схемой items.
        validate({**current, 'items': []}, pr)
        revision = current.get('revision', 0)
        if payload.get('revision', 0) != revision:
            raise RevisionConflict('Карта изменена: подготовь ремонт по актуальной revision')
        try:
            validate(current, pr)
        except ValueError:
            pass
        else:
            raise ValueError('Карта уже валидна: используй load/save')
        previous = {}
        for item in current['items']:
            comment_id = item.get('comment_id') if isinstance(item, dict) else None
            if not isinstance(comment_id, str) or not comment_id.strip() or comment_id in previous:
                raise ValueError('Нельзя автоматически восстановить отсутствующие/повторные comment_id')
            previous[comment_id] = item
        if not previous.keys() <= {item['comment_id'] for item in payload['items']}:
            raise ValueError('Ремонт не может удалить исходные comment_id')
        updated = {**current, **payload, 'revision': revision + 1, 'items': [
            {**previous.get(item['comment_id'], {}), **item} for item in payload['items']
        ]}
        validate(updated, pr)
        descriptor, backup = tempfile.mkstemp(prefix=path.stem + '.backup-', suffix='.json', dir=path.parent)
        os.close(descriptor)
        write_bytes(backup, original)
        write_json(path, updated)
        payload.clear()
        payload.update(updated)
    return path, Path(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('path', 'load', 'save', 'repair'))
    parser.add_argument('--repo', default='.', help='Локальный каталог git-репозитория')
    parser.add_argument('--pr', required=True, help='Канонический URL PR')
    parser.add_argument('--input', type=Path, help='Приватный JSON для save/repair')
    args = parser.parse_args()
    if (args.command in ('save', 'repair')) != bool(args.input):
        parser.error('--input нужен только для save/repair')
    try:
        if args.command == 'load':
            print(json.dumps(load_map(args.repo, args.pr), ensure_ascii=False, indent=2))
        elif args.command in ('save', 'repair'):
            payload = read_json(args.input)
            if args.command == 'repair':
                path, backup = repair_map(args.repo, args.pr, payload)
                result = {'path': str(path), 'revision': payload['revision'], 'backup': str(backup)}
            else:
                path = save_map(args.repo, args.pr, payload)
                result = {'path': str(path), 'revision': payload['revision']}
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(map_path(args.repo, args.pr))
    except RevisionConflict as error:
        parser.exit(3, f'review-kit reply-map: {error}\n')
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'review-kit reply-map: {error}\n')


if __name__ == '__main__':
    main()
