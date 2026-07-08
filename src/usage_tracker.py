from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests


WEBHOOK_TIMEOUT_SECONDS = 60
WEBHOOK_ATTEMPTS = 2

USAGE_HEADERS = [
    "Timestamp",
    "App Name",
    "Email",
    "Filename",
    "Input Unit",
    "Count",
    "Tokens Input",
    "Tokens Output",
    "Model",
    "Cost (INR)",
]

MODEL_PRICING_USD_PER_1M = {
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00},
}

IMAGE_PRICE_USD = {
    "gemini-2.5-flash-image": 0.039,
}


def _secret_value(secrets: Any, name: str) -> str:
    try:
        return str(secrets.get(name, "")).strip()
    except Exception:
        return ""


def _secret_list(secrets: Any, name: str) -> list[str]:
    try:
        raw = secrets.get(name, "")
    except Exception:
        raw = ""
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return _split_config_list(str(raw))


def _split_config_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("\n", ",").replace(";", ",").split(",") if item.strip()]


def _spreadsheet_id(sheet_url_or_id: str) -> str:
    text = sheet_url_or_id.strip()
    if "/spreadsheets/d/" in text:
        return text.split("/spreadsheets/d/", 1)[1].split("/", 1)[0]
    return text


def _sheet_gid(sheet_url_or_id: str) -> str:
    parsed = urlparse(sheet_url_or_id.strip())
    for params_text in (parsed.query, parsed.fragment):
        params = parse_qs(params_text.lstrip("#"))
        gid = params.get("gid", [""])[0]
        if gid:
            return gid
    return ""


def _usage_tracking_sheet_url(secrets: Any) -> str:
    urls = _usage_tracking_sheet_urls(secrets)
    return urls[0] if urls else ""


def _usage_tracking_sheet_urls(secrets: Any) -> list[str]:
    urls = (
        _split_config_list(os.getenv("USAGE_TRACKING_GOOGLE_SHEET_URLS", ""))
        or _secret_list(secrets, "usage_tracking_google_sheet_urls")
    )
    if urls:
        return urls
    single = (
        os.getenv("USAGE_TRACKING_GOOGLE_SHEET_URL", "").strip()
        or _secret_value(secrets, "usage_tracking_google_sheet_url")
        or os.getenv("ALLOWED_EMAILS_GOOGLE_SHEET_URL", "").strip()
        or _secret_value(secrets, "allowed_emails_google_sheet_url")
    )
    return [single] if single else []


def _usage_tracking_gid(secrets: Any, sheet_url: str) -> str:
    gids = _usage_tracking_gids(secrets)
    return gids[0] if gids else _sheet_gid(sheet_url)


def _usage_tracking_gids(secrets: Any) -> list[str]:
    return (
        _split_config_list(os.getenv("USAGE_TRACKING_GIDS", ""))
        or _secret_list(secrets, "usage_tracking_gids")
        or _split_config_list(os.getenv("USAGE_TRACKING_GID", ""))
        or _split_config_list(_secret_value(secrets, "usage_tracking_gid"))
    )


def _usage_tracking_gid_for(secrets: Any, sheet_url: str, index: int) -> str:
    gids = _usage_tracking_gids(secrets)
    if index < len(gids):
        return gids[index]
    return _sheet_gid(sheet_url)


def _usage_tracking_sheet_name(secrets: Any) -> str:
    return os.getenv("USAGE_TRACKING_SHEET_NAME", "").strip() or _secret_value(secrets, "usage_tracking_sheet_name")


def _service_account_info(secrets: Any) -> dict[str, Any] | None:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() or _secret_value(secrets, "google_service_account_json")
    if raw:
        if raw.startswith("{"):
            return json.loads(raw)
        path = Path(raw).expanduser()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

    path_text = (
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        or _secret_value(secrets, "google_service_account_file")
    )
    if path_text:
        path = Path(path_text).expanduser()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def _append_via_webhook(row: list[Any], secrets: Any) -> str | None:
    webhook_url = os.getenv("USAGE_TRACKING_WEBHOOK_URL", "").strip() or _secret_value(secrets, "usage_tracking_webhook_url")
    if not webhook_url:
        return None
    sheet_urls = _usage_tracking_sheet_urls(secrets)
    if not sheet_urls:
        return None
    base_event_id = hashlib.sha256(json.dumps(row, default=str).encode("utf-8")).hexdigest()
    for index, sheet_url in enumerate(sheet_urls):
        payload = {
            "eventId": f"{base_event_id}:{index}",
            "spreadsheetId": _spreadsheet_id(sheet_url),
            "headers": USAGE_HEADERS,
            "values": [row],
            "sheetId": _usage_tracking_gid_for(secrets, sheet_url, index),
            "sheetName": _usage_tracking_sheet_name(secrets),
        }
        last_error: Exception | None = None
        for _ in range(WEBHOOK_ATTEMPTS):
            try:
                response = requests.post(webhook_url, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS)
                break
            except requests.Timeout as exc:
                last_error = exc
        else:
            raise RuntimeError(
                f"Webhook timed out for destination {index + 1} after {WEBHOOK_ATTEMPTS} attempt(s) "
                f"with {WEBHOOK_TIMEOUT_SECONDS}s timeout."
            ) from last_error
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            snippet = response.text.strip().replace("\n", " ")[:160]
            raise RuntimeError(f"Webhook did not return JSON. Response starts with: {snippet}") from exc
        if not payload.get("ok"):
            raise RuntimeError(f"Webhook rejected usage row for destination {index + 1}: {payload.get('error') or payload}")
    return f"google_sheet_webhook:{len(sheet_urls)}"


def _quote_sheet_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _sheet_title_for_gid(spreadsheet_id: str, gid: str, token: str) -> str:
    if not gid:
        return ""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=sheets.properties"
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    response.raise_for_status()
    for sheet in response.json().get("sheets", []):
        props = sheet.get("properties", {})
        if str(props.get("sheetId", "")) == str(gid):
            return str(props.get("title", "")).strip()
    return ""


def _append_via_service_account(row: list[Any], secrets: Any) -> str | None:
    sheet_urls = _usage_tracking_sheet_urls(secrets)
    if not sheet_urls:
        return None

    info = _service_account_info(secrets)
    if not info:
        return None

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    credentials.refresh(Request())

    base_sheet_range = (
        os.getenv("USAGE_TRACKING_RANGE", "").strip()
        or _secret_value(secrets, "usage_tracking_range")
        or "A:J"
    )
    sheet_name = _usage_tracking_sheet_name(secrets)
    for index, sheet_url in enumerate(sheet_urls):
        spreadsheet_id = _spreadsheet_id(sheet_url)
        gid = _usage_tracking_gid_for(secrets, sheet_url, index)
        sheet_range = base_sheet_range
        if "!" not in sheet_range:
            if sheet_name:
                sheet_range = f"{_quote_sheet_title(sheet_name)}!{sheet_range}"
            elif gid:
                sheet_title = _sheet_title_for_gid(spreadsheet_id, gid, credentials.token)
                if sheet_title:
                    sheet_range = f"{_quote_sheet_title(sheet_title)}!{sheet_range}"

        encoded_range = quote(sheet_range, safe="")
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
            f"{encoded_range}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
        )
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {credentials.token}"},
            json={"values": [row]},
            timeout=15,
        )
        response.raise_for_status()
    return f"google_sheets_api:{len(sheet_urls)}"


def _append_local(row: list[Any], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(USAGE_HEADERS)
        writer.writerow(row)
    return "local_csv"


def summarize_usage(metadata: dict[str, Any]) -> dict[str, int]:
    tokens_input = 0
    tokens_output = 0
    redrawn_images = 0
    for call in metadata.get("api_calls", []) or []:
        if not isinstance(call, dict):
            continue
        tokens_input += int(call.get("tokens_input") or 0)
        tokens_output += int(call.get("tokens_output") or 0)
        redrawn_images += int(call.get("redrawn") or 0)
    return {
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "redrawn_images": redrawn_images,
    }


def estimate_cost_inr(
    model: str,
    tokens_input: int,
    tokens_output: int,
    *,
    image_model: str = "",
    redrawn_images: int = 0,
    usd_to_inr: float = 83.0,
) -> float:
    pricing = MODEL_PRICING_USD_PER_1M.get(model, {})
    text_cost_usd = (
        (tokens_input / 1_000_000) * float(pricing.get("input", 0.0))
        + (tokens_output / 1_000_000) * float(pricing.get("output", 0.0))
    )
    image_cost_usd = redrawn_images * IMAGE_PRICE_USD.get(image_model, 0.0)
    return round((text_cost_usd + image_cost_usd) * usd_to_inr, 4)


def build_usage_row(
    *,
    app_name: str,
    email: str,
    filename: str,
    input_unit: str,
    count: int,
    model: str,
    metadata: dict[str, Any],
    image_model: str,
    usd_to_inr: float,
) -> list[Any]:
    usage = summarize_usage(metadata)
    cost_inr = estimate_cost_inr(
        model,
        usage["tokens_input"],
        usage["tokens_output"],
        image_model=image_model,
        redrawn_images=usage["redrawn_images"],
        usd_to_inr=usd_to_inr,
    )
    return [
        datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        app_name,
        email,
        filename,
        input_unit,
        count,
        usage["tokens_input"],
        usage["tokens_output"],
        model,
        cost_inr,
    ]


def append_usage_row(row: list[Any], *, secrets: Any, local_path: Path) -> str:
    errors: list[str] = []
    try:
        method = _append_via_webhook(row, secrets)
        if method:
            return method
    except Exception as exc:
        errors.append(f"webhook failed: {exc}")

    try:
        method = _append_via_service_account(row, secrets)
        if method:
            return method
    except Exception as exc:
        errors.append(f"service account failed: {exc}")

    method = _append_local(row, local_path)
    if errors:
        return f"{method}; Google Sheet not updated ({'; '.join(errors)})"
    return method
