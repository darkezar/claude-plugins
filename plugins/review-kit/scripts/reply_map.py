#!/usr/bin/env python3
"""Находит и сохраняет приватную карту ответов PR, общую для worktree и новых сессий."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from review_storage import private_directory, read_json, write_json


def canonical_pr(value):
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
    if (not isinstance(payload, dict) or payload.get('version') != 1
            or payload.get('pr') != canonical_pr(pr) or not isinstance(payload.get('items'), list)
            or any(not isinstance(item, dict) for item in payload['items'])):
        raise ValueError('Карта не соответствует версии, PR или структуре items')
    return payload


def load_map(repo, pr):
    path = map_path(repo, pr)
    if not path.exists() and not path.is_symlink():
        return {'version': 1, 'pr': canonical_pr(pr), 'items': []}
    return validate(read_json(path), pr)


def save_map(repo, pr, payload):
    payload = validate(payload, pr)
    path = map_path(repo, pr, create=True)
    write_json(path, payload)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('path', 'load', 'save'))
    parser.add_argument('--repo', default='.')
    parser.add_argument('--pr', required=True, help='Канонический URL PR')
    parser.add_argument('--input', type=Path, help='Приватный JSON, только для save')
    args = parser.parse_args()
    if (args.command == 'save') != bool(args.input):
        parser.error('--input нужен только для save')
    try:
        if args.command == 'load':
            print(json.dumps(load_map(args.repo, args.pr), ensure_ascii=False, indent=2))
        elif args.command == 'save':
            print(save_map(args.repo, args.pr, read_json(args.input)))
        else:
            print(map_path(args.repo, args.pr))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'review-kit reply-map: {error}\n')


if __name__ == '__main__':
    main()
