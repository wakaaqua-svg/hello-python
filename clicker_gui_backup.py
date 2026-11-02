import ctypes
import json
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key, KeyCode, Listener
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController
from pynput.mouse import Listener as MouseListener

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

mouse = MouseController()
keyboard = KeyboardController()
ESC_ID = ("key", Key.esc)

# --- Настройки, от которых зависит нагрузка на процессор ---
DEFAULT_MAX_CPS = 1000  # скорость в режиме «Без ограничения» (меняется в настройках)
CPS_LIMIT = 100000      # больше этого число в полях не принимается
MIN_PAUSE = 0.008       # поток кликов просыпается не чаще ~125 раз в секунду
MAX_PAUSE = 0.25        # и не спит дольше 0.25 с (быстро реагирует на смену скорости)
POLL_MS = 20            # как часто окно проверяет очередь нажатых клавиш
MOUSE_POLL_S = 0.008    # как часто опрашиваются кнопки мыши, назначенные горячими

# Кнопки мыши, которые можно назначить горячими. Левую и правую нельзя:
# автокликер сам ими кликает, и клавиша запускала бы сама себя.
MOUSE_VK = {"middle": 0x04, "x1": 0x05, "x2": 0x06}
MOUSE_NAMES = {"middle": "MOUSE3", "x1": "MOUSE4", "x2": "MOUSE5"}

# Названия знаков и клавиш цифрового блока (по коду клавиши, не по раскладке)
OEM_NAMES = {
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
    0x6A: "NUM *", 0x6B: "NUM +", 0x6D: "NUM -", 0x6E: "NUM .", 0x6F: "NUM /",
}


# --- Прямой вызов Windows API ---
# Все клики за один «тик» уходят в систему ОДНИМ вызовом SendInput (раньше на
# каждый клик было два вызова через библиотеку pynput). Это резко снижает нагрузку.
try:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
except AttributeError:  # не Windows (например, при тестах)
    user32 = kernel32 = None


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUT(ctypes.Structure):
    class _Union(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _Union)]


if user32 is not None:
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    kernel32.GetCurrentThread.restype = ctypes.c_void_p
    kernel32.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
    kernel32.SetThreadPriority.restype = ctypes.c_int


class FastClicker:
    """Отправляет пачку кликов одним вызовом SendInput, курсор не двигается."""

    CHUNK = 256
    FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010)}

    def __init__(self):
        self.cache = {}  # готовые массивы событий, чтобы не создавать их заново

    def build(self, name, n):
        down, up = self.FLAGS[name]
        arr = (INPUT * (2 * n))()
        for i in range(n):
            arr[2 * i].type = 0  # INPUT_MOUSE
            arr[2 * i].mi.dwFlags = down
            arr[2 * i + 1].type = 0
            arr[2 * i + 1].mi.dwFlags = up
        return arr

    def click(self, name, n):
        while n > 0:
            m = min(n, self.CHUNK)
            arr = self.cache.get((name, m))
            if arr is None:
                arr = self.cache[(name, m)] = self.build(name, m)
            total = 2 * m
            sent = user32.SendInput(total, arr, ctypes.sizeof(INPUT))
            # Windows may insert fewer events than requested. Retry only the
            # unsent tail; never silently report a full batch as successful.
            while sent < total:
                remaining = total - sent
                offset = sent * ctypes.sizeof(INPUT)
                ptr = ctypes.cast(
                    ctypes.byref(arr, offset), ctypes.POINTER(INPUT)
                )
                extra = user32.SendInput(
                    remaining, ptr, ctypes.sizeof(INPUT)
                )
                if extra == 0:
                    raise OSError(
                        ctypes.get_last_error(),
                        f"SendInput inserted {sent}/{total} events"
                    )
                sent += extra
            n -= m


fast_clicker = FastClicker() if user32 is not None else None


def send_clicks(button, n):
    if fast_clicker is not None and button.name in FastClicker.FLAGS:
        fast_clicker.click(button.name, n)
    else:
        mouse.click(button, n)


def set_thread_priority(level):
    """Приоритет текущего потока: -1 чуть ниже обычного, 0 обычный."""
    if kernel32 is None:
        return
    try:
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), level)
    except Exception as exc:
        print("Не удалось изменить приоритет потока:", exc)


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
            if not math.isfinite(t) or t < 0:
                continue
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
        self.title("VSS CLICKER")
        # Прозрачная иконка убирает стандартный значок Tkinter в заголовке окна.
        self._blank_icon = tk.PhotoImage(width=16, height=16)
        self.iconphoto(True, self._blank_icon)
        self.geometry("540x860")
        self.minsize(440, 650)
        self.resizable(True, True)
        self.configure(fg_color="#050505")

        # --- состояние ---
        self.running = True
        self.click_event = threading.Event()
        self.clicking = False
        self.mouse_listener = None
        self.last_name = None
        self.recording = False
        self.playing = False
        self.binding = None
        self.unlimited = False
        self.hold_mode = False
        self.cps = 10.0
        self.max_cps = float(DEFAULT_MAX_CPS)
        self.low_priority = True
        self.mouse_poll_event = threading.Event()
        self.mouse_wanted = ()
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
        self.play_stop_event = threading.Event()
        self.down_keys = {}
        self.down_buttons = set()
        self.note = ""

        # Матовый интерфейс в духе Apple: без бликов, blur и анимаций.
        self.configure(fg_color="#050505")
        self.geometry("540x860")
        self.minsize(440, 650)
        self.resizable(True, True)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(27, 18))
        ctk.CTkLabel(
            header, text="VSS CLICKER", text_color="#F2F2F2",
            font=ctk.CTkFont(family="Segoe UI", size=27, weight="bold")
        ).pack(anchor="w")
        ctk.CTkLabel(
            header, text="AUTOMATION  ·  INPUT CONTROL",
            text_color="#858585", font=ctk.CTkFont(size=10, weight="bold")
        ).pack(anchor="w", pady=(2, 14))
        status_row = ctk.CTkFrame(
            header, fg_color="#111111", corner_radius=16,
            border_width=1, border_color="#262626"
        )
        status_row.pack(fill="x")
        ctk.CTkLabel(
            status_row, text="SYSTEM STATUS", text_color="#858585",
            font=ctk.CTkFont(size=10, weight="bold")
        ).pack(side="left", padx=15, pady=13)
        self.status = ctk.CTkLabel(
            status_row, text="Остановлен", text_color="#D0D0D0",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold")
        )
        self.status.pack(side="right", padx=15, pady=12)

        self.tabs = ctk.CTkTabview(
            self, width=480, height=620, fg_color="#080808",
            segmented_button_fg_color="#151515",
            segmented_button_selected_color="#D8D8D8",
            segmented_button_selected_hover_color="#E5E5E5",
            segmented_button_unselected_color="#151515",
            segmented_button_unselected_hover_color="#242424",
            text_color="#111111", corner_radius=20,
            border_width=1, border_color="#292929"
        )
        self.tabs.pack(padx=26, pady=(0, 9), fill="both", expand=True)
        self.build_click_tab(self.tabs.add("CLICKER"))
        self.build_macro_tab(self.tabs.add("MACROS"))
        self.build_settings_tab(self.tabs.add("SETTINGS"))

        self.last_key = ctk.CTkLabel(
            self, text="LAST INPUT  /  —", text_color="#777777",
            font=ctk.CTkFont(size=10, weight="bold")
        )
        self.last_key.pack(pady=(3, 15))

        self.default_color = self.toggle_btn.cget("fg_color")
        self.default_hover = self.toggle_btn.cget("hover_color")
        self.refresh_labels()

        threading.Thread(target=self.click_loop, daemon=True).start()
        threading.Thread(target=self.mouse_poll_loop, daemon=True).start()
        self.update_mouse_poll()
        self.listener = Listener(on_press=self.on_press, on_release=self.on_release)
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
    def _section(self, parent, title, subtitle=None):
        card = ctk.CTkFrame(
            parent, fg_color="#121212", corner_radius=19,
            border_width=1, border_color="#292929"
        )
        card.pack(fill="x", padx=12, pady=(12, 0))
        ctk.CTkLabel(
            card, text=title, text_color="#F0F0F0",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold")
        ).pack(anchor="w", padx=15, pady=(13, 2))
        if subtitle:
            ctk.CTkLabel(
                card, text=subtitle, text_color="#888888",
                font=ctk.CTkFont(size=10)
            ).pack(anchor="w", padx=15, pady=(0, 8))
        return card

    def _label(self, parent, text):
        ctk.CTkLabel(
            parent, text=text, text_color="#BDBDBD",
            font=ctk.CTkFont(size=10, weight="bold")
        ).pack(anchor="w", padx=15, pady=(9, 3))

    def build_click_tab(self, tab):
        tab.configure(fg_color="#080808")
        card = self._section(tab, "CLICK ENGINE", "Настрой параметры автоматического ввода")
        self._label(card, "CLICKS PER SECOND")
        self.cps_entry = ctk.CTkEntry(
            card, height=38, width=155, justify="center", corner_radius=9,
            fg_color="#090909", border_color="#343434", text_color="#F5F5F5"
        )
        self.cps_entry.insert(0, "10")
        self.cps_entry.pack(anchor="w", padx=15, pady=(2, 10))
        self.cps_entry.bind("<Return>", self.apply_cps)
        self.cps_entry.bind("<FocusOut>", self.apply_cps)
        self.unlimited_switch = ctk.CTkSwitch(
            card, text="Ограничение отключено", command=self.on_unlimited,
            progress_color="#AFAFAF", button_color="#E5E5E5",
            button_hover_color="#FFFFFF", text_color="#C7C7C7"
        )
        self.unlimited_switch.pack(anchor="w", padx=15, pady=(0, 14))

        card2 = self._section(tab, "INPUT CONFIGURATION")
        self._label(card2, "КНОПКА МЫШИ")
        self.btn_choice = ctk.CTkSegmentedButton(
            card2, values=["Левая", "Правая"], command=self.on_button,
            selected_color="#BFC1C3", selected_hover_color="#D0D2D4",
            unselected_color="#1A1A1A", unselected_hover_color="#262626",
            text_color="#D8D8D8"
        )
        self.btn_choice.set("Левая")
        self.btn_choice.pack(fill="x", padx=15, pady=(2, 10))
        self._label(card2, "РЕЖИМ ГОРЯЧЕЙ КЛАВИШИ")
        self.mode_choice = ctk.CTkSegmentedButton(
            card2, values=["Переключение", "Удержание"], command=self.on_mode,
            selected_color="#BFC1C3", selected_hover_color="#D0D2D4",
            unselected_color="#1A1A1A", unselected_hover_color="#262626",
            text_color="#D8D8D8"
        )
        self.mode_choice.set("Переключение")
        self.mode_choice.pack(fill="x", padx=15, pady=(2, 10))
        self._label(card2, "HOTKEY")
        self.click_hotkey_btn = ctk.CTkButton(
            card2, text="", height=37, corner_radius=9,
            fg_color="#1B1B1B", hover_color="#262626",
            border_width=1, border_color="#353535", text_color="#EEEEEE",
            command=lambda: self.start_binding("click")
        )
        self.click_hotkey_btn.pack(fill="x", padx=15, pady=(2, 14))
        self.toggle_btn = ctk.CTkButton(
            tab, text="", height=50, corner_radius=15,
            fg_color="#D0D0D0", hover_color="#E0E0E0", text_color="#101010",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            command=self.toggle_click
        )
        self.toggle_btn.pack(fill="x", padx=10, pady=(14, 4))
        self.hint = ctk.CTkLabel(
            tab, text="", text_color="#858585", font=ctk.CTkFont(size=10)
        )
        self.hint.pack(pady=(4, 0))

    def build_macro_tab(self, tab):
        tab.configure(fg_color="#080808")
        card = self._section(tab, "MACRO RECORDER", "Запись и воспроизведение последовательностей")
        self.macro_info = ctk.CTkLabel(
            card, text="", text_color="#C5C5C5", height=48,
            wraplength=310, justify="left", font=ctk.CTkFont(size=11)
        )
        self.macro_info.pack(fill="x", padx=15, pady=(4, 10))
        self.record_btn = ctk.CTkButton(
            card, text="", height=40, corner_radius=10,
            fg_color="#1B1B1B", hover_color="#292929",
            border_width=1, border_color="#414141", text_color="#F1F1F1",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            command=lambda: self.toggle_record(from_button=True)
        )
        self.record_btn.pack(fill="x", padx=15, pady=(0, 8))
        self.play_btn = ctk.CTkButton(
            card, text="", height=40, corner_radius=10,
            fg_color="#D0D0D0", hover_color="#E0E0E0", text_color="#101010",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"), command=self.toggle_play
        )
        self.play_btn.pack(fill="x", padx=15, pady=(0, 14))

        card2 = self._section(tab, "PLAYBACK OPTIONS")
        row = ctk.CTkFrame(card2, fg_color="transparent")
        row.pack(fill="x", padx=15, pady=(9, 8))
        ctk.CTkLabel(row, text="Повторов", text_color="#C7C7C7").pack(side="left")
        self.repeat_entry = ctk.CTkEntry(
            row, width=75, height=33, justify="center", corner_radius=8,
            fg_color="#101010", border_color="#444444"
        )
        self.repeat_entry.insert(0, "1")
        self.repeat_entry.pack(side="right")
        self.infinite_switch = ctk.CTkSwitch(
            card2, text="Бесконечный повтор", width=150, command=self.on_infinite,
            progress_color="#AFAFAF", button_color="#E5E5E5", text_color="#C7C7C7"
        )
        self.infinite_switch.pack(anchor="w", padx=15, pady=(0, 10))
        row2 = ctk.CTkFrame(card2, fg_color="transparent")
        row2.pack(fill="x", padx=15, pady=(0, 13))
        ctk.CTkLabel(row2, text="Скорость", text_color="#C7C7C7").pack(side="left")
        self.speed_menu = ctk.CTkOptionMenu(
            row2, values=["0.5x", "1x", "2x", "4x"], width=90, height=33,
            corner_radius=8, fg_color="#292929", button_color="#414141",
            button_hover_color="#505050"
        )
        self.speed_menu.set("1x")
        self.speed_menu.pack(side="right")

        card3 = self._section(tab, "MACRO HOTKEYS")
        row3 = ctk.CTkFrame(card3, fg_color="transparent")
        row3.pack(fill="x", padx=15, pady=10)
        self.rec_hotkey_btn = ctk.CTkButton(
            row3, text="", height=35, corner_radius=9,
            fg_color="#1B1B1B", hover_color="#292929", command=lambda: self.start_binding("record")
        )
        self.rec_hotkey_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.play_hotkey_btn = ctk.CTkButton(
            row3, text="", height=35, corner_radius=9,
            fg_color="#1B1B1B", hover_color="#292929", command=lambda: self.start_binding("play")
        )
        self.play_hotkey_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))
        row4 = ctk.CTkFrame(card3, fg_color="transparent")
        row4.pack(fill="x", padx=15, pady=(0, 14))
        ctk.CTkButton(
            row4, text="Сохранить", height=33, corner_radius=9,
            fg_color="#292929", hover_color="#393939", command=self.save_macro
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            row4, text="Загрузить", height=33, corner_radius=9,
            fg_color="#292929", hover_color="#393939", command=self.load_macro
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

    def build_settings_tab(self, tab):
        tab.configure(fg_color="#080808")
        card = self._section(tab, "PERFORMANCE", "Параметры нагрузки и отклика")
        self.priority_switch = ctk.CTkSwitch(
            card, text="Низкий приоритет потока кликов", command=self.on_priority,
            progress_color="#AFAFAF", button_color="#E5E5E5", text_color="#C7C7C7"
        )
        self.priority_switch.select()
        self.priority_switch.pack(anchor="w", padx=15, pady=(10, 4))
        ctk.CTkLabel(
            card, text="Поток кликов уступает ресурсы другим приложениям.",
            text_color="#888888", wraplength=310, justify="left",
            font=ctk.CTkFont(size=10)
        ).pack(anchor="w", padx=15, pady=(0, 13))
        card2 = self._section(tab, "MAXIMUM CLICK RATE")
        self._label(card2, "CPS В РЕЖИМЕ БЕЗ ОГРАНИЧЕНИЯ")
        self.max_cps_entry = ctk.CTkEntry(
            card2, width=155, height=38, justify="center", corner_radius=9,
            fg_color="#101010", border_color="#444444"
        )
        self.max_cps_entry.insert(0, str(DEFAULT_MAX_CPS))
        self.max_cps_entry.pack(anchor="w", padx=15, pady=(2, 9))
        self.max_cps_entry.bind("<Return>", self.apply_max_cps)
        self.max_cps_entry.bind("<FocusOut>", self.apply_max_cps)
        ctk.CTkLabel(
            card2, text="Высокие значения могут увеличить нагрузку на игру.",
            text_color="#888888", wraplength=310, justify="left",
            font=ctk.CTkFont(size=10)
        ).pack(anchor="w", padx=15, pady=(0, 13))
        card3 = self._section(tab, "SUPPORTED MOUSE HOTKEYS")
        ctk.CTkLabel(
            card3, text="MOUSE3 — колесо\nMOUSE4 / MOUSE5 — боковые кнопки",
            text_color="#C5C5C5", justify="left", font=ctk.CTkFont(size=11)
        ).pack(anchor="w", padx=15, pady=(6, 14))

        card4 = self._section(tab, "CONFIGURATION", "Сохрани настройки и перенеси их на другой компьютер")
        config_row = ctk.CTkFrame(card4, fg_color="transparent")
        config_row.pack(fill="x", padx=15, pady=(8, 14))
        ctk.CTkButton(
            config_row, text="Сохранить конфиг", height=36, corner_radius=9,
            fg_color="#292929", hover_color="#393939",
            command=self.save_config
        ).pack(side="left", expand=True, fill="x", padx=(0, 5))
        ctk.CTkButton(
            config_row, text="Загрузить конфиг", height=36, corner_radius=9,
            fg_color="#292929", hover_color="#393939",
            command=self.load_config
        ).pack(side="left", expand=True, fill="x", padx=(5, 0))

    def _pack_hotkey_id(self, kid):
        if not isinstance(kid, tuple) or len(kid) != 2:
            return None
        kind, value = kid
        if kind == "key":
            return ["special", value.name]
        if kind == "vk":
            return ["vk", int(value)]
        if kind == "char":
            return ["char", str(value)]
        return None

    def _unpack_hotkey_id(self, packed):
        if not isinstance(packed, (list, tuple)) or len(packed) != 2:
            raise ValueError("Некорректная горячая клавиша")
        kind, value = packed
        if kind == "special":
            key = Key[str(value)]
            return ("key", key), key_name(key)
        if kind == "vk":
            key = KeyCode.from_vk(int(value))
            return ("vk", int(value)), key_name(key)
        if kind == "char":
            char = str(value)
            if len(char) != 1:
                raise ValueError("Некорректный символ")
            key = KeyCode.from_char(char)
            return ("char", char.lower()), key_name(key)
        raise ValueError("Неизвестный тип горячей клавиши")

    def save_config(self):
        self.apply_cps()
        self.apply_max_cps()
        path = filedialog.asksaveasfilename(
            title="Сохранить конфигурацию VSS CLICKER",
            defaultextension=".json",
            filetypes=[("Конфигурация VSS CLICKER", "*.json")],
            initialfile="vss_clicker_config.json",
        )
        if not path:
            return
        data = {
            "format": "vss_clicker_config",
            "version": 1,
            "settings": {
                "cps": self.cps,
                "unlimited": bool(self.unlimited_switch.get()),
                "button": "right" if self.button == Button.right else "left",
                "hold_mode": bool(self.hold_mode),
                "max_cps": self.max_cps,
                "low_priority": bool(self.priority_switch.get()),
                "repeat": self.repeat_entry.get().strip(),
                "infinite_repeat": bool(self.infinite_switch.get()),
                "playback_speed": self.speed_menu.get(),
            },
            "hotkeys": {
                action: self._pack_hotkey_id(value[0])
                for action, value in self.hotkeys.items()
            },
            "macro": self.macro,
        }
        try:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            self.set_note("Конфигурация сохранена")
        except (OSError, TypeError, ValueError) as exc:
            self.set_note(f"Не удалось сохранить конфиг: {exc}")

    def load_config(self):
        path = filedialog.askopenfilename(
            title="Загрузить конфигурацию VSS CLICKER",
            filetypes=[("Конфигурация VSS CLICKER", "*.json"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict) or data.get("format") != "vss_clicker_config":
                raise ValueError("Это не файл конфигурации VSS CLICKER")
            settings = data.get("settings")
            if not isinstance(settings, dict):
                raise ValueError("В конфиге отсутствуют настройки")

            cps = float(settings.get("cps", 10))
            max_cps = float(settings.get("max_cps", DEFAULT_MAX_CPS))
            if not math.isfinite(cps) or not 0.001 <= cps <= CPS_LIMIT:
                raise ValueError("Некорректное значение CPS")
            if not math.isfinite(max_cps) or not 1 <= max_cps <= CPS_LIMIT:
                raise ValueError("Некорректное максимальное CPS")
            button_name = settings.get("button", "left")
            if button_name not in ("left", "right"):
                raise ValueError("Некорректная кнопка мыши")
            speed = settings.get("playback_speed", "1x")
            if speed not in ("0.5x", "1x", "2x", "4x"):
                speed = "1x"
            repeat = str(settings.get("repeat", "1"))
            if not repeat.isdigit() or int(repeat) < 1:
                repeat = "1"

            raw_hotkeys = data.get("hotkeys", {})
            if not isinstance(raw_hotkeys, dict):
                raise ValueError("Некорректные горячие клавиши")
            new_hotkeys = dict(self.hotkeys)
            for action in ("click", "record", "play"):
                if action in raw_hotkeys:
                    new_hotkeys[action] = self._unpack_hotkey_id(raw_hotkeys[action])
            macro = validate_events(data.get("macro", []))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.set_note(f"Не удалось загрузить конфиг: {exc}")
            return

        if self.recording:
            self.stop_recording()
        self.set_clicking(False)
        self.playing = False
        self.play_stop_event.set()
        self.hotkeys = new_hotkeys
        self.cps = cps
        self.max_cps = max_cps
        self.unlimited = bool(settings.get("unlimited", False))
        self.hold_mode = bool(settings.get("hold_mode", False))
        self.button = Button.right if button_name == "right" else Button.left
        self.low_priority = bool(settings.get("low_priority", True))
        self.macro = macro

        self.cps_entry.delete(0, "end")
        self.cps_entry.insert(0, f"{self.cps:g}")
        self.max_cps_entry.delete(0, "end")
        self.max_cps_entry.insert(0, f"{self.max_cps:g}")
        if self.unlimited:
            self.unlimited_switch.select()
        else:
            self.unlimited_switch.deselect()
        self.cps_entry.configure(state="disabled" if self.unlimited else "normal")
        self.btn_choice.set("Правая" if button_name == "right" else "Левая")
        self.mode_choice.set("Удержание" if self.hold_mode else "Переключение")
        if self.low_priority:
            self.priority_switch.select()
        else:
            self.priority_switch.deselect()
        self.repeat_entry.delete(0, "end")
        self.repeat_entry.insert(0, repeat)
        if settings.get("infinite_repeat", False):
            self.infinite_switch.select()
        else:
            self.infinite_switch.deselect()
        self.speed_menu.set(speed)
        self.refresh_labels()
        self.set_note("Конфигурация загружена")

    # Надписи и статус
    # ------------------------------------------------------------------
    def refresh_labels(self):
        names = {action: value[1] for action, value in self.hotkeys.items()}

        # статус
        if self.recording:
            self.status.configure(text="● Идёт запись", text_color="#D0A0A0")
        elif self.playing:
            self.status.configure(text="▶ Воспроизведение", text_color="#B8CDBB")
        elif self.clicking:
            self.status.configure(text="Автокликер работает", text_color="#B8CDBB")
        else:
            self.status.configure(text="Остановлен", text_color="#AAB2B7")

        # вкладка «Автокликер»
        if self.binding == "click":
            self.click_hotkey_btn.configure(
                text="Нажми любую клавишу... (Esc — отмена)"
            )
        else:
            self.click_hotkey_btn.configure(
                text=f"{names['click']}  (нажми, чтобы изменить)"
            )
        if self.hold_mode:
            # в режиме удержания кнопка в окне не нужна: клики идут, пока держишь клавишу
            self.toggle_btn.configure(
                text=f"Удерживай {names['click']}", state="disabled"
            )
            self.hint.configure(
                text=f"Наведи курсор на цель и держи {names['click']}"
            )
        else:
            verb = "Стоп" if self.clicking else "Старт"
            self.toggle_btn.configure(
                text=f"{verb} ({names['click']})", state="normal"
            )
            self.hint.configure(
                text=f"Наведи курсор на цель и нажми {names['click']}"
            )

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
            if not math.isfinite(value) or not (0.001 <= value <= CPS_LIMIT):
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

    def apply_max_cps(self, event=None):
        """Максимальная скорость для режима «Без ограничения»."""
        text = self.max_cps_entry.get().strip().replace(",", ".")
        try:
            value = float(text)
            if not math.isfinite(value) or not (1 <= value <= CPS_LIMIT):
                raise ValueError
        except ValueError:
            self.max_cps_entry.delete(0, "end")
            self.max_cps_entry.insert(0, f"{self.max_cps:g}")
            return
        self.max_cps = value

    def on_priority(self):
        self.low_priority = bool(self.priority_switch.get())

    def on_button(self, value):
        self.button = Button.left if value == "Левая" else Button.right

    def set_clicking(self, on):
        self.note = ""
        if on:
            self.apply_cps()
            self.apply_max_cps()
            self.stop_recording()
            self.playing = False
            self.play_stop_event.set()
        self.clicking = on
        self.refresh_labels()

    def toggle_click(self):
        self.set_clicking(not self.clicking)

    def on_mode(self, value):
        self.hold_mode = value == "Удержание"
        self.set_clicking(False)

    def click_loop(self):
        applied = None  # какой приоритет потока сейчас выставлен
        while self.running:
            # Пока автокликер выключен, поток просто ждёт: нагрузка на процессор 0%
            self.click_event.wait()
            if not self.running:
                return
            if applied != self.low_priority:
                applied = self.low_priority
                set_thread_priority(-1 if applied else 0)
            last_ns = time.perf_counter_ns()
            carry = 1.0  # первый клик сразу
            while self.clicking and self.running:
                try:
                    # Snapshot settings once per tick; the UI can change them
                    # while this worker is active.
                    cps = self.max_cps if self.unlimited else self.cps
                    button = self.button
                    now_ns = time.perf_counter_ns()
                    elapsed = (now_ns - last_ns) / 1_000_000_000
                    last_ns = now_ns

                    # Fractional accumulator keeps the average CPS stable.
                    # Bound catch-up to 100 ms to avoid huge bursts after a stall.
                    carry = min(carry + elapsed * cps, cps * 0.1 + 1.0)
                    count = int(carry)
                    if count:
                        carry -= count
                        send_clicks(button, count)

                    pause = (1.0 - carry) / cps
                    time.sleep(min(max(pause, MIN_PAUSE), MAX_PAUSE))
                except Exception as exc:
                    print("Ошибка в цикле кликов:", exc)
                    time.sleep(0.05)

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
            self.play_stop_event.set()
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
            if t is not None and ("mouse", button.name) not in self.hotkey_ids():
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
            self.playing = False
            self.play_stop_event.set()
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
        self.play_stop_event.clear()
        self.playing = True
        self.play_thread = threading.Thread(
            target=self.play_loop,
            args=(list(self.macro), speed, repeats),
            daemon=True,
        )
        self.play_thread.start()
        self.refresh_labels()

    def wait_until(self, target):
        # Event.wait() wakes immediately when the user stops playback.
        while self.playing and not self.play_stop_event.is_set():
            remaining = target - time.perf_counter()
            if remaining <= 0:
                return True
            if self.play_stop_event.wait(min(remaining, 0.02)):
                return False
        return False

    def play_loop(self, events, speed, repeats):
        try:
            done = 0
            while self.playing:
                self.run_macro_once(events, speed)
                done += 1
                if repeats is not None and done >= repeats:
                    break
                if not self.wait_until(time.perf_counter() + 0.05):
                    break
        except Exception as exc:
            print("Ошибка воспроизведения:", exc)
        finally:
            self.play_stop_event.set()
            self.release_all()
            self.playing = False
            self.events.put(("play_done",))

    def run_macro_once(self, events, speed):
        t0 = time.perf_counter()
        for ev in events:
            if not self.wait_until(t0 + ev[1] / speed):
                return
            if not self.playing or self.play_stop_event.is_set():
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
            if not isinstance(data, dict):
                raise ValueError("Некорректный формат макроса")
            events = validate_events(data.get("events", []))
        except (OSError, ValueError, AttributeError, TypeError):
            self.set_note("Не удалось прочитать файл")
            return
        if not events:
            self.set_note("В файле нет подходящего макроса")
            return
        self.playing = False
        self.play_stop_event.set()
        self.macro = events
        self.set_note("Макрос загружен")

    # ------------------------------------------------------------------
    # Клавиатура: поток слушателя только кладёт события в очередь
    # ------------------------------------------------------------------
    def on_press(self, key):
        try:
            self.hotkey_down(key_id(key), key_name(key), key)
        except Exception as exc:
            print("Ошибка обработки клавиши:", exc)

    def on_release(self, key):
        try:
            self.hotkey_up(key_id(key), key)
        except Exception as exc:
            print("Ошибка обработки клавиши:", exc)

    def hotkey_down(self, kid, name, key=None):
        """Нажатие клавиши клавиатуры или кнопки мыши (из потоков-слушателей)."""
        if kid in self.held:  # игнорируем автоповтор при удержании
            return
        self.held.add(kid)
        if key is not None:
            self.record_key(key, kid, True)
        # режим удержания: включаем клики сразу, не дожидаясь окна
        if (self.hold_mode and not self.binding and not self.recording
                and not self.playing and kid == self.hotkeys["click"][0]):
            self.clicking = True
        self.events.put(("key", kid, name))

    def hotkey_up(self, kid, key=None):
        if key is not None:
            self.record_key(key, kid, False)
        self.held.discard(kid)
        if self.hold_mode and kid == self.hotkeys["click"][0]:
            self.clicking = False  # отпустил: клики сразу прекращаются
            self.events.put(("release", kid))

    def hotkey_ids(self):
        return {value[0] for value in self.hotkeys.values()}

    # --- Кнопки мыши как горячие клавиши -------------------------------
    # Глобальный перехватчик мыши не используется: он пропускал бы через Python
    # каждое движение и каждый клик и тормозил бы игру. Вместо него раз в 8 мс
    # проверяется состояние только нужных кнопок, и только пока это нужно.
    def update_mouse_poll(self):
        if self.binding:
            self.mouse_wanted = tuple(MOUSE_VK)  # ждём любую из кнопок
        else:
            self.mouse_wanted = tuple(
                kid[1] for kid in self.hotkey_ids() if kid[0] == "mouse"
            )
        if self.mouse_wanted:
            self.mouse_poll_event.set()
        else:
            self.mouse_poll_event.clear()  # опрос спит, нагрузка 0

    def mouse_poll_loop(self):
        if user32 is None:
            return
        down = {name: False for name in MOUSE_VK}
        while self.running:
            self.mouse_poll_event.wait()
            if not self.running:
                return
            try:
                for name in self.mouse_wanted:
                    pressed = bool(user32.GetAsyncKeyState(MOUSE_VK[name]) & 0x8000)
                    if pressed != down[name]:
                        down[name] = pressed
                        kid = ("mouse", name)
                        if pressed:
                            self.hotkey_down(kid, MOUSE_NAMES[name])
                        else:
                            self.hotkey_up(kid)
            except Exception as exc:
                print("Ошибка опроса кнопок мыши:", exc)
            time.sleep(MOUSE_POLL_S)

    # ------------------------------------------------------------------
    # Главный поток забирает события из очереди
    # ------------------------------------------------------------------
    def poll_events(self):
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] == "play_done":
                    self.refresh_labels()
                elif item[0] == "release":
                    self.handle_release(item[1])
                else:
                    self.handle_key(item[1], item[2])
        except queue.Empty:
            pass
        if self.running:
            self.after(POLL_MS, self.poll_events)

    def start_binding(self, action):
        self.clicking = False
        self.playing = False
        self.play_stop_event.set()
        self.stop_recording()
        self.binding = action
        self.update_mouse_poll()
        self.refresh_labels()

    def handle_key(self, kid, name):
        if name != self.last_name:
            self.last_name = name
            self.last_key.configure(text=f"LAST INPUT  /  {name}")
        if self.binding:
            action = self.binding
            self.binding = None
            if kid != ESC_ID:
                taken = [a for a, (k, _) in self.hotkeys.items()
                         if k == kid and a != action]
                if taken:
                    self.last_key.configure(text=f"HOTKEY CONFLICT  /  {name}")
                else:
                    self.hotkeys[action] = (kid, name)
            self.update_mouse_poll()
            self.refresh_labels()
            return
        for action, (hotkey_id, _) in self.hotkeys.items():
            if kid == hotkey_id:
                if action == "click":
                    if self.hold_mode:
                        self.set_clicking(True)
                    else:
                        self.toggle_click()
                elif action == "record":
                    self.toggle_record()
                elif action == "play":
                    self.toggle_play()
                break

    def handle_release(self, kid):
        if self.hold_mode and kid == self.hotkeys["click"][0]:
            self.set_clicking(False)

    def on_close(self):
        self.running = False
        self.clicking = False
        self.click_event.set()  # будим потоки, чтобы они завершились
        self.mouse_poll_event.set()
        self.recording = False
        self.playing = False
        self.play_stop_event.set()
        self.listener.stop()
        self.stop_mouse_listener()
        # Workers are daemon threads, but give them a moment to release any
        # held macro inputs before the application window disappears.
        if self.play_thread is not None and self.play_thread.is_alive():
            self.play_thread.join(timeout=0.15)
        self.destroy()


if __name__ == "__main__":
    AutoClicker().mainloop()
