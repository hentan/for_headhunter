import argparse
import csv
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
        raise ParseError(f"Ошибка запроса токена приложения: {exc}")
    if resp.status_code != 200:
        raise ParseError(
            f"Не удалось получить токен приложения (HTTP {resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("access_token")
    if not token:
        raise ParseError("API не вернул access_token")
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
        raise ParseError(f"Ошибка при поиске региона: {exc}")
    if not items:
        raise ParseError(f"Регион '{name}' не найден на hh.ru")
    area_id = items[0]["id"]
    area_name = items[0].get("text") or items[0].get("title", name)
    log(f"Регион: {area_name} (id={area_id})")
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
        raise ParseError(f"API отклонил запрос: {resp.json()}")
    if resp.status_code == 403:
        ip = describe_public_ip()
        ip_note = f" Твой внешний IP: {ip}." if ip else ""
        raise ParseError(
            "API hh.ru вернул 403 Forbidden. Причины: (1) поиск вакансий требует "
            "авторизацию приложения — зарегистрируйся на dev.hh.ru и укажи "
            "Client ID / Client Secret; (2) доступ возможен только с российских "
            f"IP-адресов.{ip_note} Отключи VPN/прокси или используй выход через РФ.")
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
        log("Получаю токен приложения...")
        token = get_app_token(client_id, client_secret)
        log("Токен приложения получен")
    elif getattr(args, "client_id", None) or os.environ.get("HH_CLIENT_ID"):
        raise ParseError("Указан только Client ID — нужен ещё Client Secret")
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
            log(f"Период {window_index + 1}/{len(windows)}: {date_from} — {date_to}")
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
            log(f"Страница {params['page'] + 1}: получено {len(items)}, новых {new_count} "
                f"(всего {len(vacancies)} из ~{data.get('found', 0)})")

            max_pages = min(MAX_PAGES, data.get("pages", 0))
            if len(vacancies) >= args.limit or params["page"] + 1 >= max_pages \
                    or params["page"] + 1 >= args.pages:
                break
            if should_stop and should_stop():
                log("Остановлено пользователем")
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

    log(f"Сохранено: {csv_path}")
    log(f"Сохранено: {json_path}")
    return csv_path, json_path


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Парсер вакансий HeadHunter через api.hh.ru",
        epilog='Пример: py hh_parser.py "python разработчик" --area Москва '
               "--salary 150000 --experience between1And3 --pages 3",
    )
    parser.add_argument("text", nargs="?", default="", help="Поисковый запрос")
    parser.add_argument("--area", help="Город или регион (например, Москва)")
    parser.add_argument("--salary", type=int, help="Минимальная зарплата (рубли)")
    parser.add_argument("--currency", default="RUR", help="Валюта зарплаты (по умолчанию RUR)")
    parser.add_argument("--experience", choices=EXPERIENCE_CHOICES,
                        help="noExperience, between1And3, between3And6, moreThan6")
    parser.add_argument("--days", type=int,
                        help="Только вакансии, опубликованные за последние N дней")
    parser.add_argument("--pages", type=int, default=5, help="Сколько страниц собрать (макс. 200)")
    parser.add_argument("--per-page", type=int, default=50, help="Вакансий на страницу (до 100)")
    parser.add_argument("--limit", type=int, default=10**9, help="Остановиться после N вакансий")
    parser.add_argument("--delay", type=float, default=0.3, help="Пауза между запросами, сек")
    parser.add_argument("--only-with-salary", action="store_true", help="Только вакансии с зарплатой")
    parser.add_argument("--client-id", default=os.environ.get("HH_CLIENT_ID"),
                        help="Client ID приложения hh.ru (или env HH_CLIENT_ID)")
    parser.add_argument("--client-secret", default=os.environ.get("HH_CLIENT_SECRET"),
                        help="Client Secret приложения hh.ru (или env HH_CLIENT_SECRET)")
    parser.add_argument("--out", default="hh_vacancies", help="База имени выходных файлов")
    return parser


def main():
    args = build_arg_parser().parse_args()
    if not any([args.text, args.area, args.salary]):
        sys.exit("Укажите хотя бы один параметр поиска: текст, регион или зарплату")
    try:
        vacancies, found = collect_vacancies(args)
    except ParseError as exc:
        sys.exit(str(exc))
    if not vacancies:
        print("Ничего не найдено по заданным условиям")
        return
    save_outputs(vacancies, found, args)


if __name__ == "__main__":
    main()
