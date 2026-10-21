"""Приватные JSON-артефакты: запрет симлинков, проверка владельца, атомарная запись."""

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def private_directory(path, create=False):
    path = Path(path).absolute()
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError(f'Нужен собственный каталог 0700 без симлинка: {path}')
    return path


def check_file(info, path):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError(f'Нужен собственный JSON-файл 0600 без симлинка: {path}')


def read_bytes(path):
    path = Path(path)
    private_directory(path.parent)
    check_file(path.lstat(), path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        check_file(os.fstat(stream.fileno()), path)
        return stream.read()


def read_json(path):
    return json.loads(read_bytes(path).decode('utf-8'))


def write_bytes(path, content):
    path = Path(path)
    private_directory(path.parent)
    try:
        check_file(path.lstat(), path)
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix='.review-kit-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, payload):
    write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('write',))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        # Вход — JSON-данные, без выполнения Python-кода из проверяемого проекта.
        write_json(args.output, json.load(sys.stdin.buffer))
    except (OSError, ValueError) as error:
        parser.exit(1, f'review-kit storage: {error}\n')
    print(json.dumps({'path': str(args.output.resolve())}))


if __name__ == '__main__':
    main()
