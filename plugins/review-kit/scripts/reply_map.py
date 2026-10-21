#!/usr/bin/env python3
"""Находит и сохраняет приватную карту ответов PR, общую для worktree и новых сессий."""

import argparse
import fcntl
import os
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from review_storage import check_file, private_directory, read_json, write_json


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


def save_map(repo, pr, payload):
    payload = validate(payload, pr)
    path = map_path(repo, pr, create=True)
    descriptor = os.open(path.with_suffix('.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+', encoding='utf-8') as lock:
        check_file(os.fstat(lock.fileno()), path.with_suffix('.lock'))
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = load_map(repo, pr)
        revision = current.get('revision', 0)
        if payload.get('revision', 0) != revision:
            raise RevisionConflict('Карта изменена другой сессией: перечитай и сведи items по comment_id')
        updated = {**payload, 'revision': revision + 1}
        write_json(path, updated)
        payload['revision'] = updated['revision']
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('path', 'load', 'save'))
    parser.add_argument('--repo', default='.', help='Локальный каталог git-репозитория')
    parser.add_argument('--pr', required=True, help='Канонический URL PR')
    parser.add_argument('--input', type=Path, help='Приватный JSON, только для save')
    args = parser.parse_args()
    if (args.command == 'save') != bool(args.input):
        parser.error('--input нужен только для save')
    try:
        if args.command == 'load':
            print(json.dumps(load_map(args.repo, args.pr), ensure_ascii=False, indent=2))
        elif args.command == 'save':
            payload = read_json(args.input)
            path = save_map(args.repo, args.pr, payload)
            print(json.dumps({'path': str(path), 'revision': payload['revision']}, ensure_ascii=False))
        else:
            print(map_path(args.repo, args.pr))
    except RevisionConflict as error:
        parser.exit(3, f'review-kit reply-map: {error}\n')
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'review-kit reply-map: {error}\n')


if __name__ == '__main__':
    main()
