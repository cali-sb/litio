"""Programa de fondo: lee la batería y la muestra en la bandeja de Windows.

Qué dispositivos lee, cómo se ven y si publica la API para el Stream Deck sale de
settings.json, que edita la ventana de ajustes (settings_app.py, con --settings). La ventana va
en un proceso aparte para no cargar Qt en lo que corre siempre.

La API, si está activa, es http://127.0.0.1:47631/battery.json.
"""
import ctypes
import json
import logging
import logging.handlers
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pystray
from PIL import Image, ImageColor, ImageDraw, ImageFont
from pystray._util import serialized_image, win32

import brand
import config
import updater
from devices import READERS

PORT = 47631
SM_CXSMICON = 49
IDLE = "general"  # Icono que lleva el menú cuando no hay ningún dispositivo que mostrar.
LOW_FILL = (225, 40, 40, 255)
LOW_TEXT = (255, 255, 255, 255)
CHARGING_INK = (255, 216, 74, 255)  # El amarillo del rayo de la tecla del Stream Deck.

log = logging.getLogger("battery-tray")
lock = threading.Lock()
state = {}
changes = threading.Condition()  # Avisa de cada cambio en state; version los cuenta.
version = 0
settings = config.load()
quit_event = threading.Event()
api_server = None
updates = updater.Updater(auto=lambda: settings["auto_update"], on_install=quit_event.set)


def load_state():
    try:
        saved = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for value in saved.values():
        value["online"] = False
    state.update(saved)


def write_state():
    global version
    with lock:
        snapshot = json.dumps(state, ensure_ascii=False)
    tmp = config.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(snapshot, encoding="utf-8")
    tmp.replace(config.STATE_FILE)
    with changes:  # Despierta al Stream Deck, que espera a que algo cambie.
        version += 1
        changes.notify_all()


def sync_state():
    """Ajusta el estado a la lista de dispositivos, que puede haber cambiado."""
    with lock:
        wanted = {d["id"]: d for d in settings["devices"]}
        for key in [k for k in state if k not in wanted]:
            del state[key]
        for key, device in wanted.items():
            entry = state.setdefault(key, {"level": None, "charging": False, "online": False,
                                           "updated": None})
            entry.update(name=device["name"], kind=device.get("kind", "other"))


def update(key, level=None, charging=False, online=True):
    """Guarda una lectura; sin nivel nuevo conserva el último conocido."""
    new = {"online": online, "charging": charging}
    if level is not None:
        new["level"] = level
    with lock:
        current = state.get(key)
        if current is None:  # Lo han quitado mientras se leía.
            return
        if all(current.get(k) == v for k, v in new.items()):
            return  # Se lee a menudo; el disco solo se toca si algo cambia.
        current.update(new, updated=int(time.time()))
    write_state()


class LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_ms():
    info = LastInput(ctypes.sizeof(LastInput), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF


def worker():
    readers, due, answered = {}, {}, {}
    while True:
        now = time.monotonic()
        with lock:
            devices = list(settings["devices"])
        for device in devices:
            key = device["id"]
            try:
                if key not in readers:
                    readers[key] = READERS[device["driver"]](device)
                reader = readers[key]
                if now < due.get(key, 0) or (reader.needs_activity and idle_ms() >= 1500):
                    continue
                reading = reader.read()
                # Mientras carga o no contesta se repite antes, para notar que vuelve a gastar.
                pause = reader.interval if reading and not reading[1] else reader.retry
                due[key] = now + pause
                if reading:
                    answered[key] = now
                elif now - answered.get(key, -reader.grace) < reader.grace:
                    continue  # Se ha dormido hace poco: se deja como estaba.
                update(key, *(reading or (None, False)), online=reading is not None)
            except Exception:
                due[key] = now + 60
                log.exception("fallo al leer %s", key)
        for key in set(readers) - {d["id"] for d in devices}:
            del readers[key]
            answered.pop(key, None)
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    """Solo para el plugin del Stream Deck.

    Una web abierta en el navegador que intente leerla manda siempre la cabecera Origin;
    el plugin, no. Y Host tiene que ser esta dirección: así tampoco sirve hacer que un
    dominio apunte a 127.0.0.1 (DNS rebinding), donde el navegador ya no mandaría Origin.

    Con ?since=N espera a que el estado deje de ser la versión N, hasta WAIT segundos.
    El Stream Deck frena los temporizadores del plugin a uno por minuto cuando lleva un
    rato en segundo plano, pero no las respuestas de red: así se entera al momento.
    """

    WAIT = 25
    rejected = set()  # Para avisar en el registro una vez por origen, no en cada petición.

    def do_GET(self):
        host, origin = self.headers.get("Host"), self.headers.get("Origin")
        if host != f"127.0.0.1:{PORT}" or origin is not None:
            if (host, origin) not in self.rejected:
                self.rejected.add((host, origin))
                log.warning("API: rechazada una petición de Host %s, Origin %s", host, origin)
            self.send_error(403)
            return
        path, _, query = self.path.partition("?")
        if path != "/battery.json":
            self.send_error(404)
            return
        if query.startswith("since="):
            since = query[len("since="):]
            with changes:
                changes.wait_for(lambda: str(version) != since, timeout=self.WAIT)
                current = version
            with lock:
                body = json.dumps({"version": current, "devices": state}, ensure_ascii=False).encode()
        else:  # Plugins anteriores a la 2.4.0.
            with lock:
                body = json.dumps(state, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def set_api(enabled):
    """Enciende o apaga la API que usa el plugin del Stream Deck."""
    global api_server
    if enabled and api_server is None:
        try:
            api_server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        except OSError:
            log.warning("el puerto %s está ocupado: el Stream Deck no recibirá datos", PORT)
            return
        threading.Thread(target=api_server.serve_forever, daemon=True).start()
    elif not enabled and api_server is not None:
        api_server.shutdown()
        api_server.server_close()
        api_server = None


def colour(level):
    if level is None:
        return (150, 150, 150, 255)
    if level >= 30:
        return (70, 210, 110, 255)
    return (245, 170, 40, 255) if level >= 15 else (240, 70, 60, 255)


def light_taskbar():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def glyph(kind, size, ink):
    """Dibuja el dispositivo en una rejilla de 24 x 12, a 8 aumentos para suavizarlo."""
    u = size * 8 / 24
    image = Image.new("RGBA", (size * 8, size * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    clear = (0, 0, 0, 0)

    def box(*values):
        return [v * u for v in values]

    if kind == "mouse":
        d.rounded_rectangle(box(7, 0, 17, 12), radius=4.8 * u, fill=ink)
        d.rectangle(box(11, 2, 13, 5.5), fill=clear)
    elif kind == "headset":
        d.arc(box(3.5, 0, 20.5, 17), 180, 360, fill=ink, width=round(2.4 * u))
        d.rounded_rectangle(box(2, 6.5, 8, 12), radius=1.5 * u, fill=ink)
        d.rounded_rectangle(box(16, 6.5, 22, 12), radius=1.5 * u, fill=ink)
    elif kind == "keyboard":
        d.rounded_rectangle(box(0.5, 0.5, 23.5, 11.5), radius=2 * u, fill=ink)
        for x in (3, 7.5, 11.1, 14.7, 19.2):
            d.rectangle(box(x, 3, x + 1.8, 4.8), fill=clear)
        d.rectangle(box(6, 7.5, 18, 9), fill=clear)
    elif kind == "gamepad":
        d.rounded_rectangle(box(4, 0.5, 20, 8.5), radius=3.5 * u, fill=ink)
        for top, bottom in (((6, 4), (3.8, 9.8)), ((18, 4), (20.2, 9.8))):
            d.line(box(*top, *bottom), fill=ink, width=round(4.2 * u))
            d.ellipse(box(bottom[0] - 2.1, bottom[1] - 2.1, bottom[0] + 2.1, bottom[1] + 2.1), fill=ink)
        d.rectangle(box(6, 3.7, 9.6, 5.3), fill=clear)
        d.rectangle(box(7, 2.7, 8.6, 6.3), fill=clear)
        d.ellipse(box(14.2, 2.8, 16.6, 5.2), fill=clear)
        d.ellipse(box(17, 4.4, 19.4, 6.8), fill=clear)
    else:
        d.rounded_rectangle(box(3, 1.5, 19.5, 10.5), radius=2 * u, outline=ink, width=round(1.8 * u))
        d.rectangle(box(20.2, 4, 21.8, 8), fill=ink)
    return image.resize((size, size // 2), Image.LANCZOS)


def neutral_ink(light):
    return (40, 40, 45, 255) if light else (235, 235, 240, 255)


def idle_image(size, light):
    """El rayo de Litio, para el icono que solo sale si no hay dispositivos que mostrar."""
    return brand.bolt_image(size, neutral_ink(light))


def device_colour(device):
    try:
        return ImageColor.getcolor(device.get("color", ""), "RGBA")
    except ValueError:
        return None


def tray_image(device, level, size, light, low_level, charging=False):
    """Icono de un dispositivo: su dibujo arriba y el número debajo.

    Mientras carga, un rayo amarillo sustituye al dibujo, como en el Stream Deck.
    Con color fijo el color no avisa de nada, así que por debajo de `low_level`
    el número va sobre un fondo rojo.
    """
    fixed = device_colour(device) if device.get("color_mode") == "fixed" else None
    tinted = device_colour(device) if device.get("tint_glyph") else None
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if charging:
        image.alpha_composite(brand.bolt_image(size // 2, CHARGING_INK), (size // 4, 0))
    else:
        image.alpha_composite(glyph(device.get("kind", "other"), size, tinted or neutral_ink(light)))
    draw = ImageDraw.Draw(image)
    text = "--" if level is None else str(level)
    points = round(size * 0.66)
    while draw.textlength(text, font=ImageFont.truetype("arialbd.ttf", points)) > size:
        points -= 1
    font = ImageFont.truetype("arialbd.ttf", points)
    if fixed and level is not None and level < low_level and not charging:
        draw.rounded_rectangle((0, size // 2, size - 1, size - 1), radius=size // 8, fill=LOW_FILL)
        fixed = LOW_TEXT
    draw.text((size / 2, size - (1 if fixed == LOW_TEXT else 0)), text, font=font, anchor="ms",
              fill=fixed or colour(level))
    return image


class TrayIcon(pystray.Icon):
    """Icono dibujado al tamaño real de la bandeja.

    pystray lo carga al tamaño grande del sistema, que se ve borroso al reducirse. Esto
    toca métodos internos de pystray: por eso su versión está fijada en requirements.txt.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closing = threading.Event()

    def _show(self):
        self._release_icon()  # pystray reutilizaría la imagen de antes de esconderse.
        super()._show()

    def _assert_icon_handle(self):
        if self._icon_handle:
            return
        size = self.icon.width
        with serialized_image(self.icon.copy(), "ICO") as path:
            self._icon_handle = win32.LoadImage(None, path, win32.IMAGE_ICON, size, size,
                                                win32.LR_LOADFROMFILE)


def snapshot():
    """Devuelve la lista de (ajustes, estado) y el aviso de batería baja."""
    with lock:
        pairs = [(dict(d), dict(state[d["id"]])) for d in settings["devices"] if d["id"] in state]
        return pairs, settings["low_level"]


def line(status):
    if status["level"] is None:
        return f"{status['name']}: sin datos"
    bolt = " (cargando)" if status["charging"] else ""
    off = "" if status["online"] else " (último dato)"
    return f"{status['name']}: {status['level']} %{bolt}{off}"


def tray_loop(icon, key, size, light):
    """Muestra el icono si el dispositivo está elegido y encendido, también mientras carga."""
    shown = None
    while not icon.closing.is_set():
        pairs, low_level = snapshot()
        active = frozenset(d["id"] for d, s in pairs
                           if d.get("tray", True) and s["online"])
        mine = next(((d, s) for d, s in pairs if d["id"] == key), None)
        current = (active, low_level, json.dumps(mine, sort_keys=True, default=str) if mine else "",
                   tuple(line(s) + str(d.get("tray", True)) for d, s in pairs))
        if current != shown:
            shown = current
            if mine:
                icon.icon = tray_image(mine[0], mine[1]["level"], size, light, low_level,
                                       mine[1]["charging"] and mine[1]["online"])
                icon.title = line(mine[1])[:127]
            icon.update_menu()
            icon.visible = key in active or (key == IDLE and not active)
        icon.closing.wait(1)
    icon.visible = False


def toggle_tray(key):
    saved = config.load()
    for device in saved["devices"]:
        if device["id"] == key:
            device["tray"] = not device.get("tray", True)
    config.save(saved)


def open_settings(*_):
    if getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable, "--settings"])
    else:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--settings"])


def toggler(key):
    return lambda icon, item: toggle_tray(key)


def menu_items():
    pairs, _ = snapshot()
    items = [pystray.MenuItem(line(s), toggler(d["id"]), checked=lambda item, on=d.get("tray", True): on)
             for d, s in pairs]
    if updates.available and updater.installed():
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem(f"Actualizar a la {updates.available['version']}",
                                   lambda icon, _: updates.ask("install"))]
    return [*items, pystray.Menu.SEPARATOR, pystray.MenuItem("Ajustes…", open_settings),
            pystray.MenuItem("Salir", lambda icon, _: quit_event.set())]


def start_icon(key, size, light):
    icon = TrayIcon(f"battery-tray-{key}", idle_image(size, light),
                    "Litio: ningún dispositivo en uso", pystray.Menu(menu_items))
    loop = threading.Thread(target=tray_loop, args=(icon, key, size, light), daemon=True)
    thread = threading.Thread(target=icon.run, args=(lambda i: loop.start(),))
    thread.start()
    return icon, thread


def stop_icon(icon, thread):
    icon.closing.set()
    time.sleep(0.05)
    icon.stop()
    thread.join(3)


def apply_settings(icons, size, light):
    """Aplica settings.json: iconos que entran o salen, y API encendida o apagada."""
    global settings
    fresh = config.load()
    with lock:
        settings = fresh
    sync_state()
    write_state()
    set_api(fresh["streamdeck"])
    wanted = {d["id"] for d in fresh["devices"]}
    for key in [k for k in icons if k != IDLE and k not in wanted]:
        stop_icon(*icons.pop(key))
    for key in wanted - icons.keys():
        icons[key] = start_icon(key, size, light)


def promote_tray():
    """Saca los iconos del desplegable «^», donde Windows mete los de un programa nuevo.

    Windows guarda una entrada por ejecutable. Solo se marca si nadie la ha tocado: si el
    usuario lo ocultó, IsPromoted ya existe y se respeta. Devuelve True si ya está resuelto.
    """
    tails = {tuple(part.lower() for part in Path(exe).parts[-2:])
             for exe in (sys.executable, getattr(sys, "_base_executable", sys.executable))}
    done = False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings") as parent:
        for index in range(winreg.QueryInfoKey(parent)[0]):
            name = winreg.EnumKey(parent, index)
            with winreg.OpenKey(parent, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as entry:
                try:
                    path = winreg.QueryValueEx(entry, "ExecutablePath")[0]
                except OSError:
                    continue
                # La ruta puede empezar por un GUID de carpeta conocida: se compara el final.
                if tuple(part.lower() for part in Path(path).parts[-2:]) not in tails:
                    continue
                done = True
                try:
                    winreg.QueryValueEx(entry, "IsPromoted")
                except OSError:
                    winreg.SetValueEx(entry, "IsPromoted", 0, winreg.REG_DWORD, 1)
    return done


def run():
    if not config.single_instance(config.TRAY_MUTEX):
        return
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(config.DATA_DIR / "battery-tray.log",
                                                   maxBytes=200_000, backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Sin esto, Windows reescala los iconos.
    load_state()
    size = ctypes.windll.user32.GetSystemMetrics(SM_CXSMICON)
    light = light_taskbar()
    icons = {IDLE: start_icon(IDLE, size, light)}
    stamp = config.modified()
    apply_settings(icons, size, light)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=updates.run, daemon=True).start()
    if not settings["devices"]:
        open_settings()
    promoted = False
    while not quit_event.wait(1.5):
        updates.poll()
        promoted = promoted or promote_tray()
        if config.modified() != stamp:
            stamp = config.modified()
            apply_settings(icons, size, light)
    for icon, thread in icons.values():
        stop_icon(icon, thread)


def main():
    if "--settings" in sys.argv:
        import settings_app
        settings_app.main()
    else:
        run()


if __name__ == "__main__":
    main()
