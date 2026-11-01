import time
import threading
from pynput.mouse import Button, Controller
from pynput.keyboard import Key, Listener

CLICK_DELAY = 0.1  # пауза между кликами в секундах

mouse = Controller()
clicking = False
running = True


def click_loop():
    while running:
        if clicking:
            mouse.click(Button.left)
        time.sleep(CLICK_DELAY)


def on_press(key):
    global clicking, running
    if key == Key.f6:
        clicking = not clicking
        print("Клики:", "ВКЛ" if clicking else "ВЫКЛ")
    elif key == Key.esc:
        running = False
        return False


threading.Thread(target=click_loop, daemon=True).start()

with Listener(on_press=on_press) as listener:
    print("F6 — включить/выключить, Esc — выйти")
    listener.join()