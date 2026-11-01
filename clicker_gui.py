import math
import queue
import threading
import time
import customtkinter as ctk
from pynput.mouse import Button, Controller
from pynput.keyboard import Key, KeyCode, Listener

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

mouse = Controller()
ESC_ID = ("key", Key.esc)

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


class AutoClicker(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Автокликер")
        self.geometry("340x560")
        self.resizable(False, False)

        self.clicking = False
        self.running = True
        self.binding = False
        self.unlimited = False
        self.cps = 10.0
        self.button = Button.left
        self.hotkey_id = ("key", Key.f6)
        self.hotkey_name = "F6"
        self.held = set()
        self.events = queue.Queue()

        ctk.CTkLabel(
            self, text="Автокликер", font=ctk.CTkFont(size=26, weight="bold")
        ).pack(pady=(24, 4))

        self.status = ctk.CTkLabel(
            self, text="Остановлен", text_color="#e5534b",
            font=ctk.CTkFont(size=16),
        )
        self.status.pack(pady=(0, 16))

        ctk.CTkLabel(self, text="Скорость (кликов в секунду)").pack()
        self.cps_entry = ctk.CTkEntry(self, width=140, justify="center")
        self.cps_entry.insert(0, "10")
        self.cps_entry.pack(pady=(4, 8))
        self.cps_entry.bind("<Return>", self.apply_cps)
        self.cps_entry.bind("<FocusOut>", self.apply_cps)

        self.unlimited_switch = ctk.CTkSwitch(
            self, text="Без ограничения скорости", command=self.on_unlimited
        )
        self.unlimited_switch.pack(pady=(0, 16))

        ctk.CTkLabel(self, text="Кнопка мыши").pack()
        self.btn_choice = ctk.CTkSegmentedButton(
            self, values=["Левая", "Правая"], command=self.on_button
        )
        self.btn_choice.set("Левая")
        self.btn_choice.pack(pady=(4, 16))

        ctk.CTkLabel(self, text="Горячая клавиша").pack()
        self.hotkey_btn = ctk.CTkButton(
            self, text="", height=34, fg_color="#3a3a3a",
            hover_color="#4a4a4a", command=self.start_binding,
        )
        self.hotkey_btn.pack(padx=30, pady=(4, 16), fill="x")

        self.toggle_btn = ctk.CTkButton(
            self, text="", height=44,
            font=ctk.CTkFont(size=16, weight="bold"), command=self.toggle,
        )
        self.toggle_btn.pack(padx=30, fill="x")

        self.hint = ctk.CTkLabel(self, text="", text_color="gray")
        self.hint.pack(pady=(14, 0))

        self.last_key = ctk.CTkLabel(
            self, text="Последняя нажатая клавиша: —", text_color="gray"
        )
        self.last_key.pack(pady=(6, 0))

        self.refresh_labels()

        threading.Thread(target=self.click_loop, daemon=True).start()
        self.listener = Listener(
            on_press=self.on_press, on_release=self.on_release
        )
        self.listener.start()
        self.after(20, self.poll_events)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def refresh_labels(self):
        name = self.hotkey_name
        if self.binding:
            self.hotkey_btn.configure(
                text="Нажми любую клавишу... (Esc — отмена)"
            )
        else:
            self.hotkey_btn.configure(text=f"{name}  (нажми, чтобы изменить)")
        action = "Стоп" if self.clicking else "Старт"
        self.toggle_btn.configure(text=f"{action} ({name})")
        self.hint.configure(text=f"Наведи курсор на цель и нажми {name}")

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

    def start_binding(self):
        if self.clicking:
            self.toggle()
        self.binding = True
        self.refresh_labels()

    def toggle(self):
        if not self.clicking:
            self.apply_cps()
        self.clicking = not self.clicking
        if self.clicking:
            self.status.configure(text="Работает", text_color="#3fb950")
        else:
            self.status.configure(text="Остановлен", text_color="#e5534b")
        self.refresh_labels()

    def click_loop(self):
        while self.running:
            try:
                if self.clicking:
                    mouse.click(self.button)
                    if self.unlimited:
                        time.sleep(0)  # без пауз, только отдаём процессор
                    else:
                        time.sleep(1 / self.cps)
                else:
                    time.sleep(0.02)
            except Exception as exc:
                print("Ошибка в цикле кликов:", exc)
                time.sleep(0.1)

    # --- Клавиатура: поток слушателя только кладёт события в очередь ---
    def on_press(self, key):
        try:
            kid = key_id(key)
            if kid in self.held:  # игнорируем автоповтор при удержании
                return
            self.held.add(kid)
            self.events.put((kid, key_name(key)))
        except Exception as exc:
            print("Ошибка обработки клавиши:", exc)

    def on_release(self, key):
        try:
            self.held.discard(key_id(key))
        except Exception as exc:
            print("Ошибка обработки клавиши:", exc)

    # --- Главный поток забирает события из очереди ---
    def poll_events(self):
        try:
            while True:
                kid, name = self.events.get_nowait()
                self.handle_key(kid, name)
        except queue.Empty:
            pass
        if self.running:
            self.after(20, self.poll_events)

    def handle_key(self, kid, name):
        self.last_key.configure(text=f"Последняя нажатая клавиша: {name}")
        if self.binding:
            self.binding = False
            if kid != ESC_ID:
                self.hotkey_id = kid
                self.hotkey_name = name
            self.refresh_labels()
            return
        if kid == self.hotkey_id:
            self.toggle()

    def on_close(self):
        self.running = False
        self.listener.stop()
        self.destroy()


if __name__ == "__main__":
    AutoClicker().mainloop()