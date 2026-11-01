import threading
import time
import customtkinter as ctk
from pynput.mouse import Button, Controller
from pynput.keyboard import Key, Listener

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

mouse = Controller()


class AutoClicker(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Автокликер")
        self.geometry("340x430")
        self.resizable(False, False)

        self.clicking = False
        self.running = True
        self.cps = 10
        self.button = Button.left

        ctk.CTkLabel(
            self, text="Автокликер", font=ctk.CTkFont(size=26, weight="bold")
        ).pack(pady=(24, 4))

        self.status = ctk.CTkLabel(
            self, text="Остановлен", text_color="#e5534b",
            font=ctk.CTkFont(size=16),
        )
        self.status.pack(pady=(0, 16))

        self.cps_label = ctk.CTkLabel(self, text="Скорость: 10 кликов/сек")
        self.cps_label.pack()

        self.slider = ctk.CTkSlider(
            self, from_=1, to=50, number_of_steps=49, command=self.on_slider
        )
        self.slider.set(10)
        self.slider.pack(padx=30, pady=(4, 16), fill="x")

        ctk.CTkLabel(self, text="Кнопка мыши").pack()
        self.btn_choice = ctk.CTkSegmentedButton(
            self, values=["Левая", "Правая"], command=self.on_button
        )
        self.btn_choice.set("Левая")
        self.btn_choice.pack(pady=(4, 20))

        self.toggle_btn = ctk.CTkButton(
            self, text="Старт (F6)", height=44,
            font=ctk.CTkFont(size=16, weight="bold"), command=self.toggle,
        )
        self.toggle_btn.pack(padx=30, fill="x")

        ctk.CTkLabel(
            self, text="Наведи курсор на цель и нажми F6", text_color="gray"
        ).pack(pady=(14, 0))

        threading.Thread(target=self.click_loop, daemon=True).start()
        self.listener = Listener(on_press=self.on_key)
        self.listener.start()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_slider(self, value):
        self.cps = int(value)
        self.cps_label.configure(text=f"Скорость: {self.cps} кликов/сек")

    def on_button(self, value):
        self.button = Button.left if value == "Левая" else Button.right

    def toggle(self):
        self.clicking = not self.clicking
        if self.clicking:
            self.status.configure(text="Работает", text_color="#3fb950")
            self.toggle_btn.configure(text="Стоп (F6)")
        else:
            self.status.configure(text="Остановлен", text_color="#e5534b")
            self.toggle_btn.configure(text="Старт (F6)")

    def click_loop(self):
        while self.running:
            if self.clicking:
                mouse.click(self.button)
            time.sleep(1 / self.cps)

    def on_key(self, key):
        if key == Key.f6:
            self.after(0, self.toggle)

    def on_close(self):
        self.running = False
        self.listener.stop()
        self.destroy()


if __name__ == "__main__":
    AutoClicker().mainloop()
