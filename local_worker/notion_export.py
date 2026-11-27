"""Deterministic local-only exporter. No model gets credentials or mutation tools."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib import request, error

from .core import WorkerError, SingleWorker, atomic_json, read_json, safe_id, local_root
from .generation import date_of, title_of, missing_metadata, validate_body


def rich_text(text: str) -> list[dict]:
    result = []
    for index, value in enumerate(re.split(r"\*\*(.*?)\*\*", text.replace("<br>", "\n"))):
        for offset in range(0, len(value), 1800):
            result.append({"type": "text", "text": {"content": value[offset:offset + 1800]}, "annotations": {"bold": bool(index % 2)}})
    if len(result) > 100:
        raise WorkerError("notion_text_limit", "Notion 문단이나 표 셀 길이를 줄이세요.", "needs_review")
    return result


def markdown_blocks(markdown: str) -> list[dict]:
    # Markdown title/metadata belong in DB properties, never repeated in the body.
    match = re.search(r"^## 1\. 회의 결론\s*$", markdown, re.M)
    if not match:
        raise WorkerError("notion_body", "회의 결론부터 시작하는 검토된 본문이 필요합니다.", "needs_review")
    lines = markdown[match.start():].splitlines()
    blocks, index = [], 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        if line.startswith("|"):
            rows = [line]
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(lines[index].strip())
                index += 1
            cells = [re.split(r"(?<!\\)\|", row.strip("|")) for row in rows]
            cells = [[cell.strip().replace("\\|", "|") for cell in row] for row in cells]
            cells = [row for row in cells if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in row)]
            width = len(cells[0])
            if any(len(row) != width for row in cells) or width > 20:
                raise WorkerError("notion_table", "표 열 수가 일치하지 않습니다.", "needs_review")
            # A header-only table is retained without inventing rows.
            for offset in range(1, max(2, len(cells)), 90):
                batch = [cells[0]] + cells[offset:offset + 90]
                blocks.append({"object": "block", "type": "table", "table": {"table_width": width,
                    "has_column_header": True, "has_row_header": False,
                    "children": [{"object": "block", "type": "table_row", "table_row": {"cells": [rich_text(cell) for cell in row]}} for row in batch]}})
            continue
        heading = re.match(r"^(#{2,4})\s+(.+)", line)
        if heading:
            kind, text = "heading_" + str(len(heading[1]) - 1), heading[2]
        elif line.startswith("- "):
            kind, text = "bulleted_list_item", line[2:]
        elif re.match(r"^\d+\.\s", line):
            kind, text = "numbered_list_item", re.sub(r"^\d+\.\s", "", line)
        elif line.startswith("<!--") or line == "---":
            continue
        else:
            kind, text = "paragraph", line
        blocks.append({"object": "block", "type": kind, kind: {"rich_text": rich_text(text)}})
    return blocks


def notion_token(token_file: str | None = None) -> str:
    if os.name != "nt":
        raise WorkerError("notion_local_only", "Notion 내보내기는 인증을 보관한 Windows PC에서만 실행합니다.", "needs_setup")
    # Token travels only from DPAPI to this local process's pipe, never argv/files/logs.
    script = "$ErrorActionPreference='Stop'; $p=$env:MEETING_NOTION_TOKEN_FILE; if (-not $p) { $p=Join-Path $env:LOCALAPPDATA 'CodexNotionLocal\\token.xml' }; $s=Import-Clixml -LiteralPath $p; [Console]::Write([System.Net.NetworkCredential]::new('', $s).Password)"
    env = {**os.environ}
    if token_file:
        env["MEETING_NOTION_TOKEN_FILE"] = str(Path(token_file).resolve())
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=20, creationflags=0x08000000, env=env)
    token = completed.stdout.strip()
    if completed.returncode or not token:
        raise WorkerError("notion_token_missing", "이 PC의 Windows DPAPI token.xml에 Notion 인증을 설정하세요(local_worker/README.md).", "needs_setup")
    return token


class NotionClient:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, path: str, body=None):
        from .client import NoRedirect
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        if payload and len(payload) > 450000:
            raise WorkerError("notion_payload_limit", "Notion 요청 크기를 초과했습니다.", "needs_review")
        req = request.Request("https://api.notion.com/v1" + path, payload,
            {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Notion-Version": "2025-09-03"}, method=method)
        try:
            with request.build_opener(NoRedirect()).open(req, timeout=60) as response:
                return json.load(response)
        except error.HTTPError as exc:
            if exc.code in {400, 401, 403, 404, 409, 429}:
                raise WorkerError("notion_http_" + str(exc.code), "Notion 속성·권한·호출 한도를 확인하세요.", "needs_setup" if exc.code in {401, 403, 404} else "failed", True) from None
            raise WorkerError("notion_unknown", "Notion 요청 결과가 불명확합니다. 페이지와 로컬 기록을 확인하세요.", "unknown_outcome") from None
        except (error.URLError, TimeoutError, OSError):
            raise WorkerError("notion_unknown", "Notion 응답이 끊겨 자동 재전송을 차단했습니다.", "unknown_outcome") from None


def typed_properties(job: dict, schema: dict, config: dict) -> dict:
    available = schema["properties"]
    titles = [name for name, value in available.items() if value["type"] == "title"]
    if len(titles) != 1:
        raise WorkerError("notion_schema", "대상 DB 제목 속성을 확인하세요.", "needs_setup")
    properties = {titles[0]: {"title": rich_text(title_of(job))}}
    values = {"일시": ("date", {"start": job["metadata"]["datetime"]}),
              "장소": ("rich_text", rich_text(job["metadata"]["place"])),
              "참석자": ("rich_text", rich_text(job["metadata"]["attendees"])),
              "작성자": ("rich_text", rich_text(job["metadata"]["author"]))}
    for name, (kind, value) in values.items():
        if name not in available or available[name]["type"] != kind:
            raise WorkerError("notion_schema", f"대상 DB의 {name} 속성 형식을 확인하세요.", "needs_setup")
        properties[name] = {kind: value}
    selections = {"회의 형태": job["metadata"]["meeting_format"], **config.get("notion_selects", {})}
    for name, value in selections.items():
        field = available.get(name)
        if not field or field["type"] not in {"select", "status"}:
            raise WorkerError("notion_schema", f"대상 DB의 {name} 선택 속성을 확인하세요.", "needs_setup")
        kind = field["type"]
        if value not in {option["name"] for option in field[kind]["options"]}:
            raise WorkerError("notion_option", f"대상 DB의 {name} 기존 옵션과 메타데이터를 맞추세요.", "needs_review")
        properties[name] = {kind: {"name": value}}
    return properties


def _rich_signature(items: list[dict]) -> list:
    # Notion may split adjacent runs; merge by the only supported inline style.
    runs = []
    for item in items:
        value = item.get("text", {}).get("content", item.get("plain_text", ""))
        bold = bool(item.get("annotations", {}).get("bold", False))
        if runs and runs[-1][1] == bold:
            runs[-1][0] += value
        elif value:
            runs.append([value, bold])
    return runs


def _block_signature(block: dict, client=None):
    kind = block["type"]
    data = block[kind]
    if kind == "table":
        children = data.get("children")
        if children is None and client:
            children, cursor = [], ""
            from urllib.parse import urlencode
            while True:
                result = client.call("GET", f"/blocks/{block['id']}/children?" + urlencode({"page_size": 100, **({"start_cursor": cursor} if cursor else {})}))
                children += result.get("results", [])
                if not result.get("has_more"):
                    break
                cursor = result["next_cursor"]
        return [kind, data["table_width"], data["has_column_header"], [_block_signature(child, client) for child in children or []]]
    if kind == "table_row":
        return [kind, [_rich_signature(cell) for cell in data["cells"]]]
    return [kind, _rich_signature(data.get("rich_text", []))]


def export(job: dict, config: dict, *, reviewed: bool, client=None, state_root: Path | None = None) -> dict:
    root = state_root or local_root() / "exports"
    target = config.get("notion_targets", {}).get(job.get("meeting_type"), {})
    identifier = f"{job.get('id')}:{(job.get('draft') or {}).get('revision')}:{target.get('data_source_id')}"
    lock = root / ".locks" / hashlib.sha256(identifier.encode()).hexdigest()
    with SingleWorker(lock):
        return _export(job, config, reviewed=reviewed, client=client, state_root=root)


def _export(job: dict, config: dict, *, reviewed: bool, client=None, state_root: Path | None = None) -> dict:
    if not reviewed:
        raise WorkerError("export_review_required", "검토를 마친 회의록만 --reviewed로 내보낼 수 있습니다.", "needs_review")
    draft = job.get("draft")
    if not draft or draft["transcript_revision"] != job["transcript_revision"]:
        raise WorkerError("stale_draft", "최신 검토 녹취로 작성한 회의록을 선택하세요.", "needs_review")
    if missing_metadata(job) or date_of(job) is None:
        raise WorkerError("missing_metadata", "Notion 저장 전에 제목과 필수 메타데이터를 확인하세요.", "needs_review")
    target = config.get("notion_targets", {}).get(job["meeting_type"])
    if not target:
        raise WorkerError("notion_target_missing", "이 회의 유형의 로컬 허용 DB를 먼저 설정하세요.", "needs_setup")
    database_id, source_id = safe_id(target["database_id"]), safe_id(target["data_source_id"])
    match = re.search(r"^## 1\. 회의 결론\s*$", draft["content"], re.M)
    if not match:
        raise WorkerError("notion_body", "양식에 맞는 회의록 본문이 필요합니다.", "needs_review")
    validate_body(draft["content"][match.start():], job)
    blocks = markdown_blocks(draft["content"])
    identifier = f"{safe_id(job['id'])}:{draft['revision']}:{source_id}"
    root = state_root or local_root() / "exports"
    path = root / (hashlib.sha256(identifier.encode()).hexdigest() + ".json")
    content_hash = hashlib.sha256(json.dumps({"metadata": job["metadata"], "draft": draft}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    state = read_json(path, {"state": "pending", "content_hash": content_hash, "identifier": identifier, "completed_blocks": 0})
    if state["content_hash"] != content_hash:
        raise WorkerError("export_revision_reused", "같은 회의록 버전에 다른 내용이 있습니다. 새 버전으로 저장하세요.", "needs_review")
    if state["state"] == "completed":
        return state
    if state["state"] in {"creating", "appending", "unknown_outcome"}:
        raise WorkerError("export_unknown", "이전 Notion 요청 결과가 불명확합니다. 자동 재전송 없이 로컬 기록을 확인하세요.", "unknown_outcome")
    client = client or NotionClient(notion_token(config.get("notion_token_file")))
    schema = client.call("GET", "/data_sources/" + source_id)
    if schema.get("id", "").replace("-", "") != source_id.replace("-", "") or schema.get("parent", {}).get("database_id", "").replace("-", "") != database_id.replace("-", ""):
        raise WorkerError("notion_target_mismatch", "DB와 데이터 소스 ID가 로컬 허용 목록과 다릅니다.", "needs_setup")
    properties = typed_properties(job, schema, config)
    try:
        if not state.get("page_id"):
            state["state"] = "creating"
            atomic_json(path, state)  # Persist intention before every possibly effective mutation.
            page = client.call("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": source_id}, "properties": properties})
            state.update(page_id=page["id"], page_url=page.get("url", ""), state="pending")
            atomic_json(path, state)
        while state["completed_blocks"] < len(blocks):
            start = state["completed_blocks"]
            batch = []
            for block in blocks[start:start + 90]:
                candidate = batch + [block]
                # Count nested table rows against Notion's 1,000 block payload limit too.
                if len(json.dumps({"children": candidate}, ensure_ascii=False).encode()) > 400000 or sum(1 + len(value.get("table", {}).get("children", [])) for value in candidate) > 900:
                    break
                batch = candidate
            if not batch:
                raise WorkerError("notion_block_limit", "Notion 표나 문단이 너무 큽니다.", "needs_review")
            state.update(state="appending", inflight_start=start, inflight_count=len(batch))
            atomic_json(path, state)
            response = client.call("PATCH", f"/blocks/{state['page_id']}/children", {"children": batch})
            if len(response.get("results", [])) != len(batch):
                raise WorkerError("notion_append_unknown", "Notion이 일부 블록만 확인했습니다. 자동 재전송을 차단합니다.", "unknown_outcome")
            state.update(completed_blocks=start + len(batch), state="pending")
            atomic_json(path, state)
        # Read back the actual page children before marking complete.
        actual, cursor = [], ""
        from urllib.parse import urlencode
        while True:
            response = client.call("GET", f"/blocks/{state['page_id']}/children?" + urlencode({"page_size": 100, **({"start_cursor": cursor} if cursor else {})}))
            actual += response.get("results", [])
            if not response.get("has_more"):
                break
            cursor = response["next_cursor"]
        if [_block_signature(item, client) for item in actual] != [_block_signature(item) for item in blocks]:
            raise WorkerError("notion_verification", "저장된 Notion 본문·표·강조 내용이 검토한 초안과 다릅니다.", "needs_review")
        state["state"] = "completed"
        atomic_json(path, state)
        return state
    except WorkerError as exc:
        # Definite precondition/HTTP rejections may be manually retried on the same page.
        state["state"] = "unknown_outcome" if exc.state == "unknown_outcome" else "failed"
        state["last_error"] = exc.code
        atomic_json(path, state)
        raise
    except Exception:
        state.update(state="unknown_outcome", last_error="notion_unexpected")
        atomic_json(path, state)
        raise WorkerError("notion_unknown", "Notion 응답 처리 결과를 확인해야 합니다. 자동 재전송을 차단했습니다.", "unknown_outcome") from None
