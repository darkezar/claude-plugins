"""Приватные JSON-артефакты: запрет симлинков, проверка владельца, атомарная запись."""

import json
import os
from pathlib import Path
import stat
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


def read_json(path):
    path = Path(path)
    private_directory(path.parent)
    check_file(path.lstat(), path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        check_file(os.fstat(stream.fileno()), path)
        return json.load(stream)


def write_json(path, payload):
    path = Path(path)
    private_directory(path.parent)
    try:
        check_file(path.lstat(), path)
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix='.review-kit-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
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
