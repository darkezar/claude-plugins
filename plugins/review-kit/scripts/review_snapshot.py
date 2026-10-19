#!/usr/bin/env python3
"""Фиксирует предмет ревью и хранит результаты завершённых локальных прогонов."""

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VERDICTS = {"READY TO PR", "READY AFTER FIXES", "NEEDS WORK"}


def git(repo, *args):
    """Выполняет git без shell и возвращает байты, сохраняя имена файлов буквально."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    ).stdout


def digest(parts):
    """Хеширует последовательность с разделением элементов по длине."""
    result = hashlib.sha256()
    for part in parts:
        result.update(len(part).to_bytes(8, "big"))
        result.update(part)
    return result.hexdigest()


def file_digest(path):
    """Хеширует файл потоком; для симлинка учитывает ссылку и содержимое цели."""
    result = hashlib.sha256()
    if path.is_symlink():
        result.update(str(path.readlink()).encode())
    if path.is_file():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(chunk)
    return result.digest()


def files_digest(root, paths):
    """Считает отпечаток содержимого и относительных имён выбранных файлов."""
    parts = []
    for path in sorted(set(paths)):
        parts.extend([str(path.relative_to(root)).encode(), file_digest(path)])
    return digest(parts)


def log_path(repo):
    """Возвращает общий для worktree журнал, не попадающий в diff проекта."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common.decode().strip()) / "review-kit-codex.jsonl"


def history(repo, target):
    """Читает завершённые прогоны цели, пропуская повреждённые строки."""
    path = log_path(repo)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("snapshot"), dict)
                and entry["snapshot"].get("target") == target
                and isinstance(entry.get("report"), dict)
                and entry["report"].get("verdict") in VERDICTS
            ):
                entries.append(entry)
        except (ValueError, TypeError):
            continue
    return entries


def capture(repo, base, head="HEAD", target=None, scope="auto", rules=()):
    """Фиксирует обе стороны сравнения, дерево, правила и версию инструкций."""
    repo = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip())
    base_sha = git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    head_sha = git(repo, "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}")
    base_sha, head_sha = base_sha.decode().strip(), head_sha.decode().strip()
    current = git(repo, "rev-parse", "HEAD").decode().strip()
    if current != head_sha:
        raise ValueError("Рабочее дерево должно быть открыто на проверяемом head SHA")
    merge_base = git(repo, "merge-base", base_sha, head_sha).decode().strip()
    branch = git(repo, "branch", "--show-current").decode().strip()
    target = target or branch or "detached"
    status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    diff = git(repo, "diff", "--binary", "HEAD", "--")
    staged = git(repo, "diff", "--cached", "--binary", "HEAD", "--")
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    paths = [repo / name.decode() for name in untracked.split(b"\0") if name]
    tree_hash = digest([status, diff, staged, files_digest(repo, paths).encode()])

    names = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    rule_paths = {
        repo / name.decode()
        for name in names.split(b"\0")
        if name and Path(name.decode()).name in {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md"}
    }
    extra_rules = []
    for name in rules:
        path = Path(name)
        path = path if path.is_absolute() else repo / path
        path = path.absolute()
        path.relative_to(repo)
        if not path.is_file():
            raise ValueError(f"Не найден файл правил: {path}")
        extra_rules.append(str(path.relative_to(repo)))
        rule_paths.add(path)
    package_files = [
        path for path in PLUGIN_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".md", ".json", ".yaml", ".py"}
    ]
    snapshot = {
        "target": target,
        "scope": ",".join(sorted(set(scope.split(",")))),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "dirty": bool(status),
        "tree_hash": tree_hash,
        "rules_hash": files_digest(repo, rule_paths),
        "plugin_hash": files_digest(PLUGIN_ROOT, package_files),
        "rules": sorted(extra_rules),
    }
    snapshot["key"] = digest([json.dumps(snapshot, sort_keys=True).encode()])
    previous = history(repo, target)
    last = previous[-1] if previous else None
    mode = "first"
    if last:
        old = last["snapshot"]
        if old.get("key") == snapshot["key"]:
            mode = "unchanged"
        elif (
            not snapshot["dirty"] and old.get("dirty") is False
            and all(old.get(key) == snapshot[key] for key in (
                "base_sha", "rules_hash", "plugin_hash", "scope", "rules"
            ))
            and isinstance(old.get("head_sha"), str)
        ):
            ancestor = subprocess.run(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor",
                 old["head_sha"], head_sha], capture_output=True
            )
            if ancestor.returncode == 0:
                mode = "followup"
    return {"snapshot": snapshot, "mode": mode, "previous": last}


def record(repo, snapshot, report):
    """Записывает результат лишь для неизменившегося предмета завершённого ревью."""
    if report.get("verdict") not in VERDICTS:
        raise ValueError("Неизвестный вердикт")
    for key in ("findings", "open", "checks"):
        if not isinstance(report.get(key), list):
            raise ValueError(f"Поле {key} должно быть массивом")
    current = capture(
        repo, snapshot["base_sha"], snapshot["head_sha"], snapshot["target"],
        snapshot["scope"], snapshot["rules"]
    )["snapshot"]
    if current["key"] != snapshot["key"]:
        raise ValueError("Код, правила или пакет изменились во время ревью; запись отклонена")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot,
        "report": report,
    }
    path = log_path(repo)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"log": str(path), "key": snapshot["key"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    take = commands.add_parser("capture", help="Снять состояние для ревью")
    take.add_argument("--repo", default=".")
    take.add_argument("--base", required=True)
    take.add_argument("--head", default="HEAD")
    take.add_argument("--target")
    take.add_argument("--scope", default="auto")
    take.add_argument("--rules", action="append", default=[])
    save = commands.add_parser("record", help="Записать завершённый прогон")
    save.add_argument("--repo", default=".")
    save.add_argument("--snapshot", required=True)
    save.add_argument("--report", required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            result = capture(args.repo, args.base, args.head, args.target, args.scope, args.rules)
        else:
            data = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
            result = record(args.repo, data["snapshot"], report)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"review-kit: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
