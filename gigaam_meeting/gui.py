"""Russian desktop UI. Only explicit user actions start audio capture."""
from __future__ import annotations

from pathlib import Path
from bisect import bisect_right
import gc
from queue import Empty, Queue
from threading import Thread
from time import monotonic
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Any

from .contracts import AudioDevice, Event, Segment, SessionConfig, SOURCE_LABELS
from .audio import AudioCaptureError
from .exports import EXPORT_FORMATS, default_export_name, export_transcript, format_time
from .storage import validate_local_path

ACTIVE_STATES = {"loading", "starting", "preview", "recording", "paused", "stopping", "draining"}
STATE_LABELS = {
    "idle": "Готово. Захват выключен.",
    "loading": "Загрузка локальной модели. Захват ещё не начался.",
    "starting": "ИНИЦИАЛИЗИРУЕМ ИСТОЧНИКИ — захват может начаться; говорите после «ЗАХВАТ ВКЛЮЧЁН»",
    "preview": "ПРОВЕРКА УРОВНЕЙ: захват звука на 3 секунды",
    "recording": "ЗАХВАТ ВКЛЮЧЁН — микрофон / звук выбранного устройства",
    "paused": "ПАУЗА — захват выключен; ранее полученный звук обрабатывается",
    "stopping": "ОСТАНАВЛИВАЕМ ЗАХВАТ — дождитесь подтверждения остановки",
    "draining": "Захват выключен. Завершаем оставшиеся фрагменты…",
    "error": "Ошибка. Захват остановлен; прочитайте сообщение ниже.",
}


class MeetingApp:
    def __init__(self, root: tk.Tk, manager: Any, event_queue: Queue):
        self.root = root
        self.manager = manager
        self.events = event_queue
        self.state = "idle"
        self.segments: list[Segment] = []
        self.segment_marks: list[str] = []
        self.devices_by_source: dict[str, list[AudioDevice]] = {"microphone": [], "system": []}
        self.dirty = False
        self.revision = 0
        self.exporting = False
        self.enumerating = False
        self.closing = False
        self.previewing = False
        self.capture_sources = ""
        self.level_seen = {}
        self.session_error: str | None = None
        self.session_warning: str | None = None
        root.title("GigaAM — локальная транскрипция звонка")
        root.geometry(f"1040x{min(960, root.winfo_screenheight() - 85)}")
        root.minsize(850, min(650, root.winfo_screenheight() - 85))
        root.protocol("WM_DELETE_WINDOW", self._close_requested)
        self.consent = tk.BooleanVar(value=False)
        self.save_audio = tk.BooleanVar(value=False)
        self.folder = tk.StringVar(value=str(Path.home() / "GigaAM" / "transcripts"))
        self.format = tk.StringVar(value="TXT")
        self.status = tk.StringVar(value=STATE_LABELS["idle"])
        self.lag = tk.StringVar(value="Очередь пуста")
        self.notice = tk.StringVar(value="Выберите источники и явно разрешите захват.")
        self.saved_status = tk.StringVar(value="Транскрипция ещё не получена")
        self.alert_text = tk.StringVar(value="")
        self._build()
        self._refresh_devices()
        self._set_state("idle")
        root.after(75, self._poll)

    def _build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        permanent = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        permanent.grid(row=0, column=0, sticky="ew")
        permanent.columnconfigure(0, weight=1)
        self.indicator = tk.Label(
            permanent, textvariable=self.status, font=("Segoe UI", 11, "bold"),
            anchor="w", padx=10, pady=10, bg="#e7f0e9", fg="#21422a", wraplength=850,
        )
        self.indicator.grid(row=0, column=0, sticky="ew")
        self.quick_stop_button = ttk.Button(permanent, text="Остановить", command=self._stop)
        self.quick_stop_button.grid(row=0, column=1, padx=(8, 0))
        self.alert_label = ttk.Label(permanent, textvariable=self.alert_text, foreground="#9a5300", wraplength=950)
        self.alert_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.alert_label.grid_remove()
        # Small laptop displays remain usable; the capture banner and stop button
        # stay visible while the rest of the interface can scroll vertically.
        viewport = ttk.Frame(self.root)
        viewport.grid(row=1, column=0, sticky="nsew")
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        canvas = tk.Canvas(viewport, borderwidth=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        page_scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=canvas.yview)
        page_scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=page_scrollbar.set)
        outer = ttk.Frame(canvas, padding=16)
        outer_id = canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        def fit_content(event):
            canvas.itemconfigure(outer_id, width=event.width)
            available = max(500, event.width - 55)
            widgets = list(outer.winfo_children())
            while widgets:
                widget = widgets.pop()
                widgets.extend(widget.winfo_children())
                if isinstance(widget, ttk.Label) and int(widget.cget("wraplength")) > 0:
                    widget.configure(wraplength=available)
            self.indicator.configure(wraplength=max(400, event.width - 145))
            self.alert_label.configure(wraplength=available)

        canvas.bind("<Configure>", fit_content)

        def scroll_page(event):
            if isinstance(event.widget, (tk.Text, ttk.Combobox)):
                return
            canvas.yview_scroll(int(-event.delta / 120), "units")

        self.root.bind("<MouseWheel>", scroll_page)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(6, weight=1)
        ttk.Label(outer, text="GigaAM — локальная транскрипция", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer, text="Казахский, русский и английский, включая смешанную речь. "
            "Вы уже участвуете в звонке; приложение слушает только выбранные устройства.",
            wraplength=970,
        ).grid(row=1, column=0, sticky="w", pady=(4, 12))

        sources = ttk.LabelFrame(outer, text="Источники звука", padding=10)
        sources.grid(row=2, column=0, sticky="ew")
        sources.columnconfigure(1, weight=1)
        self.combos: dict[str, ttk.Combobox] = {}
        self.levels: dict[str, ttk.Progressbar] = {}
        for row, source in enumerate(("microphone", "system")):
            ttk.Label(sources, text=SOURCE_LABELS[source]).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            combo = ttk.Combobox(sources, state="readonly", width=60)
            combo.grid(row=row, column=1, sticky="ew", pady=4)
            self.combos[source] = combo
            meter = ttk.Progressbar(sources, length=130, maximum=100, mode="determinate")
            meter.grid(row=row, column=2, sticky="e", padx=(12, 0))
            self.levels[source] = meter
        self.refresh_button = ttk.Button(sources, text="Обновить устройства", command=self._refresh_devices)
        self.refresh_button.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.preview_button = ttk.Button(sources, text="Проверить уровни · 3 сек", command=self._preview)
        self.preview_button.grid(row=2, column=1, sticky="w", pady=(8, 0))
        ttk.Label(
            sources,
            text="Звук компьютера: захватываются ВСЕ приложения на выбранном устройстве вывода, "
                 "включая уведомления. Выбор отдельного приложения пока недоступен. "
                 "Наушники поддерживаются без виртуального аудиокабеля.",
            foreground="#754600", wraplength=950,
        ).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        permission = ttk.Frame(outer)
        permission.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        permission.columnconfigure(0, weight=1)
        self.consent_check = ttk.Checkbutton(
            permission, variable=self.consent,
            text="Разрешаю захват выбранных источников; учёл согласие участников и правила организации.",
        )
        self.consent_check.grid(row=0, column=0, sticky="w")
        ttk.Label(
            permission, text="Разрешение Windows на микрофон не заменяет согласие участников. "
            "Захват не запускается автоматически и не восстанавливается после перезапуска.",
            wraplength=970, foreground="#555555",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        controls = ttk.Frame(outer)
        controls.grid(row=4, column=0, sticky="ew", pady=12)
        self.start_button = ttk.Button(controls, text="Начать", command=self._start)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(controls, text="Пауза", command=self._pause_resume)
        self.pause_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(controls, text="Остановить и завершить текст", command=self._stop)
        self.stop_button.pack(side="left")
        self.lag_label = ttk.Label(controls, textvariable=self.lag)
        self.lag_label.pack(side="right")

        status_box = ttk.Frame(outer)
        status_box.grid(row=5, column=0, sticky="ew")
        self.notice_label = ttk.Label(status_box, textvariable=self.notice, wraplength=970)
        self.notice_label.pack(anchor="w", pady=(6, 4))
        ttk.Label(status_box, text="Фрагменты появляются с задержкой. Таймкоды приблизительные; "
                  "«Микрофон» и «Звук компьютера» — источники, а не имена людей.",
                  wraplength=970, foreground="#555555").pack(anchor="w", pady=(0, 7))

        text_frame = ttk.Frame(outer)
        text_frame.grid(row=6, column=0, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.transcript = tk.Text(
            text_frame, wrap="word", font=("Segoe UI", 11), height=12,
            state="disabled", padx=10, pady=8, undo=False,
        )
        self.transcript.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(text_frame, command=self.transcript.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.tag_configure("microphone", foreground="#1e5586", spacing1=10)
        self.transcript.tag_configure("system", foreground="#6b3d7e", spacing1=10)

        storage = ttk.LabelFrame(outer, text="Локальное сохранение", padding=10)
        storage.grid(row=7, column=0, sticky="ew", pady=(12, 0))
        storage.columnconfigure(1, weight=1)
        ttk.Label(storage, text="Папка").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.folder_entry = ttk.Entry(storage, textvariable=self.folder)
        self.folder_entry.grid(row=0, column=1, sticky="ew")
        self.folder_button = ttk.Button(storage, text="Выбрать…", command=self._choose_folder)
        self.folder_button.grid(row=0, column=2, padx=(8, 0))
        self.audio_check = ttk.Checkbutton(
            storage, text="Дополнительно сохранять раздельное аудио на диск (по умолчанию выключено)",
            variable=self.save_audio,
        )
        self.audio_check.grid(row=1, column=0, columnspan=3, sticky="w", pady=6)
        save_row = ttk.Frame(storage)
        save_row.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.export_format = ttk.Combobox(save_row, textvariable=self.format, values=EXPORT_FORMATS, state="readonly", width=7)
        self.export_format.pack(side="left")
        self.export_button = ttk.Button(save_row, text="Сохранить транскрипцию…", command=self._export)
        self.export_button.pack(side="left", padx=8)
        ttk.Label(save_row, textvariable=self.saved_status).pack(side="left")
        ttk.Label(
            outer, text="Аудио и текст обрабатываются локально. Без сохранения аудио используется ограниченный буфер памяти. "
            "ОС может использовать файл подкачки; резервные копии и синхронизация выбранной папки контролируются вами.",
            wraplength=970, foreground="#555555",
        ).grid(row=8, column=0, sticky="w", pady=(8, 0))

    def _refresh_devices(self):
        if self.state in ACTIVE_STATES or self.enumerating or self.closing:
            return
        previous = {source: self._selected(source) for source in ("microphone", "system")}
        self.enumerating = True
        self.notice.set("Получаем список устройств Windows… Захват выключен.")
        self._set_state(self.state)
        manager, events = self.manager, self.events

        def enumerate_devices():
            try:
                devices = manager.devices()
                events.put(Event("devices_ready", data={"devices": devices, "previous": previous}))
            except AudioCaptureError as exc:
                events.put(Event("devices_error", message=str(exc)))
            except Exception:
                events.put(Event("devices_error", message="Не удалось получить список устройств Windows. Проверьте подключение устройств и полностью распакованную сборку."))

        Thread(target=enumerate_devices, name="wasapi-device-catalog", daemon=True).start()

    def _apply_devices(self, devices: dict[str, list[AudioDevice]], previous: dict[str, AudioDevice | None]):
        self.enumerating = False
        for source in ("microphone", "system"):
            prior = previous.get(source)
            options = list(devices.get(source, []))
            self.devices_by_source[source] = options
            combo = self.combos[source]
            combo["values"] = ["Не захватывать"] + [device.name for device in options]
            index = 0
            if prior is not None:
                matches = [i for i, device in enumerate(options) if device.fingerprint == prior.fingerprint and device.id == prior.id]
                if matches:
                    index = matches[0] + 1
            combo.current(index)
        self.notice.set("Устройства обновлены. Выберите хотя бы один источник; автоматического переключения нет.")
        self._set_state(self.state)

    def _selected(self, source: str) -> AudioDevice | None:
        index = self.combos[source].current() - 1
        options = self.devices_by_source[source]
        return options[index] if 0 <= index < len(options) else None

    def _config(self) -> SessionConfig | None:
        microphone, system = self._selected("microphone"), self._selected("system")
        if microphone is None and system is None:
            messagebox.showwarning("Источники не выбраны", "Выберите микрофон, устройство системного звука или оба источника.", parent=self.root)
            return None
        if not self.consent.get():
            messagebox.showwarning("Нужно явное разрешение", "Перед каждым сеансом отметьте разрешение на захват выбранных источников.", parent=self.root)
            return None
        folder = Path(self.folder.get().strip()).expanduser()
        if not self.folder.get().strip() or not folder.is_absolute():
            messagebox.showwarning("Выберите папку", "Укажите полный путь к локальной папке для сохранения.", parent=self.root)
            return None
        try:
            folder = validate_local_path(folder)
        except ValueError as exc:
            messagebox.showwarning("Нужна локальная папка", str(exc), parent=self.root)
            return None
        return SessionConfig(microphone, system, True, folder, self.save_audio.get())

    def _preview(self):
        config = self._config()
        if config is None:
            return
        self.previewing = True
        self.session_error = None
        self.session_warning = None
        self._clear_alert()
        self.notice_label.configure(foreground="#333333")
        self.notice.set("Проверяем выбранные источники 3 секунды; транскрипция и запись на диск не создаются.")
        self._set_state("preview")
        try:
            self.manager.preview(SessionConfig(config.microphone, config.system, True, config.output_folder, False), seconds=3)
        except Exception as exc:
            self._show_error(str(exc))

    def _start(self):
        config = self._config()
        if config is None:
            return
        if self.dirty and self.segments:
            if not messagebox.askyesno(
                "Есть несохранённый текст", "Начать новый сеанс? Текущий текст будет заменён. "
                "Вы можете отменить и сначала сохранить транскрипцию.", parent=self.root,
            ):
                return
        self.segments.clear()
        self.session_error = None
        self.session_warning = None
        self._clear_alert()
        self.notice_label.configure(foreground="#333333")
        self.revision += 1
        self.dirty = False
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        if self.segment_marks:
            self.transcript.mark_unset(*self.segment_marks)
            self.segment_marks.clear()
        self.transcript.configure(state="disabled")
        self.saved_status.set("Ожидаем распознавание")
        self.capture_sources = " + ".join(SOURCE_LABELS[source] for source, device in (("microphone", config.microphone), ("system", config.system)) if device)
        self.notice.set("Модель загружается с локального диска. Первый запуск может занять время.")
        self._set_state("loading")
        try:
            self.manager.start(config)
        except Exception as exc:
            self._show_error(str(exc))

    def _pause_resume(self):
        try:
            if self.state == "recording":
                self.manager.pause()
            elif self.state == "paused":
                self.manager.resume()
        except Exception as exc:
            self._show_error(str(exc))

    def _stop(self):
        self.notice.set("Захват останавливается; оставшийся текст появится после завершения обработки.")
        self._set_state("stopping")
        try:
            self.manager.stop()
        except Exception as exc:
            self._show_error(str(exc))

    def _choose_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title="Локальная папка для транскрипций и необязательного аудио", mustexist=True)
        if folder:
            self.folder.set(folder)

    def _set_state(self, state: str):
        self.state = state
        label = STATE_LABELS.get(state, state)
        if state == "recording":
            label = f"ЗАХВАТ ВКЛЮЧЁН — {self.capture_sources}"
        self.status.set(label)
        if state in {"starting", "recording", "preview", "stopping"}:
            self.indicator.configure(bg="#b5212c", fg="white")
        elif state == "error":
            self.indicator.configure(bg="#fde8e8", fg="#8a1423")
        elif state in {"paused", "draining", "loading"}:
            self.indicator.configure(bg="#fff0cc", fg="#634900")
        else:
            self.indicator.configure(bg="#e7f0e9", fg="#21422a")
        active = state in ACTIVE_STATES
        editing = not active and not self.exporting and not self.closing and not self.enumerating
        for combo in self.combos.values():
            combo.configure(state="readonly" if editing else "disabled")
        for widget in (self.refresh_button, self.preview_button, self.consent_check, self.folder_entry, self.folder_button, self.audio_check, self.start_button):
            widget.configure(state="normal" if editing else "disabled")
        self.pause_button.configure(text="Продолжить" if state == "paused" else "Пауза", state="normal" if state in {"recording", "paused"} and not self.closing else "disabled")
        self.stop_button.configure(state="normal" if state in {"loading", "starting", "preview", "recording", "paused"} and not self.closing else "disabled")
        self.quick_stop_button.configure(state="normal" if state in {"loading", "starting", "preview", "recording", "paused"} and not self.closing else "disabled")
        can_export = bool(self.segments) and not active and not self.exporting and not self.closing
        self.export_button.configure(state="normal" if can_export else "disabled")
        self.export_format.configure(state="readonly" if can_export else "disabled")
        if state in {"idle", "paused", "error", "draining"}:
            for meter in self.levels.values():
                meter["value"] = 0

    def _append(self, segment: Segment):
        position = bisect_right(
            [(item.start, item.end, item.source) for item in self.segments],
            (segment.start, segment.end, segment.source),
        )
        self.dirty = True
        self.revision += 1
        self.saved_status.set("Есть несохранённый текст")
        follow = self.transcript.yview()[1] >= 0.98
        self.transcript.configure(state="normal")
        insertion = self.transcript.index(
            self.segment_marks[position] if position < len(self.segment_marks) else "end-1c"
        )
        label = f"[{format_time(segment.start)} — {format_time(segment.end)}] {SOURCE_LABELS.get(segment.source, segment.source)}\n"
        self.transcript.insert(insertion, label, segment.source, segment.text + "\n", ())
        mark = f"segment_{self.revision}"
        self.transcript.mark_set(mark, insertion)
        self.transcript.mark_gravity(mark, "right")
        self.segments.insert(position, segment)
        self.segment_marks.insert(position, mark)
        self.transcript.configure(state="disabled")
        if follow:
            self.transcript.see("end")
        self._set_state(self.state)

    def _export(self):
        kind = self.format.get()
        target = filedialog.asksaveasfilename(
            parent=self.root, title="Сохранить локально — существующие файлы не перезаписываются",
            initialdir=self.folder.get(), initialfile=default_export_name(kind),
            defaultextension="." + kind.lower(), filetypes=[(kind, "*." + kind.lower())],
            confirmoverwrite=False,
        )
        if not target:
            return
        records = list(self.segments)
        revision = self.revision
        self.exporting = True
        self.saved_status.set("Сохраняем…")
        self._set_state(self.state)
        events = self.events

        def write():
            try:
                result = export_transcript(records, target, kind)
                events.put(Event("export_finished", data={"path": str(result), "revision": revision}))
            except Exception as exc:
                events.put(Event("export_error", message=str(exc)))

        Thread(target=write, name="transcript-export", daemon=True).start()

    def _show_error(self, message: str):
        self.session_error = message or "Произошла ошибка. Остановите сеанс и проверьте выбранные устройства."
        self.notice.set(self.session_error)
        self.notice_label.configure(foreground="#8a1423")
        self._set_alert(self.session_error, error=True)
        self.consent.set(False)
        self._set_state("error")

    def _set_alert(self, message: str, *, error: bool = False):
        self.alert_text.set(message)
        self.alert_label.configure(foreground="#8a1423" if error else "#9a5300")
        self.alert_label.grid()

    def _clear_alert(self):
        self.alert_text.set("")
        self.alert_label.grid_remove()

    def _poll(self):
        # Bound each event-loop iteration so large event bursts never freeze controls.
        for _ in range(160):
            try:
                event = self.events.get_nowait()
            except Empty:
                break
            if event.kind == "state":
                state = event.data.get("state", "idle")
                self._set_state(state)
                if state == "recording":
                    self.notice.set("Распознаём отдельные фрагменты. Используйте наушники: акустическое эхоподавление не выполняется.")
            elif event.kind == "level":
                source = event.data.get("source")
                if source in self.levels and self.state in {"recording", "preview"}:
                    self.level_seen[source] = monotonic()
                    self.levels[source]["value"] = max(0, min(100, float(event.data.get("level", 0)) * 100))
            elif event.kind == "segment":
                self._append(event.data["segment"])
            elif event.kind == "lag":
                pending = max(0, float(event.data.get("pending_seconds", 0)))
                self.lag.set(f"В очереди {pending:.1f} сек" if pending else "Очередь пуста")
                self.lag_label.configure(foreground="#ad202b" if pending >= 12 else "#555555")
            elif event.kind == "error":
                self._show_error(event.message)
            elif event.kind == "warning":
                self.session_warning = event.message or "Обнаружен пропуск звука. Часть речи могла не попасть в текст."
                if not self.session_error:
                    self._set_alert(self.session_warning)
            elif event.kind == "devices_changed":
                self.session_error = event.message or "Устройство изменилось. Сеанс остановлен; заново выберите устройства и разрешите захват."
                self.notice.set(self.session_error)
                self._set_alert(self.session_error, error=True)
                self.consent.set(False)
            elif event.kind == "devices_ready":
                self._apply_devices(event.data["devices"], event.data["previous"])
            elif event.kind == "devices_error":
                self.enumerating = False
                if self.state in ACTIVE_STATES:
                    self._set_alert(event.message, error=True)
                else:
                    self._show_error(event.message)
            elif event.kind == "finished":
                self.consent.set(False)
                self.save_audio.set(False)
                self._set_state("error" if self.session_error else "idle")
                self.lag.set("Очередь пуста")
                self.notice.set(self.session_error or ("Проверка уровней завершена. Для начала сеанса снова отметьте разрешение." if self.previewing else "Сеанс завершён. Сохраните текст вручную; проверьте ошибки распознавания."))
                self.previewing = False
                if self.closing:
                    self._destroy()
                    return
            elif event.kind == "export_finished":
                self.exporting = False
                if event.data.get("revision") == self.revision:
                    self.dirty = False
                self.saved_status.set("Текст сохранён")
                self.notice.set("Сохранено локально: " + event.data["path"])
                self._set_state(self.state)
                if self.closing:
                    self._destroy()
                    return
            elif event.kind == "export_error":
                self.exporting = False
                self.saved_status.set("Текст не сохранён")
                self.notice.set(event.message)
                self._set_state(self.state)
                if self.closing:
                    self._destroy()
                    return
        if self.state in {"recording", "preview"}:
            now = monotonic()
            for source, meter in self.levels.items():
                if now - self.level_seen.get(source, 0) > 0.5:
                    meter["value"] = 0
        self.root.after(75, self._poll)

    def _close_requested(self):
        if self.closing:
            return
        if self.dirty and self.segments:
            if not messagebox.askyesno("Несохранённая транскрипция", "Закрыть приложение без сохранения текущего текста? Можно отменить и выполнить экспорт.", parent=self.root):
                return
        self.closing = True
        if self.state in ACTIVE_STATES:
            self.notice.set("Закрываем приложение после остановки захвата и завершения оставшихся фрагментов.")
            self._stop()
        elif self.exporting:
            self.notice.set("Дожидаемся завершения экспорта перед закрытием.")
            self._set_state(self.state)
        else:
            self._destroy()

    def _destroy(self):
        self.manager.close()
        self.root.destroy()


def run_app(manager_factory: Callable[[Queue], Any]) -> None:
    events: Queue = Queue()
    manager = manager_factory(events)
    root = tk.Tk()
    app = None
    try:
        # Keep all Tk objects on the GUI thread until the event loop ends.
        app = MeetingApp(root, manager, events)
        app.root.mainloop()
    finally:
        try:
            manager.close()
        finally:
            # Tk interpreters/variables cannot be finalized by an ASR worker's
            # automatic cyclic GC. Workers retain plain data, never this app.
            enabled = gc.isenabled()
            gc.disable()
            try:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
                app = None
                root = None
                gc.collect()
            finally:
                if enabled:
                    gc.enable()
