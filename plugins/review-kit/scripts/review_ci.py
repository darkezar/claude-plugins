#!/usr/bin/env python3
"""Читает CI проверенного PR и ограниченно ждёт без обращений к модели и загрузки логов."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import subprocess
import time

from review_storage import write_json


CHECK_FIELDS = "name,state,bucket,workflow,link"
GH_CALL_TIMEOUT = 20
READ_ONLY_BUDGET = 30
EXIT_CODES = {"pass": 0, "fail": 1, "pending": 8, "unconfirmed": 2, "stale": 3, "error": 4}
EMPTY_CHECKS = re.compile(r"no (?:required )?checks reported on the '[^\n]+' branch")


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(EXIT_CODES['error'], f'{self.prog}: {message}\n')


def expected_checks(values):
    result = set()
    for value in values:
        if isinstance(value, str):
            if value.count('/') != 1:
                raise ValueError('--expect-check требует WORKFLOW/NAME с одним /; '
                                 'для имён со слешами используй --expect-check-pair WORKFLOW NAME')
            workflow, name = value.split('/')
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            workflow, name = value
        else:
            raise ValueError('Нужна пара WORKFLOW NAME из доверенной конфигурации')
        if any(not isinstance(part, str) or not part.strip() for part in (workflow, name)):
            raise ValueError('WORKFLOW и NAME должны быть непустыми строками')
        result.add((workflow, name))
    return result


def query(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Истёк бюджет проверки CI")
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=min(GH_CALL_TIMEOUT, remaining)
    )
    # gh возвращает 1 при упавших checks, 8 при ожидающих. Ошибку API не выдаём за CI.
    if result.returncode not in (0, 1, 8):
        raise ValueError(result.stderr.strip() or f"gh завершился с кодом {result.returncode}")
    if (args[:2] == ['pr', 'checks'] and result.returncode == 1
            and not result.stdout.strip() and EMPTY_CHECKS.fullmatch(result.stderr.strip())):
        return []
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise ValueError(result.stderr.strip() or "gh не вернул JSON") from error


def check_list(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Ожидался массив checks")
    return value


def observation(repo, pr, head, base, expected, deadline):
    target = [str(pr), "--repo", repo]
    meta_args = ["pr", "view", *target, "--json", "headRefOid,baseRefOid,url"]
    before = query(meta_args, deadline)
    if not isinstance(before, dict):
        raise ValueError("Не получены SHA pull request")
    if before.get("headRefOid") != head:
        return {"state": "stale", "reason": "Голова PR отличается от проверенной",
                "observed": before, "checks": [], "required": []}

    checks = check_list(query(["pr", "checks", *target, "--json", CHECK_FIELDS], deadline))
    required_error = None
    try:
        required = check_list(query(
            ["pr", "checks", *target, "--required", "--json", CHECK_FIELDS], deadline
        ))
    except ValueError as error:
        required, required_error = [], str(error)
    after = query(meta_args, deadline)
    if not isinstance(after, dict):
        raise ValueError('Не получены SHA pull request после чтения checks')
    result = {"observed": after, "checks": checks, "required": required,
              "base_moved": any(meta.get('baseRefOid') != base for meta in (before, after)),
              "observed_base_sha": after.get('baseRefOid')}
    if after.get('headRefOid') != head:
        return {**result, "state": "stale", "reason": "Голова PR изменилась во время проверки CI"}

    expected = expected_checks(expected)
    present = {(item.get('workflow'), item.get('name')) for item in checks}
    missing = sorted(f'{workflow}/{name}' for workflow, name in expected - present)
    selected = required + [item for item in checks if (item.get('workflow'), item.get('name')) in expected]
    result.update(missing=missing, required_error=required_error)
    if not selected and not missing:
        return {**result, "state": "unconfirmed",
                "reason": "Не установлен непустой набор обязательных проверок"}
    buckets = {item.get("bucket") for item in selected}
    if "fail" in buckets:
        return {**result, "state": "fail", "reason": "Обязательная проверка упала"}
    if required_error:
        return {**result, "state": "unconfirmed", "reason": "Не удалось определить required checks"}
    if missing or "pending" in buckets:
        return {**result, "state": "pending", "reason": "Обязательные проверки ещё не завершены"}
    if buckets != {"pass"}:
        return {**result, "state": "unconfirmed",
                "reason": "Есть пропущенные, отменённые или неизвестные обязательные проверки"}
    return {**result, "state": "pass", "reason": "Обязательные проверки завершились успешно"}


def collect(repo, pr, head, base, expected=(), wait_seconds=0, interval=15):
    if wait_seconds < 0 or interval <= 0:
        raise ValueError("Бюджет ожидания неотрицательный, интервал положительный")
    expected_checks(expected)
    started = time.monotonic()
    deadline = started + (wait_seconds or READ_ONLY_BUDGET)
    result = {"state": "unconfirmed", "reason": "CI не подтверждён", "checks": [], "required": []}
    while True:
        poll_started = time.monotonic()
        try:
            result = observation(repo, pr, head, base, expected, deadline)
        except (TimeoutError, subprocess.TimeoutExpired) as error:
            if result['state'] == 'pending':
                result['reason'] = result.get('reason', 'Ожидание CI') + '; бюджет ожидания исчерпан'
            else:
                result = {**result, 'state': 'unconfirmed', 'reason': str(error)}
            break
        except (OSError, ValueError) as error:
            result = {**result, "state": "unconfirmed", "reason": str(error)}
            break
        poll_duration = time.monotonic() - poll_started
        if result["state"] != "pending" or not wait_seconds:
            break
        remaining = deadline - time.monotonic()
        if remaining <= interval + poll_duration:
            result["reason"] += "; бюджет ожидания исчерпан"
            break
        time.sleep(interval)
    return {**result, "repo": repo, "pr": pr, "head_sha": head, "base_sha": base,
            "base_evidence": "observed_pr_metadata_only",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 2)}


def summary(result, output):
    """Полный список остаётся артефактом; в контекст идут счётчики и проблемные jobs."""
    fields = ("state", "reason", "repo", "pr", "head_sha", "base_sha", "checked_at",
              "elapsed_seconds", "missing", "required_error", "base_evidence", "base_moved", "observed_base_sha")
    return {
        **{key: result[key] for key in fields if key in result},
        "counts": dict(Counter(item.get("bucket", "unknown") for item in result["checks"])),
        "attention": [item for item in result["checks"] if item.get("bucket") != "pass"],
        "artifact": str(output.resolve()),
    }


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="[HOST/]OWNER/REPO проверенного PR")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--expect-check", action="append", default=[],
                        help="Совместимость: WORKFLOW/NAME с одним /; иначе --expect-check-pair")
    parser.add_argument("--expect-check-pair", action="append", nargs=2, default=[],
                        metavar=("WORKFLOW", "NAME"),
                        help="Доверенная обязательная job: два точных имени, в том числе со слешами")
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--output", type=Path, required=True, help="JSON вне проверяемого дерева")
    args = parser.parse_args()
    try:
        result = collect(args.repo, args.pr, args.head, args.base, args.expect_check + args.expect_check_pair,
                         args.wait_seconds, args.interval)
        write_json(args.output, result)
    except (OSError, ValueError) as error:
        parser.exit(EXIT_CODES["error"], f"review-kit CI: {error}\n")
    print(json.dumps(summary(result, args.output), ensure_ascii=False, indent=2))
    raise SystemExit(EXIT_CODES[result["state"]])


if __name__ == "__main__":
    main()
