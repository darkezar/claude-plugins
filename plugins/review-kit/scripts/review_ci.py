#!/usr/bin/env python3
"""Читает CI проверенного PR и ограниченно ждёт без обращений к модели и загрузки логов."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time


CHECK_FIELDS = "name,state,bucket,workflow,link"


def query(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Истёк бюджет проверки CI")
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=min(20, remaining)
    )
    # gh возвращает 1 при упавших checks, 8 при ожидающих. Ошибку API не выдаём за CI.
    if result.returncode not in (0, 1, 8):
        raise ValueError(result.stderr.strip() or f"gh завершился с кодом {result.returncode}")
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
    if before.get("headRefOid") != head or before.get("baseRefOid") != base:
        return {"state": "stale", "reason": "Пара SHA PR отличается от проверенной",
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
    result = {"observed": after, "checks": checks, "required": required}
    if not isinstance(after, dict) or any(after.get(key) != before.get(key)
                                          for key in ("headRefOid", "baseRefOid")):
        return {**result, "state": "stale", "reason": "PR изменился во время проверки CI"}

    expected = set(expected)
    present = {item.get("name") for item in checks}
    missing = sorted(expected - present)
    selected = required + [item for item in checks if item.get("name") in expected]
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
    started = time.monotonic()
    deadline = started + (wait_seconds or 30)
    result = {"state": "unconfirmed", "reason": "CI не подтверждён", "checks": [], "required": []}
    while True:
        try:
            result = observation(repo, pr, head, base, expected, deadline)
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
            result = {**result, "state": "unconfirmed", "reason": str(error)}
            break
        if result["state"] != "pending" or not wait_seconds:
            break
        remaining = deadline - time.monotonic()
        if remaining <= interval:
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
              "elapsed_seconds", "missing", "required_error", "base_evidence")
    return {
        **{key: result[key] for key in fields if key in result},
        "counts": dict(Counter(item.get("bucket", "unknown") for item in result["checks"])),
        "attention": [item for item in result["checks"] if item.get("bucket") != "pass"],
        "artifact": str(output.resolve()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="[HOST/]OWNER/REPO проверенного PR")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--expect-check", action="append", default=[],
                        help="Обязательная по правилам проекта job, даже если ещё не появилась")
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--output", type=Path, required=True, help="JSON вне проверяемого дерева")
    args = parser.parse_args()
    try:
        result = collect(args.repo, args.pr, args.head, args.base, args.expect_check,
                         args.wait_seconds, args.interval)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except (OSError, ValueError) as error:
        parser.exit(2, f"review-kit CI: {error}\n")
    print(json.dumps(summary(result, args.output), ensure_ascii=False, indent=2))
    raise SystemExit({"pass": 0, "fail": 1, "pending": 8, "unconfirmed": 2, "stale": 3}[result["state"]])


if __name__ == "__main__":
    main()
