import argparse
import base64
import csv
import ctypes
from ctypes import wintypes as wt
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.hh.ru/vacancies"
TOKEN_URL = "https://api.hh.ru/token"
SUGGEST_URL = "https://api.hh.ru/suggests/areas"
HEADERS = {"User-Agent": "hh-vacancies-parser/1.0"}
MAX_PAGES = 200

EXPERIENCE_CHOICES = ["noExperience", "between1And3", "between3And6", "moreThan6"]


class ParseError(Exception):
    pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.c_void_p)]


def protect_secret(text):
    raw = text.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.c_void_p))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData failed")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))
    return base64.b64encode(raw).decode("ascii")


def unprotect_secret(encoded):
    try:
        raw = base64.b64decode(encoded)
    except (ValueError, TypeError):
        return ""
    buffer = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.c_void_p))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        return ""
    try:
        data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(blob_out.pbData))
    return data.decode("utf-16-le")


def make_session(access_token=None):
    session = requests.Session()
    session.headers.update(HEADERS)
    if access_token:
        session.headers["Authorization"] = f"Bearer {access_token}"
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_app_token(client_id, client_secret):
    try:
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }, timeout=15)
    except requests.RequestException as exc:
        raise ParseError(f"РћС€РёР±РєР° Р·Р°РїСЂРѕСЃР° С‚РѕРєРµРЅР° РїСЂРёР»РѕР¶РµРЅРёСЏ: {exc}")
    if resp.status_code != 200:
        raise ParseError(
            f"РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»СѓС‡РёС‚СЊ С‚РѕРєРµРЅ РїСЂРёР»РѕР¶РµРЅРёСЏ (HTTP {resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("access_token")
    if not token:
        raise ParseError("API РЅРµ РІРµСЂРЅСѓР» access_token")
    return token


def strip_html(value):
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def resolve_area(session, name, log=print):
    try:
        resp = session.get(SUGGEST_URL, params={"text": name}, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items") or []
    except requests.RequestException as exc:
        raise ParseError(f"РћС€РёР±РєР° РїСЂРё РїРѕРёСЃРєРµ СЂРµРіРёРѕРЅР°: {exc}")
    if not items:
        raise ParseError(f"Р РµРіРёРѕРЅ '{name}' РЅРµ РЅР°Р№РґРµРЅ РЅР° hh.ru")
    area_id = items[0]["id"]
    area_name = items[0].get("text") or items[0].get("title", name)
    log(f"Р РµРіРёРѕРЅ: {area_name} (id={area_id})")
    return area_id


def parse_vacancy(item):
    salary = item.get("salary") or {}
    employer = item.get("employer") or {}
    area = item.get("area") or {}
    experience = item.get("experience") or {}
    snippet = item.get("snippet") or {}

    def money(key):
        value = salary.get(key)
        return "" if value is None else value

    return {
        "id": item.get("id", ""),
        "name": item.get("name", ""),
        "employer": employer.get("name", ""),
        "area": area.get("name", ""),
        "experience": (experience.get("name") or "").strip(),
        "employment": (item.get("employment") or {}).get("name", ""),
        "schedule": (item.get("schedule") or {}).get("name", ""),
        "salary_from": money("from"),
        "salary_to": money("to"),
        "currency": salary.get("currency", ""),
        "gross": salary.get("gross"),
        "published_at": item.get("published_at", ""),
        "url": item.get("alternate_url", ""),
        "requirement": strip_html(snippet.get("requirement")),
        "responsibility": strip_html(snippet.get("responsibility")),
    }


def describe_public_ip():
    try:
        resp = requests.get("https://ipinfo.io/json", timeout=5)
        data = resp.json()
        country = data.get("country") or "?"
        return f"{data.get('ip')} ({country})"
    except Exception:
        return None


def fetch_page(session, params):
    resp = session.get(BASE_URL, params=params, timeout=15)
    if resp.status_code == 400:
        raise ParseError(f"API РѕС‚РєР»РѕРЅРёР» Р·Р°РїСЂРѕСЃ: {resp.json()}")
    if resp.status_code == 403:
        ip = describe_public_ip()
        ip_note = f" РўРІРѕР№ РІРЅРµС€РЅРёР№ IP: {ip}." if ip else ""
        raise ParseError(
            "API hh.ru РІРµСЂРЅСѓР» 403 Forbidden. РџСЂРёС‡РёРЅС‹: (1) РїРѕРёСЃРє РІР°РєР°РЅСЃРёР№ С‚СЂРµР±СѓРµС‚ "
            "Р°РІС‚РѕСЂРёР·Р°С†РёСЋ РїСЂРёР»РѕР¶РµРЅРёСЏ вЂ” Р·Р°СЂРµРіРёСЃС‚СЂРёСЂСѓР№СЃСЏ РЅР° dev.hh.ru Рё СѓРєР°Р¶Рё "
            "Client ID / Client Secret; (2) РґРѕСЃС‚СѓРї РІРѕР·РјРѕР¶РµРЅ С‚РѕР»СЊРєРѕ СЃ СЂРѕСЃСЃРёР№СЃРєРёС… "
            f"IP-Р°РґСЂРµСЃРѕРІ.{ip_note} РћС‚РєР»СЋС‡Рё VPN/РїСЂРѕРєСЃРё РёР»Рё РёСЃРїРѕР»СЊР·СѓР№ РІС‹С…РѕРґ С‡РµСЂРµР· Р Р¤.")
    resp.raise_for_status()
    return resp.json()


WINDOW_DAYS = 30


def build_date_windows(days, now=None):
    now = now or datetime.now()
    start = now - timedelta(days=days)
    step = timedelta(days=WINDOW_DAYS)
    windows = []
    cursor = start
    while cursor < now:
        end = min(cursor + step, now)
        windows.append((cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                        end.strftime("%Y-%m-%dT%H:%M:%S")))
        cursor = end
    return windows


def collect_vacancies(args, log=print, should_stop=None):
    client_id = getattr(args, "client_id", None) or os.environ.get("HH_CLIENT_ID")
    client_secret = getattr(args, "client_secret", None) or os.environ.get("HH_CLIENT_SECRET")
    token = None
    if client_id and client_secret:
        log("РџРѕР»СѓС‡Р°СЋ С‚РѕРєРµРЅ РїСЂРёР»РѕР¶РµРЅРёСЏ...")
        token = get_app_token(client_id, client_secret)
        log("РўРѕРєРµРЅ РїСЂРёР»РѕР¶РµРЅРёСЏ РїРѕР»СѓС‡РµРЅ")
    elif getattr(args, "client_id", None) or os.environ.get("HH_CLIENT_ID"):
        raise ParseError("РЈРєР°Р·Р°РЅ С‚РѕР»СЊРєРѕ Client ID вЂ” РЅСѓР¶РµРЅ РµС‰С‘ Client Secret")
    session = make_session(token)
    base_params = {
        "text": args.text,
        "per_page": min(args.per_page, 100),
    }
    if args.area:
        base_params["area"] = resolve_area(session, args.area, log)
    if args.salary:
        base_params["salary"] = args.salary
        base_params["currency"] = args.currency
    if args.experience:
        base_params["experience"] = args.experience
    if args.only_with_salary:
        base_params["only_with_salary"] = "true"

    days = getattr(args, "days", None)
    windows = build_date_windows(days) if days else [(None, None)]
    vacancies = []
    seen_ids = set()
    found_total = 0

    for window_index, (date_from, date_to) in enumerate(windows):
        if len(windows) > 1:
            log(f"РџРµСЂРёРѕРґ {window_index + 1}/{len(windows)}: {date_from} вЂ” {date_to}")
        params = dict(base_params)
        params["page"] = 0
        if date_from:
            params["date_from"] = date_from
            params["date_to"] = date_to
        stop_all = False
        while True:
            data = fetch_page(session, params)
            found_total += data.get("found", 0)
            items = data.get("items") or []
            new_count = 0
            for item in items:
                vacancy_id = item.get("id")
                if vacancy_id in seen_ids:
                    continue
                seen_ids.add(vacancy_id)
                vacancies.append(parse_vacancy(item))
                new_count += 1
            log(f"РЎС‚СЂР°РЅРёС†Р° {params['page'] + 1}: РїРѕР»СѓС‡РµРЅРѕ {len(items)}, РЅРѕРІС‹С… {new_count} "
                f"(РІСЃРµРіРѕ {len(vacancies)} РёР· ~{data.get('found', 0)})")

            max_pages = min(MAX_PAGES, data.get("pages", 0))
            if len(vacancies) >= args.limit or params["page"] + 1 >= max_pages \
                    or params["page"] + 1 >= args.pages:
                break
            if should_stop and should_stop():
                log("РћСЃС‚Р°РЅРѕРІР»РµРЅРѕ РїРѕР»СЊР·РѕРІР°С‚РµР»РµРј")
                stop_all = True
                break
            params["page"] += 1
            time.sleep(args.delay)
        if stop_all or len(vacancies) >= args.limit:
            break

    return vacancies, found_total


def save_outputs(vacancies, found, args, log=print):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = Path(f"{args.out}_{stamp}")

    payload = {
        "source": "api.hh.ru",
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "query": {
            "text": args.text,
            "area": args.area,
            "salary": args.salary,
            "currency": args.currency,
            "experience": args.experience,
            "days": getattr(args, "days", None),
            "only_with_salary": args.only_with_salary,
        },
        "found_total": found,
        "collected": len(vacancies),
        "vacancies": vacancies,
    }
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = base.with_suffix(".csv")
    fieldnames = list(parse_vacancy({}).keys())
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(vacancies)

    log(f"РЎРѕС…СЂР°РЅРµРЅРѕ: {csv_path}")
    log(f"РЎРѕС…СЂР°РЅРµРЅРѕ: {json_path}")
    return csv_path, json_path


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="РџР°СЂСЃРµСЂ РІР°РєР°РЅСЃРёР№ HeadHunter С‡РµСЂРµР· api.hh.ru",
        epilog='РџСЂРёРјРµСЂ: py hh_parser.py "python СЂР°Р·СЂР°Р±РѕС‚С‡РёРє" --area РњРѕСЃРєРІР° '
               "--salary 150000 --experience between1And3 --pages 3",
    )
    parser.add_argument("text", nargs="?", default="", help="РџРѕРёСЃРєРѕРІС‹Р№ Р·Р°РїСЂРѕСЃ")
    parser.add_argument("--area", help="Р“РѕСЂРѕРґ РёР»Рё СЂРµРіРёРѕРЅ (РЅР°РїСЂРёРјРµСЂ, РњРѕСЃРєРІР°)")
    parser.add_argument("--salary", type=int, help="РњРёРЅРёРјР°Р»СЊРЅР°СЏ Р·Р°СЂРїР»Р°С‚Р° (СЂСѓР±Р»Рё)")
    parser.add_argument("--currency", default="RUR", help="Р’Р°Р»СЋС‚Р° Р·Р°СЂРїР»Р°С‚С‹ (РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ RUR)")
    parser.add_argument("--experience", choices=EXPERIENCE_CHOICES,
                        help="noExperience, between1And3, between3And6, moreThan6")
    parser.add_argument("--days", type=int,
                        help="РўРѕР»СЊРєРѕ РІР°РєР°РЅСЃРёРё, РѕРїСѓР±Р»РёРєРѕРІР°РЅРЅС‹Рµ Р·Р° РїРѕСЃР»РµРґРЅРёРµ N РґРЅРµР№")
    parser.add_argument("--pages", type=int, default=5, help="РЎРєРѕР»СЊРєРѕ СЃС‚СЂР°РЅРёС† СЃРѕР±СЂР°С‚СЊ (РјР°РєСЃ. 200)")
    parser.add_argument("--per-page", type=int, default=50, help="Р’Р°РєР°РЅСЃРёР№ РЅР° СЃС‚СЂР°РЅРёС†Сѓ (РґРѕ 100)")
    parser.add_argument("--limit", type=int, default=10**9, help="РћСЃС‚Р°РЅРѕРІРёС‚СЊСЃСЏ РїРѕСЃР»Рµ N РІР°РєР°РЅСЃРёР№")
    parser.add_argument("--delay", type=float, default=0.3, help="РџР°СѓР·Р° РјРµР¶РґСѓ Р·Р°РїСЂРѕСЃР°РјРё, СЃРµРє")
    parser.add_argument("--only-with-salary", action="store_true", help="РўРѕР»СЊРєРѕ РІР°РєР°РЅСЃРёРё СЃ Р·Р°СЂРїР»Р°С‚РѕР№")
    parser.add_argument("--client-id", default=os.environ.get("HH_CLIENT_ID"),
                        help="Client ID РїСЂРёР»РѕР¶РµРЅРёСЏ hh.ru (РёР»Рё env HH_CLIENT_ID)")
    parser.add_argument("--client-secret", default=os.environ.get("HH_CLIENT_SECRET"),
                        help="Client Secret РїСЂРёР»РѕР¶РµРЅРёСЏ hh.ru (РёР»Рё env HH_CLIENT_SECRET)")
    parser.add_argument("--out", default="hh_vacancies", help="Р‘Р°Р·Р° РёРјРµРЅРё РІС‹С…РѕРґРЅС‹С… С„Р°Р№Р»РѕРІ")
    return parser


def main():
    args = build_arg_parser().parse_args()
    if not any([args.text, args.area, args.salary]):
        sys.exit("РЈРєР°Р¶РёС‚Рµ С…РѕС‚СЏ Р±С‹ РѕРґРёРЅ РїР°СЂР°РјРµС‚СЂ РїРѕРёСЃРєР°: С‚РµРєСЃС‚, СЂРµРіРёРѕРЅ РёР»Рё Р·Р°СЂРїР»Р°С‚Сѓ")
    try:
        vacancies, found = collect_vacancies(args)
    except ParseError as exc:
        sys.exit(str(exc))
    if not vacancies:
        print("РќРёС‡РµРіРѕ РЅРµ РЅР°Р№РґРµРЅРѕ РїРѕ Р·Р°РґР°РЅРЅС‹Рј СѓСЃР»РѕРІРёСЏРј")
        return
    save_outputs(vacancies, found, args)


if __name__ == "__main__":
    main()
