import json
import math
import queue
import threading
import time
from tkinter import filedialog

import customtkinter as ctk
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key, KeyCode, Listener
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController
from pynput.mouse import Listener as MouseListener

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

mouse = MouseController()
keyboard = KeyboardController()
ESC_ID = ("key", Key.esc)

# --- Настройки, от которых зависит нагрузка на процессор ---
MAX_CPS = 5000      # скорость в режиме «Без ограничения» (кликов в секунду)
MIN_PAUSE = 0.002   # поток кликов просыпается не чаще 500 раз в секунду
MAX_PAUSE = 0.25    # и не спит дольше 0.25 с (быстро реагирует на смену скорости)
POLL_MS = 40        # как часто окно проверяет очередь нажатых клавиш

# Названия знаков и клавиш цифрового блока (по коду клавиши, не по раскладке)
OEM_NAMES = {
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
    0x6A: "NUM *", 0x6B: "NUM +", 0x6D: "NUM -", 0x6E: "NUM .", 0x6F: "NUM /",
}


def key_id(key):
    """Код клавиши, не зависящий от раскладки (русская/английская)."""
    if isinstance(key, KeyCode):
        if key.vk is not None:
            return ("vk", key.vk)
        if key.char:
            return ("char", key.char.lower())
    return ("key", key)


def key_name(key):
    """Название клавиши латиницей независимо от текущей раскладки."""
    if isinstance(key, Key):
        return key.name.upper()
    if isinstance(key, KeyCode):
        vk = key.vk
        if vk is not None:
            if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
                return chr(vk)  # цифры и буквы A-Z
            if 0x60 <= vk <= 0x69:
                return f"NUM {vk - 0x60}"
            if vk in OEM_NAMES:
                return OEM_NAMES[vk]
        if key.char and key.char.isprintable():
            return key.char.upper()
        if vk is not None:
            return f"KEY {vk}"
    return str(key)


def pack_key(key):
    """Клавиша -> простой список, который можно сохранить в JSON."""
    if isinstance(key, Key):
        return ["special", key.name]
    if isinstance(key, KeyCode):
        if key.vk is not None:
            return ["vk", key.vk]
        if key.char:
            return ["char", key.char]
    return None


def unpack_key(packed):
    """Список из pack_key -> клавиша для воспроизведения (или None)."""
    try:
        kind, value = packed
        if kind == "special":
            return Key[value]
        if kind == "vk":
            return KeyCode.from_vk(int(value))
        if kind == "char":
            return KeyCode.from_char(str(value))
    except (KeyError, TypeError, ValueError):
        pass
    return None


def validate_events(raw):
    """Проверяет события макроса из файла и отбрасывает некорректные."""
    events = []
    for ev in raw:
        try:
            kind = ev[0]
            t = float(ev[1])
            if kind == "click" and len(ev) == 6:
                events.append(["click", t, int(ev[2]), int(ev[3]),
                               str(ev[4]), bool(ev[5])])
            elif kind == "scroll" and len(ev) == 6:
                events.append(["scroll", t, int(ev[2]), int(ev[3]),
                               int(ev[4]), int(ev[5])])
            elif kind == "key" and len(ev) == 4 and unpack_key(ev[2]):
                events.append(["key", t, list(ev[2]), bool(ev[3])])
        except (TypeError, ValueError, IndexError):
            continue
    events.sort(key=lambda e: e[1])
    return events


class AutoClicker(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Автокликер и макросы")
        self.geometry("380x720")
        self.resizable(False, False)

        # --- состояние ---
        self.running = True
        self.click_event = threading.Event()  # включено, пока автокликер работает
        self.clicking = False
        self.mouse_listener = None  # запускается только на время записи
        self.last_name = None
        self.recording = False
        self.playing = False
        self.binding = None  # какое действие сейчас переназначаем
        self.unlimited = False
        self.cps = 10.0
        self.button = Button.left
        self.hotkeys = {
            "click": (("key", Key.f6), "F6"),
            "record": (("key", Key.f9), "F9"),
            "play": (("key", Key.f10), "F10"),
        }
        self.held = set()
        self.events = queue.Queue()

        # --- макрос ---
        self.macro = []
        self.rec_buf = []
        self.rec_start = 0.0
        self.rec_ignore_until = 0.0
        self.play_thread = None
        self.down_keys = {}
        self.down_buttons = set()
        self.note = ""

        # --- окно ---
        ctk.CTkLabel(
            self, text="Автокликер и макросы",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).pack(pady=(18, 2))

        self.status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=15))
        self.status.pack(pady=(0, 8))

        self.tabs = ctk.CTkTabview(self, width=340, height=490)
        self.tabs.pack(padx=20)
        self.build_click_tab(self.tabs.add("Автокликер"))
        self.build_macro_tab(self.tabs.add("Макрос"))

        self.last_key = ctk.CTkLabel(
            self, text="Последняя нажатая клавиша: —", text_color="gray"
        )
        self.last_key.pack(pady=(8, 0))

        self.default_color = self.toggle_btn.cget("fg_color")
        self.default_hover = self.toggle_btn.cget("hover_color")
        self.refresh_labels()

        # --- потоки и слушатели ---
        threading.Thread(target=self.click_loop, daemon=True).start()
        self.listener = Listener(
            on_press=self.on_press, on_release=self.on_release
        )
        self.listener.start()
        self.after(POLL_MS, self.poll_events)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    @property
    def clicking(self):
        return self._clicking

    @clicking.setter
    def clicking(self, value):
        self._clicking = bool(value)
        if self._clicking:
            self.click_event.set()    # будим поток кликов
        else:
            self.click_event.clear()  # поток засыпает и не грузит процессор

    # ------------------------------------------------------------------
    # Построение вкладок
    # ------------------------------------------------------------------
    def build_click_tab(self, tab):
        ctk.CTkLabel(tab, text="Скорость (кликов в секунду)").pack(pady=(10, 0))
        self.cps_entry = ctk.CTkEntry(tab, width=140, justify="center")
        self.cps_entry.insert(0, "10")
        self.cps_entry.pack(pady=(4, 8))
        self.cps_entry.bind("<Return>", self.apply_cps)
        self.cps_entry.bind("<FocusOut>", self.apply_cps)

        self.unlimited_switch = ctk.CTkSwitch(
            tab, text="Без ограничения скорости", command=self.on_unlimited
        )
        self.unlimited_switch.pack(pady=(0, 16))

        ctk.CTkLabel(tab, text="Кнопка мыши").pack()
        self.btn_choice = ctk.CTkSegmentedButton(
            tab, values=["Левая", "Правая"], command=self.on_button
        )
        self.btn_choice.set("Левая")
        self.btn_choice.pack(pady=(4, 16))

        ctk.CTkLabel(tab, text="Горячая клавиша").pack()
        self.click_hotkey_btn = ctk.CTkButton(
            tab, text="", height=34, fg_color="#3a3a3a",
            hover_color="#4a4a4a", command=lambda: self.start_binding("click"),
        )
        self.click_hotkey_btn.pack(padx=20, pady=(4, 16), fill="x")

        self.toggle_btn = ctk.CTkButton(
            tab, text="", height=44,
            font=ctk.CTkFont(size=16, weight="bold"), command=self.toggle_click,
        )
        self.toggle_btn.pack(padx=20, fill="x")

        self.hint = ctk.CTkLabel(tab, text="", text_color="gray")
        self.hint.pack(pady=(14, 0))

    def build_macro_tab(self, tab):
        self.macro_info = ctk.CTkLabel(
            tab, text="", text_color="gray", height=40, wraplength=290
        )
        self.macro_info.pack(pady=(8, 6))

        self.record_btn = ctk.CTkButton(
            tab, text="", height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=lambda: self.toggle_record(from_button=True),
        )
        self.record_btn.pack(padx=20, pady=(0, 8), fill="x")

        self.play_btn = ctk.CTkButton(
            tab, text="", height=40,
            font=ctk.CTkFont(size=15, weight="bold"), command=self.toggle_play,
        )
        self.play_btn.pack(padx=20, pady=(0, 12), fill="x")

        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(pady=(0, 8))
        ctk.CTkLabel(row, text="Повторов:").pack(side="left")
        self.repeat_entry = ctk.CTkEntry(row, width=60, justify="center")
        self.repeat_entry.insert(0, "1")
        self.repeat_entry.pack(side="left", padx=8)
        self.infinite_switch = ctk.CTkSwitch(
            row, text="бесконечно", width=100, command=self.on_infinite
        )
        self.infinite_switch.pack(side="left")

        row2 = ctk.CTkFrame(tab, fg_color="transparent")
        row2.pack(pady=(0, 12))
        ctk.CTkLabel(row2, text="Скорость воспроизведения:").pack(side="left")
        self.speed_menu = ctk.CTkOptionMenu(
            row2, values=["0.5x", "1x", "2x", "4x"], width=80
        )
        self.speed_menu.set("1x")
        self.speed_menu.pack(side="left", padx=8)

        ctk.CTkLabel(tab, text="Горячие клавиши (нажми, чтобы изменить)").pack()
        row3 = ctk.CTkFrame(tab, fg_color="transparent")
        row3.pack(padx=20, pady=(4, 12), fill="x")
        self.rec_hotkey_btn = ctk.CTkButton(
            row3, text="", height=32, fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=lambda: self.start_binding("record"),
        )
        self.rec_hotkey_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.play_hotkey_btn = ctk.CTkButton(
            row3, text="", height=32, fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=lambda: self.start_binding("play"),
        )
        self.play_hotkey_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        row4 = ctk.CTkFrame(tab, fg_color="transparent")
        row4.pack(padx=20, fill="x")
        ctk.CTkButton(
            row4, text="Сохранить в файл", height=32, command=self.save_macro
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            row4, text="Загрузить", height=32, command=self.load_macro
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

    # ------------------------------------------------------------------
    # Надписи и статус
    # ------------------------------------------------------------------
    def refresh_labels(self):
        names = {action: value[1] for action, value in self.hotkeys.items()}

        # статус
        if self.recording:
            self.status.configure(text="● Идёт запись", text_color="#e5534b")
        elif self.playing:
            self.status.configure(text="▶ Воспроизведение", text_color="#3fb950")
        elif self.clicking:
            self.status.configure(text="Автокликер работает", text_color="#3fb950")
        else:
            self.status.configure(text="Остановлен", text_color="#9aa0a6")

        # вкладка «Автокликер»
        if self.binding == "click":
            self.click_hotkey_btn.configure(
                text="Нажми любую клавишу... (Esc — отмена)"
            )
        else:
            self.click_hotkey_btn.configure(
                text=f"{names['click']}  (нажми, чтобы изменить)"
            )
        verb = "Стоп" if self.clicking else "Старт"
        self.toggle_btn.configure(text=f"{verb} ({names['click']})")
        self.hint.configure(text=f"Наведи курсор на цель и нажми {names['click']}")

        # вкладка «Макрос»
        if self.recording:
            info = f"Идёт запись. Остановить: {names['record']}"
        elif self.playing:
            info = f"Идёт воспроизведение. Остановить: {names['play']}"
        elif self.macro:
            info = (f"Событий: {len(self.macro)} · "
                    f"длительность: {self.macro[-1][1]:.1f} с")
        else:
            info = "Макрос пуст. Нажми запись и сделай нужные действия"
        if self.note:
            info += "\n" + self.note
        self.macro_info.configure(text=info)

        if self.recording:
            self.record_btn.configure(
                text=f"■ Остановить запись ({names['record']})",
                fg_color="#c0392b", hover_color="#e74c3c",
            )
        else:
            self.record_btn.configure(
                text=f"● Запись ({names['record']})",
                fg_color=self.default_color, hover_color=self.default_hover,
            )
        if self.playing:
            self.play_btn.configure(text=f"■ Стоп ({names['play']})")
        else:
            self.play_btn.configure(text=f"▶ Воспроизвести ({names['play']})")

        if self.binding == "record":
            self.rec_hotkey_btn.configure(text="Нажми клавишу...")
        else:
            self.rec_hotkey_btn.configure(text=f"Запись: {names['record']}")
        if self.binding == "play":
            self.play_hotkey_btn.configure(text="Нажми клавишу...")
        else:
            self.play_hotkey_btn.configure(text=f"Воспр.: {names['play']}")

    def set_note(self, text):
        self.note = text
        self.refresh_labels()

    # ------------------------------------------------------------------
    # Автокликер
    # ------------------------------------------------------------------
    def apply_cps(self, event=None):
        """Читает скорость из поля. Верхнего ограничения нет."""
        text = self.cps_entry.get().strip().replace(",", ".")
        try:
            value = float(text)
            if not math.isfinite(value) or not (0.001 <= value <= 1e9):
                raise ValueError
        except ValueError:
            self.cps_entry.delete(0, "end")
            self.cps_entry.insert(0, f"{self.cps:g}")
            return
        self.cps = value

    def on_unlimited(self):
        self.unlimited = bool(self.unlimited_switch.get())
        self.cps_entry.configure(
            state="disabled" if self.unlimited else "normal"
        )

    def on_button(self, value):
        self.button = Button.left if value == "Левая" else Button.right

    def toggle_click(self):
        self.note = ""
        if self.clicking:
            self.clicking = False
        else:
            self.apply_cps()
            self.stop_recording()
            self.playing = False
            self.clicking = True
        self.refresh_labels()

    def click_loop(self):
        while self.running:
            # Пока автокликер выключен, поток просто ждёт: нагрузка на процессор 0%
            self.click_event.wait()
            if not self.running:
                return
            last = time.perf_counter()
            carry = 1.0  # первый клик сразу
            while self.clicking and self.running:
                try:
                    cps = MAX_CPS if self.unlimited else self.cps
                    now = time.perf_counter()
                    # копим «долг» по времени и делаем столько кликов, сколько
                    # положено: скорость точная, даже если система проспала лишнее
                    carry = min(carry + (now - last) * cps, cps * 0.1 + 1)
                    last = now
                    n = int(carry)
                    if n:
                        carry -= n
                        mouse.click(self.button, n)
                    pause = (1 - carry) / cps
                    time.sleep(min(max(pause, MIN_PAUSE), MAX_PAUSE))
                except Exception as exc:
                    print("Ошибка в цикле кликов:", exc)
                    time.sleep(0.1)

    # ------------------------------------------------------------------
    # Запись макроса
    # ------------------------------------------------------------------
    def toggle_record(self, from_button=False):
        self.note = ""
        if self.recording:
            self.stop_recording(trim=0.5 if from_button else 0.0)
        else:
            self.clicking = False
            self.playing = False
            self.rec_buf = []
            now = time.perf_counter()
            self.rec_start = now
            # если запись запущена кнопкой в окне, пропускаем её собственный клик
            self.rec_ignore_until = now + (0.6 if from_button else 0.0)
            self.recording = True
            self.start_mouse_listener()
        self.refresh_labels()

    def stop_recording(self, trim=0.0):
        if not self.recording:
            return
        self.recording = False
        self.stop_mouse_listener()
        limit = time.perf_counter() - self.rec_start - trim
        events = [e for e in list(self.rec_buf) if e[1] <= limit]
        if not events:
            self.note = "Ничего не записано, прошлый макрос сохранён"
            return
        t0 = events[0][1]
        self.macro = [[e[0], round(e[1] - t0, 4), *e[2:]] for e in events]

    def rec_time(self):
        """Время от начала записи или None, если записывать сейчас не нужно."""
        now = time.perf_counter()
        if not self.recording or self.playing or now < self.rec_ignore_until:
            return None
        return now - self.rec_start

    def record_key(self, key, kid, pressed):
        t = self.rec_time()
        if t is None:
            return
        if kid in {value[0] for value in self.hotkeys.values()}:
            return  # горячие клавиши в макрос не попадают
        packed = pack_key(key)
        if packed is not None:
            self.rec_buf.append(["key", t, packed, pressed])

    def start_mouse_listener(self):
        """Слушатель мыши нужен только для записи. Вне записи он не работает,
        иначе каждый клик автокликера проходил бы через Python-обработчик."""
        if self.mouse_listener is None:
            self.mouse_listener = MouseListener(
                on_click=self.on_mouse_click, on_scroll=self.on_mouse_scroll
            )
            self.mouse_listener.start()

    def stop_mouse_listener(self):
        if self.mouse_listener is not None:
            self.mouse_listener.stop()
            self.mouse_listener = None

    def on_mouse_click(self, x, y, button, pressed):
        try:
            t = self.rec_time()
            if t is not None:
                self.rec_buf.append(
                    ["click", t, int(x), int(y), button.name, bool(pressed)]
                )
        except Exception as exc:
            print("Ошибка записи клика:", exc)

    def on_mouse_scroll(self, x, y, dx, dy):
        try:
            t = self.rec_time()
            if t is not None:
                self.rec_buf.append(
                    ["scroll", t, int(x), int(y), int(dx), int(dy)]
                )
        except Exception as exc:
            print("Ошибка записи прокрутки:", exc)

    # ------------------------------------------------------------------
    # Воспроизведение макроса
    # ------------------------------------------------------------------
    def on_infinite(self):
        infinite = bool(self.infinite_switch.get())
        self.repeat_entry.configure(state="disabled" if infinite else "normal")

    def read_repeats(self):
        """Сколько раз повторять (None = бесконечно)."""
        if self.infinite_switch.get():
            return None
        try:
            value = int(float(self.repeat_entry.get().strip().replace(",", ".")))
        except ValueError:
            value = 1
        return max(1, value)

    def toggle_play(self):
        self.note = ""
        if self.playing:
            self.playing = False  # поток сам завершится и отпустит клавиши
            self.refresh_labels()
            return
        if self.recording:
            self.stop_recording()
            self.refresh_labels()
            return
        if self.play_thread is not None and self.play_thread.is_alive():
            return
        if not self.macro:
            self.set_note("Сначала запиши макрос")
            return
        speed = float(self.speed_menu.get().rstrip("x"))
        repeats = self.read_repeats()
        self.clicking = False
        self.playing = True
        self.play_thread = threading.Thread(
            target=self.play_loop,
            args=(list(self.macro), speed, repeats),
            daemon=True,
        )
        self.play_thread.start()
        self.refresh_labels()

    def wait_until(self, target):
        while self.playing:
            remaining = target - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.05))

    def play_loop(self, events, speed, repeats):
        try:
            done = 0
            while self.playing:
                self.run_macro_once(events, speed)
                done += 1
                if repeats is not None and done >= repeats:
                    break
                self.wait_until(time.perf_counter() + 0.05)
        except Exception as exc:
            print("Ошибка воспроизведения:", exc)
        finally:
            self.release_all()
            self.playing = False
            self.events.put(("play_done",))

    def run_macro_once(self, events, speed):
        t0 = time.perf_counter()
        for ev in events:
            self.wait_until(t0 + ev[1] / speed)
            if not self.playing:
                return
            self.do_event(ev)

    def do_event(self, ev):
        kind = ev[0]
        if kind == "click":
            _, _, x, y, name, pressed = ev
            button = getattr(Button, name, Button.left)
            mouse.position = (x, y)
            if pressed:
                mouse.press(button)
                self.down_buttons.add(button)
            else:
                mouse.release(button)
                self.down_buttons.discard(button)
        elif kind == "scroll":
            _, _, x, y, dx, dy = ev
            mouse.position = (x, y)
            mouse.scroll(dx, dy)
        elif kind == "key":
            _, _, packed, pressed = ev
            key = unpack_key(packed)
            if key is None:
                return
            tag = tuple(packed)
            if pressed:
                keyboard.press(key)
                self.down_keys[tag] = key
            else:
                keyboard.release(key)
                self.down_keys.pop(tag, None)

    def release_all(self):
        """Отпускает всё, что макрос не успел отпустить (чтобы ничего не залипло)."""
        for key in list(self.down_keys.values()):
            try:
                keyboard.release(key)
            except Exception:
                pass
        for button in list(self.down_buttons):
            try:
                mouse.release(button)
            except Exception:
                pass
        self.down_keys.clear()
        self.down_buttons.clear()

    # ------------------------------------------------------------------
    # Сохранение и загрузка макроса
    # ------------------------------------------------------------------
    def save_macro(self):
        if not self.macro:
            self.set_note("Нечего сохранять: макрос пуст")
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить макрос", defaultextension=".json",
            filetypes=[("Макрос", "*.json")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "events": self.macro}, f,
                          ensure_ascii=False)
            self.set_note("Макрос сохранён")
        except OSError:
            self.set_note("Не удалось сохранить файл")

    def load_macro(self):
        path = filedialog.askopenfilename(
            title="Загрузить макрос", filetypes=[("Макрос", "*.json")]
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            events = validate_events(data.get("events", []))
        except (OSError, ValueError, AttributeError, TypeError):
            self.set_note("Не удалось прочитать файл")
            return
        if not events:
            self.set_note("В файле нет подходящего макроса")
            return
        self.playing = False
        self.macro = events
        self.set_note("Макрос загружен")

    # ------------------------------------------------------------------
    # Клавиатура: поток слушателя только кладёт события в очередь
    # ------------------------------------------------------------------
    def on_press(self, key):
        try:
            kid = key_id(key)
            if kid in self.held:  # игнорируем автоповтор при удержании
                return
            self.held.add(kid)
            self.record_key(key, kid, True)
            self.events.put(("key", kid, key_name(key)))
        except Exception as exc:
            print("Ошибка обработки клавиши:", exc)

    def on_release(self, key):
        try:
            kid = key_id(key)
            self.record_key(key, kid, False)
            self.held.discard(kid)
        except Exception as exc:
            print("Ошибка обработки клавиши:", exc)

    # ------------------------------------------------------------------
    # Главный поток забирает события из очереди
    # ------------------------------------------------------------------
    def poll_events(self):
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] == "play_done":
                    self.refresh_labels()
                else:
                    self.handle_key(item[1], item[2])
        except queue.Empty:
            pass
        if self.running:
            self.after(POLL_MS, self.poll_events)

    def start_binding(self, action):
        self.clicking = False
        self.playing = False
        self.stop_recording()
        self.binding = action
        self.refresh_labels()

    def handle_key(self, kid, name):
        if name != self.last_name:
            self.last_name = name
            self.last_key.configure(text=f"Последняя нажатая клавиша: {name}")
        if self.binding:
            action = self.binding
            self.binding = None
            if kid != ESC_ID:
                taken = [a for a, (k, _) in self.hotkeys.items()
                         if k == kid and a != action]
                if taken:
                    self.last_key.configure(text=f"Клавиша {name} уже занята")
                else:
                    self.hotkeys[action] = (kid, name)
            self.refresh_labels()
            return
        for action, (hotkey_id, _) in self.hotkeys.items():
            if kid == hotkey_id:
                if action == "click":
                    self.toggle_click()
                elif action == "record":
                    self.toggle_record()
                elif action == "play":
                    self.toggle_play()
                break

    def on_close(self):
        self.running = False
        self.clicking = False
        self.click_event.set()  # будим поток кликов, чтобы он завершился
        self.recording = False
        self.playing = False
        self.listener.stop()
        self.stop_mouse_listener()
        self.destroy()


if __name__ == "__main__":
    AutoClicker().mainloop()