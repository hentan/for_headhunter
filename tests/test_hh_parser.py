import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import hh_parser as hp


class DateWindowsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 22, 12, 0, 0)

    def test_single_window(self):
        windows = hp.build_date_windows(5, now=self.now)
        self.assertEqual(len(windows), 1)
        start = datetime(2026, 8, 17, 12, 0, 0).strftime("%Y-%m-%dT%H:%M:%S")
        end = self.now.strftime("%Y-%m-%dT%H:%M:%S")
        self.assertEqual(windows[0], (start, end))

    def test_long_range_splits_contiguously(self):
        windows = hp.build_date_windows(75, now=self.now)
        self.assertEqual(len(windows), 3)
        parsed = [(datetime.strptime(f, "%Y-%m-%dT%H:%M:%S"),
                   datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")) for f, t in windows]
        for (_, prev_end), (next_start, _) in zip(parsed, parsed[1:]):
            self.assertEqual(prev_end, next_start)
        self.assertEqual(parsed[-1][1], self.now)
        expected_start = self.now - timedelta(days=75)
        self.assertEqual(parsed[0][0], expected_start)
        for start, end in parsed[:-1]:
            self.assertLessEqual((end - start).days, hp.WINDOW_DAYS)


class StripHtmlTests(unittest.TestCase):
    def test_removes_tags_and_collapse_spaces(self):
        self.assertEqual(
            hp.strip_html("<p>Опыт  разработки<br/>от 3 лет</p>"),
            "Опыт разработки от 3 лет",
        )

    def test_unescapes_entities(self):
        self.assertEqual(hp.strip_html("Python&nbsp;&amp; SQL"), "Python & SQL")

    def test_empty_values(self):
        self.assertEqual(hp.strip_html(None), "")
        self.assertEqual(hp.strip_html(""), "")


class ParseVacancyTests(unittest.TestCase):
    def test_full_item(self):
        item = {
            "id": "123",
            "name": "Python-разработчик",
            "employer": {"name": "ООО Ромашка"},
            "area": {"name": "Москва"},
            "experience": {"name": "От 1 до 3 лет"},
            "employment": {"name": "Полная занятость"},
            "schedule": {"name": "Удаленная работа"},
            "salary": {"from": 100000, "to": 200000, "currency": "RUR", "gross": True},
            "published_at": "2026-08-01T12:00:00+0300",
            "alternate_url": "https://hh.ru/vacancy/123",
            "snippet": {"requirement": "<b>Опыт</b> от 3 лет", "responsibility": None},
        }
        result = hp.parse_vacancy(item)
        self.assertEqual(result["id"], "123")
        self.assertEqual(result["employer"], "ООО Ромашка")
        self.assertEqual(result["salary_from"], 100000)
        self.assertEqual(result["salary_to"], 200000)
        self.assertEqual(result["requirement"], "Опыт от 3 лет")
        self.assertEqual(result["responsibility"], "")

    def test_missing_fields(self):
        item = {"id": "1", "name": "Вакансия"}
        result = hp.parse_vacancy(item)
        self.assertEqual(result["employer"], "")
        self.assertEqual(result["salary_from"], "")
        self.assertEqual(result["salary_to"], "")
        self.assertEqual(result["currency"], "")
        self.assertEqual(result["experience"], "")

    def test_zero_salary_is_kept(self):
        item = {"id": "2", "name": "x", "salary": {"from": 0, "to": None, "currency": "RUR"}}
        result = hp.parse_vacancy(item)
        self.assertEqual(result["salary_from"], 0)
        self.assertEqual(result["salary_to"], "")


class SaveOutputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = SimpleNamespace(
            out=str(Path(self.tmp.name) / "hh"),
            text="python", area="Москва", salary=150000, currency="RUR",
            experience="between1And3", only_with_salary=False,
        )
        self.vacancies = [hp.parse_vacancy({"id": "42", "name": "Тест"})]

    def read_csv(self, path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_files_created(self):
        csv_path, json_path = hp.save_outputs(self.vacancies, 999, self.args, log=lambda m: None)
        self.assertTrue(csv_path.exists())
        self.assertTrue(json_path.exists())

    def test_json_structure(self):
        _, json_path = hp.save_outputs(self.vacancies, 999, self.args, log=lambda m: None)
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        self.assertEqual(data["source"], "api.hh.ru")
        self.assertEqual(data["found_total"], 999)
        self.assertEqual(data["collected"], 1)
        self.assertEqual(data["vacancies"][0]["id"], "42")
        self.assertEqual(data["query"]["text"], "python")

    def test_csv_rows(self):
        csv_path, _ = hp.save_outputs(self.vacancies, 999, self.args, log=lambda m: None)
        rows = self.read_csv(csv_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "42")
        self.assertEqual(rows[0]["name"], "Тест")

    def test_empty_vacancies_list(self):
        csv_path, json_path = hp.save_outputs([], 0, self.args, log=lambda m: None)
        rows = self.read_csv(csv_path)
        self.assertEqual(rows, [])
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        self.assertEqual(data["collected"], 0)


class SecretProtectionTests(unittest.TestCase):
    def test_roundtrip(self):
        secret = "p@ssw0rd-секрет"
        encoded = hp.protect_secret(secret)
        self.assertNotIn(secret, encoded)
        self.assertEqual(hp.unprotect_secret(encoded), secret)

    def test_unprotect_garbage(self):
        self.assertEqual(hp.unprotect_secret("not-a-valid-blob!"), "")
        self.assertEqual(hp.unprotect_secret(""), "")


class CliArgsTests(unittest.TestCase):
    def test_defaults(self):
        args = hp.build_arg_parser().parse_args([])
        self.assertEqual(args.text, "")
        self.assertIsNone(args.area)
        self.assertIsNone(args.salary)
        self.assertEqual(args.currency, "RUR")
        self.assertIsNone(args.experience)
        self.assertIsNone(getattr(args, "days", "missing"))
        self.assertEqual(args.pages, 5)
        self.assertEqual(args.per_page, 50)

    def test_experience_choices(self):
        for exp in hp.EXPERIENCE_CHOICES:
            args = hp.build_arg_parser().parse_args(["dev", "--experience", exp])
            self.assertEqual(args.experience, exp)

    def test_client_credentials_flags(self):
        args = hp.build_arg_parser().parse_args(
            ["dev", "--client-id", "my-id", "--client-secret", "my-secret"])
        self.assertEqual(args.client_id, "my-id")
        self.assertEqual(args.client_secret, "my-secret")

    def test_session_token_header(self):
        session = hp.make_session("test-token-123")
        self.assertEqual(session.headers["Authorization"], "Bearer test-token-123")
        plain = hp.make_session()
        self.assertNotIn("Authorization", plain.headers)


if __name__ == "__main__":
    unittest.main()
