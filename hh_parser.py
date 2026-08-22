import argparse
import csv
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.hh.ru/vacancies"
SUGGEST_URL = "https://api.hh.ru/suggests/areas"
HEADERS = {"User-Agent": "hh-vacancies-parser/1.0"}
MAX_PAGES = 200

EXPERIENCE_CHOICES = ["noExperience", "between1And3", "between3And6", "moreThan6"]


class ParseError(Exception):
    pass


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


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


def fetch_page(session, params):
    resp = session.get(BASE_URL, params=params, timeout=15)
    if resp.status_code == 400:
        raise ParseError(f"API отклонил запрос: {resp.json()}")
    resp.raise_for_status()
    return resp.json()


def collect_vacancies(args, log=print, should_stop=None):
    session = make_session()
    params = {
        "text": args.text,
        "per_page": min(args.per_page, 100),
        "page": 0,
    }
    if args.area:
        params["area"] = resolve_area(session, args.area, log)
    if args.salary:
        params["salary"] = args.salary
        params["currency"] = args.currency
    if args.experience:
        params["experience"] = args.experience
    if args.only_with_salary:
        params["only_with_salary"] = "true"

    vacancies = []
    found = None
    while True:
        data = fetch_page(session, params)
        found = data.get("found", 0)
        items = data.get("items") or []
        for item in items:
            vacancies.append(parse_vacancy(item))
        log(f"Страница {params['page'] + 1}: получено {len(items)} вакансий "
            f"(всего {len(vacancies)} из ~{found})")

        max_pages = min(MAX_PAGES, data.get("pages", 0))
        if len(vacancies) >= args.limit or params["page"] + 1 >= max_pages \
                or params["page"] + 1 >= args.pages:
            break
        if should_stop and should_stop():
            log("Остановлено пользователем")
            break
        params["page"] += 1
        time.sleep(args.delay)

    return vacancies, found


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
    parser.add_argument("--pages", type=int, default=5, help="Сколько страниц собрать (макс. 200)")
    parser.add_argument("--per-page", type=int, default=50, help="Вакансий на страницу (до 100)")
    parser.add_argument("--limit", type=int, default=10**9, help="Остановиться после N вакансий")
    parser.add_argument("--delay", type=float, default=0.3, help="Пауза между запросами, сек")
    parser.add_argument("--only-with-salary", action="store_true", help="Только вакансии с зарплатой")
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
