import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from types import SimpleNamespace

import hh_parser as hp

SETTINGS_FILE = Path(__file__).with_name("settings.json")

EXPERIENCE_LABELS = {
    "Любой": "",
    "Нет опыта": "noExperience",
    "1–3 года": "between1And3",
    "3–6 лет": "between3And6",
    "Более 6 лет": "moreThan6",
}

AREA_SUGGESTIONS = ["Рязань", "Москва", "Санкт-Петербург"]


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Парсер вакансий HeadHunter")
        self.root.minsize(640, 520)
        self.worker = None
        self.stop_event = threading.Event()
        self._build_form()
        self.load_settings()
        self._build_log()
        self._build_buttons()

    def _build_form(self):
        form = ttk.Frame(self.root, padding=10)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Поисковый запрос:").grid(row=0, column=0, sticky="w", pady=2)
        self.text_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.text_var).grid(row=0, column=1, sticky="ew", pady=2)

        ttk.Label(form, text="Регион:").grid(row=1, column=0, sticky="w", pady=2)
        self.area_var = tk.StringVar()
        ttk.Combobox(form, textvariable=self.area_var,
                     values=AREA_SUGGESTIONS).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(form, text="(можно ввести любой город)").grid(row=1, column=2, sticky="w")

        ttk.Label(form, text="Зарплата от:").grid(row=2, column=0, sticky="w", pady=2)
        salary_frame = ttk.Frame(form)
        salary_frame.grid(row=2, column=1, sticky="w", pady=2)
        self.salary_var = tk.StringVar()
        ttk.Entry(salary_frame, textvariable=self.salary_var, width=12).pack(side="left")
        ttk.Label(salary_frame, text="Валюта:").pack(side="left", padx=(15, 4))
        self.currency_var = tk.StringVar(value="RUR")
        ttk.Entry(salary_frame, textvariable=self.currency_var, width=8).pack(side="left")

        ttk.Label(form, text="Опыт работы:").grid(row=3, column=0, sticky="w", pady=2)
        self.experience_var = tk.StringVar(value="Любой")
        ttk.Combobox(form, textvariable=self.experience_var, state="readonly",
                     values=list(EXPERIENCE_LABELS), width=20).grid(row=3, column=1, sticky="w", pady=2)

        ttk.Label(form, text="Страниц:").grid(row=4, column=0, sticky="w", pady=2)
        limits = ttk.Frame(form)
        limits.grid(row=4, column=1, sticky="w", pady=2)
        self.pages_var = tk.StringVar(value="5")
        ttk.Entry(limits, textvariable=self.pages_var, width=6).pack(side="left")
        ttk.Label(limits, text="На странице:").pack(side="left", padx=(15, 4))
        self.per_page_var = tk.StringVar(value="50")
        ttk.Entry(limits, textvariable=self.per_page_var, width=6).pack(side="left")
        ttk.Label(limits, text="Пауза, сек:").pack(side="left", padx=(15, 4))
        self.delay_var = tk.StringVar(value="0.3")
        ttk.Entry(limits, textvariable=self.delay_var, width=6).pack(side="left")
        ttk.Label(limits, text="Опубликованы за, дней:").pack(side="left", padx=(15, 4))
        self.days_var = tk.StringVar()
        ttk.Entry(limits, textvariable=self.days_var, width=6).pack(side="left")

        self.only_salary_var = tk.BooleanVar()
        ttk.Checkbutton(form, text="Только вакансии с указанной зарплатой",
                        variable=self.only_salary_var).grid(row=5, column=1, sticky="w", pady=2)

        ttk.Label(form, text="Имя файлов:").grid(row=6, column=0, sticky="w", pady=2)
        self.out_var = tk.StringVar(value="hh_vacancies")
        ttk.Entry(form, textvariable=self.out_var).grid(row=6, column=1, sticky="ew", pady=2)

    def _setting_vars(self):
        return {
            "text": self.text_var,
            "area": self.area_var,
            "salary": self.salary_var,
            "currency": self.currency_var,
            "experience": self.experience_var,
            "pages": self.pages_var,
            "per_page": self.per_page_var,
            "delay": self.delay_var,
            "days": self.days_var,
            "only_with_salary": self.only_salary_var,
            "out": self.out_var,
        }

    def load_settings(self):
        if not SETTINGS_FILE.exists():
            return
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for key, var in self._setting_vars().items():
            value = data.get(key)
            if isinstance(value, str) and not (key == "experience" and value not in EXPERIENCE_LABELS):
                var.set(value)
            elif isinstance(value, bool):
                var.set(value)

    def save_settings(self):
        data = {key: var.get() for key, var in self._setting_vars().items()}
        try:
            SETTINGS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось сохранить настройки: {exc}")
            return
        self.log(f"Настройки сохранены: {SETTINGS_FILE}")

    def _build_log(self):
        log_frame = ttk.LabelFrame(self.root, text="Журнал", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_widget = tk.Text(log_frame, height=14, wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_widget.yview)
        self.log_widget.configure(yscrollcommand=scrollbar.set)
        self.log_widget.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.log_menu = tk.Menu(self.root, tearoff=0)
        self.log_menu.add_command(label="Копировать выделенное",
                                  command=lambda: self.log_widget.event_generate("<<Copy>>"))
        self.log_menu.add_command(label="Копировать всё", command=self.copy_all_log)
        self.log_menu.add_command(label="Выделить всё", command=self.select_all_log)
        self.log_menu.add_separator()
        self.log_menu.add_command(label="Очистить журнал", command=self.clear_log)
        self.log_widget.bind("<Button-3>", self.show_log_menu)
        self.log_widget.bind("<Key>", self.block_log_editing)
        self.log_widget.bind("<Control-a>", self.select_all_log)

    def show_log_menu(self, event):
        self.log_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def block_log_editing(self, event):
        if event.state & 0x0004 and event.keysym.lower() in ("c", "a"):
            return None
        return "break"

    def select_all_log(self, event=None):
        self.log_widget.tag_add("sel", "1.0", "end")
        return "break"

    def copy_all_log(self):
        self.select_all_log()
        self.log_widget.event_generate("<<Copy>>")

    def clear_log(self):
        self.log_widget.delete("1.0", "end")

    def _build_buttons(self):
        bar = ttk.Frame(self.root, padding=10)
        bar.pack(fill="x")
        self.start_button = ttk.Button(bar, text="Начать сбор", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="Остановить", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(bar, text="Сохранить настройки",
                   command=self.save_settings).pack(side="right")

    def log(self, message):
        def append():
            self.log_widget.insert("end", message + "\n")
            self.log_widget.see("end")
        self.root.after(0, append)

    def build_args(self):
        try:
            pages = int(self.pages_var.get())
            per_page = int(self.per_page_var.get())
            delay = float(self.delay_var.get())
            salary = int(self.salary_var.get()) if self.salary_var.get().strip() else None
            days = int(self.days_var.get()) if self.days_var.get().strip() else None
        except ValueError as exc:
            raise ValueError(f"Некорректное число в параметрах: {exc}")
        if days is not None and days < 1:
            raise ValueError("Количество дней должно быть не меньше 1")
        return SimpleNamespace(
            text=self.text_var.get().strip(),
            area=self.area_var.get().strip(),
            salary=salary,
            currency=self.currency_var.get().strip() or "RUR",
            experience=EXPERIENCE_LABELS[self.experience_var.get()],
            pages=max(pages, 1),
            per_page=per_page,
            limit=10**9,
            delay=delay,
            days=days,
            only_with_salary=self.only_salary_var.get(),
            out=self.out_var.get().strip() or "hh_vacancies",
        )

    def start(self):
        try:
            args = self.build_args()
        except ValueError as exc:
            messagebox.showerror("Ошибка ввода", str(exc))
            return
        if not any([args.text, args.area, args.salary]):
            messagebox.showwarning("Пустой запрос",
                                   "Укажите хотя бы один параметр поиска:\nтекст, регион или зарплату.")
            return
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker = threading.Thread(target=self.run_parser, args=(args,), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()

    def run_parser(self, args):
        try:
            vacancies, found = hp.collect_vacancies(args, log=self.log,
                                                    should_stop=self.stop_event.is_set)
            if not vacancies:
                self.log("Ничего не найдено по заданным условиям")
                return
            csv_path, json_path = hp.save_outputs(vacancies, found, args, log=self.log)
            if not self.stop_event.is_set():
                self.root.after(0, lambda: messagebox.showinfo(
                    "Готово",
                    f"Собрано вакансий: {len(vacancies)}\n\nCSV: {csv_path}\nJSON: {json_path}"))
        except hp.ParseError as exc:
            self.log(f"Ошибка: {exc}")
            self.root.after(0, lambda: messagebox.showerror("Ошибка", str(exc)))
        except Exception as exc:
            self.log(f"Непредвиденная ошибка: {exc!r}")
        finally:
            self.root.after(0, self._finish)

    def _finish(self):
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
