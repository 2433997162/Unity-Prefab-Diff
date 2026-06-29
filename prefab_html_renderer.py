#!/usr/bin/env python3
"""Data injector for Unity Prefab/Scene diff HTML reports."""

import json
import os
from datetime import datetime

REPORT_MODE_FULL = "full"
REPORT_MODE_EMBED = "embed"

TEMPLATE_NAME = "prefab_diff_template.html"
DATA_PLACEHOLDER = "__PREFAB_DIFF_DATA__"


def normalize_report_mode(mode: str = "") -> str:
    mode = (mode or "").strip().lower()
    return REPORT_MODE_EMBED if mode in ("embed", "embedded", "sourcegit") else REPORT_MODE_FULL


_normalize_report_mode = normalize_report_mode


def _template_path(template_path: str = "") -> str:
    if template_path:
        return os.path.abspath(template_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), TEMPLATE_NAME)


def _load_template(template_path: str = "") -> str:
    path = _template_path(template_path)
    with open(path, "r", encoding="utf-8") as f:
        template = f.read()
    if DATA_PLACEHOLDER not in template:
        raise ValueError(f"HTML template missing placeholder: {DATA_PLACEHOLDER}")
    return template


def _json_for_html(data) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return payload.replace("</", "<\\/")


def _pairs(mapping):
    if not mapping:
        return []
    return [{"key": str(k), "value": "" if v is None else str(v)} for k, v in mapping.items()]


def _changed_pairs(mapping):
    if not mapping:
        return []
    rows = []
    for key, value in mapping.items():
        old_value, new_value = value if isinstance(value, (list, tuple)) and len(value) == 2 else ("", value)
        rows.append(
            {
                "key": str(key),
                "old": "" if old_value is None else str(old_value),
                "new": "" if new_value is None else str(new_value),
            }
        )
    return rows


def _node_payload(path, props=None, status=""):
    path = str(path)
    name = path.split("/")[-1] if path else "(root)"
    return {
        "path": path,
        "name": name,
        "status": status,
        "props": _pairs(props or {}),
    }


def _modified_payload(path, changes):
    changes = changes or {}
    path = str(path)
    name = path.split("/")[-1] if path else "(root)"
    return {
        "path": path,
        "name": name,
        "status": "modified",
        "added": _pairs(changes.get("added", {})),
        "removed": _pairs(changes.get("removed", {})),
        "changed": _changed_pairs(changes.get("changed", {})),
    }


def build_prefab_report_data(filename, diff_result, old_nodes=None, new_nodes=None):
    diff_result = diff_result or {}
    old_nodes = old_nodes or {}
    new_nodes = new_nodes or {}

    added = [_node_payload(path, props, "added") for path, props in diff_result.get("added_nodes", {}).items()]
    removed = [_node_payload(path, props, "removed") for path, props in diff_result.get("removed_nodes", {}).items()]
    modified = [_modified_payload(path, changes) for path, changes in diff_result.get("modified_nodes", {}).items()]

    return {
        "filename": filename or "prefab",
        "added": added,
        "removed": removed,
        "modified": modified,
        "oldPaths": [str(path) for path in old_nodes.keys()],
        "newPaths": [str(path) for path in new_nodes.keys()],
        "counts": {
            "addedNodes": len(added),
            "removedNodes": len(removed),
            "modifiedNodes": len(modified),
            "changedProperties": sum(
                len(item["added"]) + len(item["removed"]) + len(item["changed"]) for item in modified
            ),
        },
    }


def build_collection_data(title, reports, report_mode=REPORT_MODE_FULL, summary=None):
    report_items = []
    for index, report in enumerate(reports or []):
        item = build_prefab_report_data(
            report.get("filename") or f"prefab-{index + 1}",
            report.get("diff_result"),
            report.get("old_nodes"),
            report.get("new_nodes"),
        )
        item["id"] = f"report-{index}"
        report_items.append(item)

    return {
        "schemaVersion": 1,
        "reportMode": normalize_report_mode(report_mode),
        "title": title or "Prefab Diff",
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary or {},
        "reports": report_items,
    }


def render_html_from_data(data, template_path: str = "") -> str:
    template = _load_template(template_path)
    return template.replace(DATA_PLACEHOLDER, _json_for_html(data))


def generate_prefab_collection_html(
    title,
    reports,
    report_mode=REPORT_MODE_FULL,
    summary=None,
    template_path: str = "",
):
    data = build_collection_data(title, reports, report_mode, summary)
    return render_html_from_data(data, template_path)


def generate_prefab_html(
    filename,
    diff_result,
    old_nodes=None,
    new_nodes=None,
    report_mode=REPORT_MODE_FULL,
    template_path: str = "",
):
    summary = {
        "title": "Prefab 变更报告",
        "label": filename or "prefab",
        "fileCount": 1,
    }
    reports = [
        {
            "filename": filename,
            "diff_result": diff_result,
            "old_nodes": old_nodes,
            "new_nodes": new_nodes,
        }
    ]
    return generate_prefab_collection_html(filename, reports, report_mode, summary, template_path)
