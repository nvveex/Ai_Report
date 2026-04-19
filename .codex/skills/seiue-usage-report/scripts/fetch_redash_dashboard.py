#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REQUIRED_ENV = ("REDASH_API_KEY", "REDASH_BASE_URL", "REDASH_DASHBOARD_ID")
DEFAULT_CACHE_MAX_AGE = 60 * 60 * 24 * 365 * 10
OUTPUT_TIMEZONE = ZoneInfo("Asia/Shanghai")


class RedashError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def find_repo_root() -> Path:
    cwd = Path.cwd().resolve()
    script_path = Path(__file__).resolve()
    candidates = [cwd, *cwd.parents, script_path.parent, *script_path.parents]

    for candidate in candidates:
        if (candidate / ".codex/skills/seiue-usage-report/SKILL.md").is_file():
            return candidate

    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate

    raise SystemExit("无法定位仓库根目录：请在 Ai_Report 仓库内运行脚本。")


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SystemExit(f".env 编码无法读取，请使用 UTF-8：{path}") from exc

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_config(repo_root: Path) -> dict[str, str]:
    dotenv_values = parse_dotenv(repo_root / ".env")
    config: dict[str, str] = {}
    missing: list[str] = []

    for key in REQUIRED_ENV:
        value = os.environ.get(key) or dotenv_values.get(key)
        if value:
            config[key] = value.strip()
        else:
            missing.append(key)

    if missing:
        names = ", ".join(missing)
        raise SystemExit(f"缺少 Redash 配置：{names}。请设置进程环境变量或仓库根目录 .env。")

    config["REDASH_BASE_URL"] = config["REDASH_BASE_URL"].rstrip("/")
    return config


def redact(text: str, api_key: str) -> str:
    if not text:
        return text
    return text.replace(api_key, "[REDACTED]")


class RedashClient:
    def __init__(self, base_url: str, api_key: str, timeout: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            safe_body = redact(error_body, self.api_key)
            raise RedashError(f"HTTP {exc.code}: {safe_body}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise RedashError(f"网络请求失败：{exc.reason}") from exc

        if not raw:
            return {}

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RedashError("Redash 返回了非 JSON 响应。") from exc


def normalize_school_id(raw: str) -> int | str:
    value = raw.strip()
    if not value:
        raise SystemExit("school_id 不能为空。")
    return int(value) if value.isdigit() else value


def safe_filename_part(raw: Any) -> str:
    text = str(raw)
    text = re.sub(r"[^0-9A-Za-z._-]+", "-", text)
    return text.strip("-") or "unknown"


def local_today_string() -> str:
    return datetime.now(OUTPUT_TIMEZONE).date().isoformat()


def iso_now() -> str:
    return datetime.now(OUTPUT_TIMEZONE).isoformat(timespec="seconds")


def extract_queries(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    queries: dict[int, dict[str, Any]] = {}

    for widget in dashboard.get("widgets") or []:
        visualization = widget.get("visualization") or {}
        query = visualization.get("query") or {}
        query_id = query.get("id")
        if query_id is None:
            continue

        try:
            query_id_int = int(query_id)
        except (TypeError, ValueError):
            continue

        entry = queries.setdefault(
            query_id_int,
            {
                "query_id": query_id_int,
                "query_name": query.get("name") or visualization.get("name") or f"query-{query_id_int}",
                "visualization_name": visualization.get("name"),
                "visualization_type": visualization.get("type"),
                "widget_ids": [],
            },
        )
        if widget.get("id") is not None:
            entry["widget_ids"].append(widget.get("id"))

    return sorted(queries.values(), key=lambda item: item["query_id"])


def normalize_columns(columns: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for column in columns:
        if isinstance(column, dict):
            normalized.append(
                {
                    "name": column.get("name"),
                    "friendly_name": column.get("friendly_name"),
                    "type": column.get("type"),
                }
            )
        else:
            normalized.append({"name": str(column), "friendly_name": None, "type": None})
    return normalized


def query_result_payload(response: dict[str, Any]) -> dict[str, Any] | None:
    result = response.get("query_result")
    if isinstance(result, dict):
        return result
    if "data" in response:
        return response
    return None


def poll_job(client: RedashClient, job: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    job_id = job.get("id")
    if not job_id:
        raise RedashError("Redash 返回 job，但缺少 job id。")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = client.request_json("GET", f"/api/jobs/{job_id}")
        current = response.get("job") or response
        status = current.get("status")

        if status == 3:
            result_id = current.get("query_result_id")
            if not result_id:
                raise RedashError("Redash job 成功但缺少 query_result_id。")
            result_response = client.request_json("GET", f"/api/query_results/{result_id}.json")
            result = query_result_payload(result_response)
            if not result:
                raise RedashError("query_result 响应中缺少数据。")
            return result

        if status in {4, 5}:
            error_text = current.get("error") or current.get("message") or "Redash job 执行失败。"
            raise RedashError(str(error_text))

        time.sleep(1.5)

    raise RedashError(f"等待 Redash job 超时：job_id={job_id}")


def fetch_query(
    client: RedashClient,
    query_meta: dict[str, Any],
    school_id: int | str,
    cache_max_age: int,
    job_timeout: int,
) -> dict[str, Any]:
    query_id = query_meta["query_id"]
    parameters = {"school_id": school_id}

    def finish(result: dict[str, Any], fetch_mode: str) -> dict[str, Any]:
        data = result.get("data") or {}
        columns = normalize_columns(data.get("columns") or [])
        rows = data.get("rows") or []
        status = "empty" if len(rows) == 0 else fetch_mode
        return {
            **query_meta,
            "status": status,
            "fetch_mode": fetch_mode,
            "query_result_id": result.get("id"),
            "retrieved_at": result.get("retrieved_at"),
            "columns": columns,
            "row_count": len(rows),
            "rows": rows,
            "error": None,
        }

    cache_errors: list[str] = []

    try:
        cache_response = client.request_json(
            "GET",
            f"/api/queries/{query_id}/results.json",
            query={"p_school_id": school_id},
        )
        cached_result = query_result_payload(cache_response)
        if cached_result:
            return finish(cached_result, "cached")

        raise RedashError("缓存请求未返回 query_result。")

    except RedashError as cache_error:
        if cache_error.status in {401, 403}:
            return {
                **query_meta,
                "status": "error",
                "fetch_mode": None,
                "query_result_id": None,
                "retrieved_at": None,
                "columns": [],
                "row_count": 0,
                "rows": [],
                "error": str(cache_error),
            }
        cache_errors.append(str(cache_error))

    try:
        cache_response = client.request_json(
            "POST",
            f"/api/queries/{query_id}/results",
            {"parameters": parameters, "max_age": cache_max_age},
        )
        cached_result = query_result_payload(cache_response)
        if cached_result:
            return finish(cached_result, "cached")

        cache_job = cache_response.get("job")
        if isinstance(cache_job, dict):
            refreshed_result = poll_job(client, cache_job, job_timeout)
            return finish(refreshed_result, "refreshed")

        raise RedashError("缓存优先请求未返回 query_result 或 job。")

    except RedashError as cache_error:
        if cache_error.status in {401, 403}:
            return {
                **query_meta,
                "status": "error",
                "fetch_mode": None,
                "query_result_id": None,
                "retrieved_at": None,
                "columns": [],
                "row_count": 0,
                "rows": [],
                "error": str(cache_error),
            }
        cache_errors.append(str(cache_error))

    try:
        refresh_response = client.request_json(
            "POST",
            f"/api/queries/{query_id}/results",
            {"parameters": parameters, "max_age": 0},
        )
        refreshed_result = query_result_payload(refresh_response)
        if refreshed_result:
            return finish(refreshed_result, "refreshed")

        refresh_job = refresh_response.get("job")
        if isinstance(refresh_job, dict):
            refreshed_result = poll_job(client, refresh_job, job_timeout)
            return finish(refreshed_result, "refreshed")

        raise RedashError("刷新请求未返回 query_result 或 job。")
    except RedashError as refresh_error:
        detail = str(refresh_error)
        if cache_errors:
            detail = f"{detail}；缓存尝试：{' | '.join(cache_errors)}"
        return {
            **query_meta,
            "status": "error",
            "fetch_mode": None,
            "query_result_id": None,
            "retrieved_at": None,
            "columns": [],
            "row_count": 0,
            "rows": [],
            "error": detail,
        }


def build_summary(queries: list[dict[str, Any]], total_queries: int | None = None) -> dict[str, int]:
    status_counts = {
        "cached": 0,
        "refreshed": 0,
        "empty": 0,
        "error": 0,
    }
    fetch_counts = {
        "cached_count": 0,
        "refreshed_count": 0,
    }

    for query in queries:
        status = query.get("status")
        if status in status_counts:
            status_counts[status] += 1
        fetch_mode = query.get("fetch_mode")
        if fetch_mode == "cached":
            fetch_counts["cached_count"] += 1
        elif fetch_mode == "refreshed":
            fetch_counts["refreshed_count"] += 1

    usable_count = status_counts["cached"] + status_counts["refreshed"]
    completed_query_count = len(queries)
    total_query_count = total_queries if total_queries is not None else completed_query_count
    return {
        "total_queries": total_query_count,
        "completed_query_count": completed_query_count,
        "success_count": completed_query_count - status_counts["error"],
        "usable_query_count": usable_count,
        "cached_count": fetch_counts["cached_count"],
        "refreshed_count": fetch_counts["refreshed_count"],
        "empty_count": status_counts["empty"],
        "error_count": status_counts["error"],
        "pending_count": max(total_query_count - completed_query_count, 0),
    }


def build_fetch_status(
    query_metas: list[dict[str, Any]],
    queries: list[dict[str, Any]],
    started_at: str,
) -> dict[str, Any]:
    completed_query_ids = {item.get("query_id") for item in queries}
    pending_queries = [
        {
            "query_id": query_meta["query_id"],
            "query_name": query_meta["query_name"],
        }
        for query_meta in query_metas
        if query_meta["query_id"] not in completed_query_ids
    ]
    return {
        "is_complete": len(pending_queries) == 0,
        "started_at": started_at,
        "updated_at": iso_now(),
        "total_queries": len(query_metas),
        "completed_query_count": len(queries),
        "pending_query_count": len(pending_queries),
        "pending_queries": pending_queries,
    }


def build_snapshot(
    config: dict[str, str],
    dashboard_id: str,
    school_id: int | str,
    dashboard: dict[str, Any],
    query_metas: list[dict[str, Any]],
    query_results: list[dict[str, Any]],
    started_at: str,
) -> dict[str, Any]:
    return {
        "generated_at": iso_now(),
        "source": {
            "base_url": config["REDASH_BASE_URL"],
            "dashboard_id": dashboard_id,
            "school_id": school_id,
        },
        "dashboard": {
            "id": dashboard.get("id"),
            "name": dashboard.get("name"),
            "slug": dashboard.get("slug"),
            "widget_count": len(dashboard.get("widgets") or []),
            "query_count": len(query_metas),
        },
        "fetch_status": build_fetch_status(query_metas, query_results, started_at),
        "queries": query_results,
        "summary": build_summary(query_results, total_queries=len(query_metas)),
    }


def write_snapshot(output_path: Path, snapshot: dict[str, Any]) -> None:
    temp_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="缓存优先拉取希悦 Redash dashboard 查询结果，生成 JSON 数据快照。"
    )
    parser.add_argument("school_id", nargs="?", help="学校 ID，例如 539")
    parser.add_argument("--school-id", dest="school_id_option", help="学校 ID，例如 539")
    parser.add_argument("--output", help="快照输出路径；默认写入仓库根目录 output/")
    parser.add_argument("--timeout", type=int, default=30, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--job-timeout", type=int, default=300, help="单个 Redash job 最大等待秒数")
    parser.add_argument(
        "--cache-max-age",
        type=int,
        default=DEFAULT_CACHE_MAX_AGE,
        help="缓存优先请求的 max_age 秒数；默认约 10 年，不做全量强制刷新",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    school_id_raw = args.school_id_option or args.school_id
    if not school_id_raw:
        eprint("缺少 school_id。示例：python3 fetch_redash_dashboard.py 539")
        return 2

    school_id = normalize_school_id(school_id_raw)
    repo_root = find_repo_root()
    config = load_config(repo_root)

    dashboard_id = config["REDASH_DASHBOARD_ID"].strip()
    output_path = (
        Path(args.output)
        if args.output
        else repo_root
        / "output"
        / f"school-{safe_filename_part(school_id)}-{local_today_string()}.json"
    )
    if not output_path.is_absolute():
        output_path = repo_root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    client = RedashClient(config["REDASH_BASE_URL"], config["REDASH_API_KEY"], args.timeout)

    try:
        dashboard = client.request_json(
            "GET",
            f"/api/dashboards/{urllib.parse.quote(str(dashboard_id))}",
            query={f"p_school_id": school_id},
        )
    except RedashError as exc:
        eprint(f"读取 dashboard 失败：{exc}")
        return 1

    query_metas = extract_queries(dashboard)
    eprint(f"发现 dashboard query 数：{len(query_metas)}")

    started_at = iso_now()
    query_results: list[dict[str, Any]] = []
    initial_snapshot = build_snapshot(
        config,
        dashboard_id,
        school_id,
        dashboard,
        query_metas,
        query_results,
        started_at,
    )
    write_snapshot(output_path, initial_snapshot)
    eprint(f"已初始化增量快照：{output_path}")

    for index, query_meta in enumerate(query_metas, start=1):
        result = fetch_query(client, query_meta, school_id, args.cache_max_age, args.job_timeout)
        query_results.append(result)
        snapshot = build_snapshot(
            config,
            dashboard_id,
            school_id,
            dashboard,
            query_metas,
            query_results,
            started_at,
        )
        write_snapshot(output_path, snapshot)
        eprint(
            f"[{index}/{len(query_metas)}] query_id={query_meta['query_id']} "
            f"status={result['status']} rows={result['row_count']} name={query_meta['query_name']}"
        )

    snapshot = build_snapshot(
        config,
        dashboard_id,
        school_id,
        dashboard,
        query_metas,
        query_results,
        started_at,
    )
    write_snapshot(output_path, snapshot)
    eprint(f"已完成数据快照：{output_path}")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
