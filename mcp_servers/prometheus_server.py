"""
prometheus_server.py

MCP server exposing Prometheus query tools for homelab observability.
Allows the agent to run PromQL queries, inspect scrape targets, and
check active alerts inline during reasoning — no manual Grafana visit needed.

Requires PROMETHEUS_URL env var (default: http://localhost:9090).
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("prometheus")

_PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
_TIMEOUT = int(os.environ.get("PROMETHEUS_TIMEOUT", "10"))


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(path: str, params: dict | None = None) -> dict:
    url = f"{_PROMETHEUS_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Prometheus HTTP {exc.code}: {body[:300]}") from exc
    except OSError as exc:
        raise RuntimeError(f"Prometheus unreachable ({_PROMETHEUS_URL}): {exc}") from exc


# ── Formatters ────────────────────────────────────────────────────────────────

def _fmt_instant(result: list) -> str:
    if not result:
        return "No data"
    lines = []
    for item in result:
        metric = item.get("metric", {})
        val = item.get("value", [None, "?"])[1]
        label_str = ", ".join(f'{k}="{v}"' for k, v in metric.items() if k != "__name__")
        name = metric.get("__name__", "")
        label_part = f"{{{label_str}}}" if label_str else ""
        try:
            val_f = float(val)
            val_str = f"{val_f:.4g}" if val_f != int(val_f) else str(int(val_f))
        except (ValueError, TypeError):
            val_str = str(val)
        lines.append(f"{name}{label_part} = {val_str}")
    return "\n".join(lines)


def _fmt_range(result: list, max_points: int = 8) -> str:
    if not result:
        return "No data"
    parts = []
    for item in result:
        metric = item.get("metric", {})
        values = item.get("values", [])
        label_str = ", ".join(f'{k}="{v}"' for k, v in metric.items() if k != "__name__")
        name = metric.get("__name__", "")
        header = f"{name}{{{label_str}}}" if label_str else name
        # Sample evenly spaced points to keep output tight
        if len(values) > max_points:
            step = len(values) // max_points
            values = values[::step][-max_points:]
        rows = []
        for ts, val in values:
            t = datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")
            try:
                v = f"{float(val):.4g}"
            except (ValueError, TypeError):
                v = str(val)
            rows.append(f"  {t}Z  {v}")
        parts.append(f"{header}\n" + "\n".join(rows))
    return "\n\n".join(parts)


# ── Tool list ─────────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="prometheus_query",
            description=(
                "Run a PromQL instant query against the homelab Prometheus. "
                "Returns current values for the matched time series. "
                "Use for answering 'what is X right now?' — CPU, memory, disk, "
                "container state, temperature, network, etc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "PromQL expression, e.g. 'container_memory_usage_bytes{name=~\".+\"}'",
                    },
                    "time": {
                        "type": "string",
                        "description": "Evaluation timestamp as ISO 8601 or Unix seconds. Omit for now.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="prometheus_query_range",
            description=(
                "Run a PromQL range query to see metric trends over time. "
                "Use for answering 'has X been high lately?' or 'when did Y spike?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "PromQL expression"},
                    "start": {
                        "type": "string",
                        "description": "Start time as ISO 8601 or relative like '1h' (ago). Default: 1h ago.",
                    },
                    "end": {
                        "type": "string",
                        "description": "End time as ISO 8601 or 'now'. Default: now.",
                    },
                    "step": {
                        "type": "string",
                        "description": "Resolution step, e.g. '60s', '5m', '1h'. Default: auto.",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="prometheus_targets",
            description=(
                "List all Prometheus scrape targets and their UP/DOWN health. "
                "Use to diagnose which exporters or services are unreachable."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["active", "dropped", "any"],
                        "description": "Filter targets by state. Default: active.",
                    },
                },
            },
        ),
        Tool(
            name="prometheus_alerts",
            description=(
                "List currently firing or pending alerts from Prometheus. "
                "Shows alert name, labels, state, and how long it has been active."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["firing", "pending", "all"],
                        "description": "Filter by alert state. Default: all.",
                    },
                },
            },
        ),
        Tool(
            name="prometheus_metric_names",
            description=(
                "Search available metric names in Prometheus. "
                "Use when building a PromQL query and unsure of the exact metric name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Substring to filter metric names, e.g. 'container_cpu' or 'memory'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return. Default: 30.",
                    },
                },
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "prometheus_query":
            return await _handle_query(arguments)
        if name == "prometheus_query_range":
            return await _handle_query_range(arguments)
        if name == "prometheus_targets":
            return await _handle_targets(arguments)
        if name == "prometheus_alerts":
            return await _handle_alerts(arguments)
        if name == "prometheus_metric_names":
            return await _handle_metric_names(arguments)
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except RuntimeError as exc:
        return [TextContent(type="text", text=f"Error: {exc}")]


async def _handle_query(args: dict) -> list[TextContent]:
    params = {"query": args["query"]}
    if args.get("time"):
        params["time"] = args["time"]
    data = _get("/api/v1/query", params)
    if data.get("status") != "success":
        return [TextContent(type="text", text=f"Query failed: {data.get('error', 'unknown')}")]
    result = data["data"]["result"]
    result_type = data["data"]["resultType"]
    if result_type == "scalar":
        return [TextContent(type="text", text=f"scalar = {result[1]}")]
    text = _fmt_instant(result)
    return [TextContent(type="text", text=f"Query: {args['query']}\nResults ({len(result)}):\n{text}")]


async def _handle_query_range(args: dict) -> list[TextContent]:
    import time as _time

    now = _time.time()
    raw_start = args.get("start", "1h")
    raw_end = args.get("end", "now")
    step = args.get("step")

    # Resolve relative start like "1h", "30m", "2h"
    def _resolve_time(raw: str, default_now: float) -> float:
        if raw in ("now", ""):
            return default_now
        # Try plain unix float
        try:
            return float(raw)
        except ValueError:
            pass
        # Relative durations: 30m, 1h, 2d
        import re
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h|d)", raw.strip())
        if m:
            n, unit = float(m.group(1)), m.group(2)
            secs = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
            return default_now - n * secs
        # ISO 8601
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return default_now

    start_ts = _resolve_time(raw_start, now)
    end_ts = _resolve_time(raw_end, now) if raw_end != "now" else now
    duration = end_ts - start_ts

    if not step:
        # Auto step: ~50 data points
        step_secs = max(15, int(duration / 50))
        step = f"{step_secs}s"

    params = {
        "query": args["query"],
        "start": str(start_ts),
        "end": str(end_ts),
        "step": step,
    }
    data = _get("/api/v1/query_range", params)
    if data.get("status") != "success":
        return [TextContent(type="text", text=f"Range query failed: {data.get('error', 'unknown')}")]
    result = data["data"]["result"]
    text = _fmt_range(result)
    return [TextContent(type="text", text=f"Range query: {args['query']}\nPeriod: {raw_start} → {raw_end}\n\n{text}")]


async def _handle_targets(args: dict) -> list[TextContent]:
    state = args.get("state", "active")
    params = {}
    if state != "any":
        params["state"] = state
    data = _get("/api/v1/targets", params or None)
    if data.get("status") != "success":
        return [TextContent(type="text", text=f"Targets query failed: {data.get('error', 'unknown')}")]

    active = data["data"].get("activeTargets", [])
    lines = []
    up_count = 0
    down_count = 0
    for t in active:
        health = t.get("health", "unknown")
        job = t.get("labels", {}).get("job", "?")
        instance = t.get("labels", {}).get("instance", "?")
        last_err = t.get("lastError", "")
        last_scrape = t.get("lastScrape", "")
        status_sym = "UP" if health == "up" else "DOWN"
        if health == "up":
            up_count += 1
        else:
            down_count += 1
        err_part = f"  ERROR: {last_err}" if last_err else ""
        lines.append(f"[{status_sym}] {job} / {instance}{err_part}")

    summary = f"{up_count} up, {down_count} down"
    return [TextContent(type="text", text=f"Scrape targets ({summary}):\n" + "\n".join(lines))]


async def _handle_alerts(args: dict) -> list[TextContent]:
    data = _get("/api/v1/alerts")
    if data.get("status") != "success":
        return [TextContent(type="text", text=f"Alerts query failed: {data.get('error', 'unknown')}")]

    alerts = data["data"].get("alerts", [])
    state_filter = args.get("state", "all")
    if state_filter != "all":
        alerts = [a for a in alerts if a.get("state") == state_filter]

    if not alerts:
        return [TextContent(type="text", text="No alerts matching filter.")]

    lines = []
    for a in alerts:
        state = a.get("state", "?")
        alert_name = a.get("labels", {}).get("alertname", "?")
        severity = a.get("labels", {}).get("severity", "")
        instance = a.get("labels", {}).get("instance", "")
        active_at = a.get("activeAt", "")
        annotations = a.get("annotations", {})
        summary = annotations.get("summary", "")

        sev_part = f" [{severity}]" if severity else ""
        inst_part = f" on {instance}" if instance else ""
        time_part = ""
        if active_at:
            try:
                at = datetime.fromisoformat(active_at.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - at
                mins = int(delta.total_seconds() / 60)
                time_part = f" (active {mins}m)"
            except ValueError:
                pass
        lines.append(f"[{state.upper()}]{sev_part} {alert_name}{inst_part}{time_part}")
        if summary:
            lines.append(f"  {summary}")

    return [TextContent(type="text", text=f"Alerts ({len(alerts)}):\n" + "\n".join(lines))]


async def _handle_metric_names(args: dict) -> list[TextContent]:
    search = (args.get("search") or "").lower()
    limit = int(args.get("limit") or 30)

    data = _get("/api/v1/label/__name__/values")
    if data.get("status") != "success":
        return [TextContent(type="text", text=f"Metric name query failed: {data.get('error', 'unknown')}")]

    names: list[str] = data["data"]
    if search:
        names = [n for n in names if search in n.lower()]
    names = sorted(names)[:limit]

    return [TextContent(type="text", text=f"Metrics matching '{search}' ({len(names)}):\n" + "\n".join(names))]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
