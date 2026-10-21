#!/usr/bin/env python3
"""Фиксирует предмет ревью и хранит результаты завершённых локальных прогонов."""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VERDICTS = {"READY TO PR", "READY AFTER FIXES", "NEEDS WORK"}
RULE_NAMES = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md")
# Файлы пакета, от которых зависит содержание ревью. Манифесты, документация и навыки
# публикации и правок сюда не входят: их обновление не делает прежний результат неверным,
# а лишь сбросило бы все ветки на полный прогон.
REVIEW_FILES = (
    "codex-skills/agent-full-review/**/*",
    "codex-skills/detect-stack/**/*",
    "codex-skills/stack-*/**/*",
    "codex-skills/i18n-sync/**/*",
    "agents/*.md",
    "references/execution-routing.md",
    "references/balanced-review.md",
    "references/check-commands.md",
    "references/review-artifacts.md",
    "codex-skills/references/review-target.md",
    "skills/detect-stack/**/*",
    "skills/i18n-sync/**/*",
    "skills/stack-*/**/*",
    "scripts/*.py",
)


def git(repo, *args):
    """Выполняет git без shell и возвращает байты, сохраняя имена файлов буквально."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    ).stdout


def names(output):
    """Разбирает вывод git с -z в список имён."""
    return [os.fsdecode(name) for name in output.split(b"\0") if name]


def is_ancestor(repo, older, newer):
    """Проверяет, что коммит older достижим из newer; равные коммиты тоже подходят."""
    if not isinstance(older, str):
        return False
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
    )
    return result.returncode == 0


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


def display_name(root, path):
    """Имя файла в отпечатке: относительное внутри root, абсолютное снаружи."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def files_digest(root, paths):
    """Считает отпечаток содержимого и имён выбранных файлов."""
    parts = []
    for path in sorted(set(paths)):
        parts.extend([display_name(root, path).encode(), file_digest(path)])
    return digest(parts)


def submodules(repo):
    """Пути подмодулей из .gitmodules."""
    if not (repo / ".gitmodules").is_file():
        return []
    result = subprocess.run(
        ["git", "-C", str(repo), "config", "--file", ".gitmodules", "--null",
         "--get-regexp", r"^submodule\..*\.path$"],
        capture_output=True,
    )
    if result.returncode == 1:
        return []
    result.check_returncode()
    return [repo / entry.split("\n", 1)[1] for entry in names(result.stdout) if "\n" in entry]


def tree_digest(repo):
    """Отпечаток незакоммиченного состояния по байтам файлов, а не по выводу diff.

    Вывод `git diff` зависит от настроек: textconv скрывает часть изменений, внешний diff
    подменяет вывод целиком, а изменения внутри подмодуля видны лишь как `-dirty`. Поэтому
    индекс учитывается по id объектов, рабочее дерево — по содержимому изменённых файлов,
    подмодули — рекурсивно.
    """
    status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                 "--ignore-submodules=none")
    raw = ("--raw", "-z", "--no-renames", "--full-index", "--no-ext-diff", "--no-textconv")
    index = git(repo, "diff", "--cached", *raw, "HEAD", "--")
    worktree = git(repo, "diff", *raw, "HEAD", "--")
    changed = git(repo, "diff", "--name-only", "-z", "--no-renames", "HEAD", "--")
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    paths = [repo / name for name in names(changed) + names(untracked)]
    parts = [status, index, worktree, files_digest(repo, paths).encode()]
    for sub in submodules(repo):
        if (sub / ".git").exists():
            parts += [display_name(repo, sub).encode(), git(sub, "rev-parse", "HEAD"),
                      tree_digest(sub).encode()]
    return digest(parts)


def rule_files(repo, touched, rules):
    """Находит применимые файлы правил независимо от .gitignore.

    Отслеживаемые и неигнорируемые git перечисляет сам. Игнорируемые — так часто лежит
    AGENTS.override.md — он не показывает, поэтому они ищутся на диске в каталогах
    затронутых файлов вплоть до корня. Глобальные инструкции Codex берутся из CODEX_HOME.
    Файлы из --rules могут лежать и вне репозитория.
    """
    listed = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    found = {repo / name for name in names(listed) if Path(name).name in RULE_NAMES}
    for name in touched:
        directory = (repo / name).parent
        while directory == repo or repo in directory.parents:
            found.update(directory / rule for rule in RULE_NAMES if (directory / rule).is_file())
            directory = directory.parent
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    found.update(home / rule for rule in ("AGENTS.md", "AGENTS.override.md")
                 if (home / rule).is_file())
    extra = []
    for name in rules:
        path = Path(name)
        path = (path if path.is_absolute() else repo / path).absolute()
        if not path.is_file():
            raise ValueError(f"Не найден файл правил: {path}")
        extra.append(display_name(repo, path))
        found.add(path)
    return found, sorted(extra)


def tree_changes(repo, before, after):
    """Реальные пути и статусы двух деревьев, без rename-эвристик и внешних команд."""
    raw = git(repo, "diff-tree", "-r", "--no-commit-id", "--no-renames", "--no-ext-diff",
              "--no-textconv", "--raw", "-z", before, after, "--").split(b"\0")
    return {os.fsdecode(path): header.rsplit(b" ", 1)[1].decode()
            for header, path in zip(raw[::2], raw[1::2])}


def merge_files(repo, merge):
    """Находит решения merge по деревьям, не выполняя повторное слияние и его drivers."""
    parents = git(repo, "rev-list", "--parents", "-n", "1", merge).decode().split()[1:]
    result_changes = [set(tree_changes(repo, parent, merge)) for parent in parents]
    conservative = set().union(*result_changes)
    if len(parents) != 2:
        return conservative
    try:
        bases = git(repo, "merge-base", "--all", *parents).decode().split()
    except subprocess.CalledProcessError:
        return conservative
    if len(bases) != 1:
        return conservative

    left, right = [tree_changes(repo, bases[0], parent) for parent in parents]
    common = left.keys() & right.keys()
    # Удаление общего пути может быть rename/delete или rename/rename: новый путь
    # не обязан совпасть у родителей. D/F-коллизии тоже требуют полного набора.
    directories = [{str(parent) for name in changed for parent in Path(name).parents}
                   for changed in (left, right)]
    structural_conflict = (
        any(left[name] in {"D", "T"} or right[name] in {"D", "T"} for name in common)
        or left.keys() & directories[1] or right.keys() & directories[0]
    )
    if structural_conflict:
        return conservative

    from_left, from_right = result_changes
    return (common | (from_left & from_right)
            | ((left.keys() - right.keys()) & from_left)
            | ((right.keys() - left.keys()) & from_right))


def increment(repo, old_head, head_sha, base_sha):
    """Прирост с прошлого прогона: свои коммиты ветки и разрешённые конфликты слияний.

    Слияние с базой приносит в ветку уже принятый там код — его ревью не повторяется.
    Совместно изменённые пути и ручные отклонения результата входят в прирост.
    Неоднозначные структурные слияния проверяются консервативно целиком.
    """
    span = (f"{old_head}..{head_sha}", "--not", base_sha)
    commits = git(repo, "rev-list", "--no-merges", *span).decode().split()
    merges = git(repo, "rev-list", "--merges", *span).decode().split()
    files = set(names(git(repo, "log", "--no-merges", "--no-renames", "-z", "--name-only",
                          "--format=", *span, "--")))
    for merge in merges:
        files.update(merge_files(repo, merge))
    return {"commits": commits, "merges": merges, "files": sorted(files)}


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
    """Фиксирует обе стороны сравнения, дерево, правила и версию инструкций.

    Прежние голова, точка расхождения и база должны быть предками текущих:
    ветку дописали или влили базу, но не переписали историю. Изменения базы
    возвращаются отдельно в base_changes для проверки влияния на PR.
    """
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
    status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                 "--ignore-submodules=none")
    touched = (
        names(git(repo, "diff", "--name-only", "-z", "--no-renames", merge_base, head_sha, "--"))
        + names(git(repo, "diff", "--name-only", "-z", "--no-renames", "HEAD", "--"))
        + names(git(repo, "ls-files", "--others", "--exclude-standard", "-z"))
    )
    rule_paths, extra_rules = rule_files(repo, touched, rules)
    package_files = {
        path for pattern in REVIEW_FILES for path in PLUGIN_ROOT.glob(pattern)
        if path.is_file() and path.suffix in {".md", ".json", ".yaml", ".py"}
    }
    snapshot = {
        "target": target,
        "scope": ",".join(sorted(set(scope.split(",")))),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "dirty": bool(status),
        "tree_hash": tree_digest(repo),
        "rules_hash": files_digest(repo, rule_paths),
        "plugin_hash": files_digest(PLUGIN_ROOT, package_files),
        "rules": extra_rules,
    }
    snapshot["key"] = digest([json.dumps(snapshot, sort_keys=True).encode()])
    previous = history(repo, target)
    last = previous[-1] if previous else None
    mode, added, base_changes = "first", None, []
    if last:
        old = last["snapshot"]
        if old.get("key") == snapshot["key"]:
            mode = "unchanged"
        elif (
            not snapshot["dirty"] and old.get("dirty") is False
            and all(old.get(key) == snapshot[key] for key in (
                "rules_hash", "plugin_hash", "scope", "rules"
            ))
            and is_ancestor(repo, old.get("head_sha"), head_sha)
            and is_ancestor(repo, old.get("merge_base"), merge_base)
            and is_ancestor(repo, old.get("base_sha"), base_sha)
        ):
            mode = "followup"
            added = increment(repo, old["head_sha"], head_sha, base_sha)
            base_changes = sorted(tree_changes(repo, old["base_sha"], base_sha))
    return {"snapshot": snapshot, "mode": mode, "previous": last, "increment": added,
            "base_changes": base_changes,
            "base_changes_key": digest([json.dumps(base_changes, ensure_ascii=False).encode()]),
            "analysis_round": last_analysis_round(previous)}


def last_analysis_round(previous):
    """Последний завершённый анализ; старые записи без номера учитываются по порядку."""
    number = 0
    for entry in previous:
        value = entry.get("analysis_round")
        if type(value) is int and value >= 0:
            number = max(number, value)
        elif entry["report"].get("analysis_reused") is not True:
            number += 1
    return number


def record(repo, snapshot, report):
    """Записывает результат лишь для неизменившегося предмета завершённого ревью."""
    if report.get("verdict") not in VERDICTS:
        raise ValueError("Неизвестный вердикт")
    for key in ("findings", "open", "checks"):
        if not isinstance(report.get(key), list):
            raise ValueError(f"Поле {key} должно быть массивом")
    state = capture(
        repo, snapshot["base_sha"], snapshot["head_sha"], snapshot["target"],
        snapshot["scope"], snapshot["rules"]
    )
    current = state["snapshot"]
    if current["key"] != snapshot["key"]:
        raise ValueError("Код, правила или пакет изменились во время ревью; запись отклонена")
    reused = report.get("analysis_reused") is True
    if reused and (state["mode"] == "first" or any(
        (state["increment"] or {}).get(key) for key in ("commits", "files")
    )):
        raise ValueError("Нельзя переиспользовать анализ при новом коде или несовместимом снимке")
    if reused and state["base_changes"]:
        impact = report.get("impact")
        assessment = impact.get("base_assessment") if isinstance(impact, dict) else None
        if not (
            isinstance(assessment, dict)
            and assessment.get("base_changes_key") == state["base_changes_key"]
            and assessment.get("affects_pr") is False
            and isinstance(assessment.get("evidence"), str)
            and assessment["evidence"].strip()
        ):
            raise ValueError("Переиспользование после изменения базы требует impact.base_assessment "
                             "с текущим base_changes_key, affects_pr: false и evidence")
        report = {**report, "impact": {**impact, "base_assessment": {
            **assessment, "paths": state["base_changes"],
        }}}
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot,
        "report": report,
        "analysis_round": state["analysis_round"] + (not reused),
    }
    path = log_path(repo)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"log": str(path), "key": snapshot["key"], "analysis_round": entry["analysis_round"]}


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
