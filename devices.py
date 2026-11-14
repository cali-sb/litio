"""Lectura de batería y búsqueda de dispositivos.

Cada lector tiene `read()`, que devuelve (nivel, cargando) o None si está apagado. El nivel
puede ser None si está encendido pero no lo da en ese momento.

Regla de la búsqueda: solo se envían comandos a modelos identificados por su VID:PID. A un
aparato desconocido nunca se le manda nada, porque un comando de otro modelo podría cambiarle
algo. De esos solo se lee lo que Windows ya sabe de ellos.
"""
import ctypes
import json
import re
import time
import uuid
from ctypes import wintypes

import hid

import config

SONY_VID, SONY_PIDS = 0x054C, (0x0CE6, 0x0DF2)
LOGI_VID = 0x046D
LOGI_RECEIVERS = {0xC54D: True}  # PID: probado. Se añaden con `False` hasta poder probarlos.
LOGI_RECEIVERS.update({pid: False for pid in (0xC539, 0xC53A, 0xC53D, 0xC53F, 0xC541, 0xC545, 0xC547)})
# Ratones enchufados por cable para cargar: el receptor deja de verlos y salen con su propio PID.
LOGI_WIRED = {0xC09B: True}  # PRO X 2. PID: probado.
PERIPHERAL = re.compile(r"wireless|headset|headphone|mouse|keyboard|gamepad|controller|2\.4\s*ghz"
                        r"|receiver|dongle", re.I)
KIND_WORDS = (("headset", r"head|buds|casco|auric|arctis|airpods|\bwh-|\bwf-"),
              ("gamepad", r"controller|gamepad|dualsense|dualshock|joy"),
              ("mouse", r"mouse|rat[oó]n|mx master"),
              ("keyboard", r"keyboard|teclado|ducky|keychron"))


def guess_kind(name):
    return next((kind for kind, words in KIND_WORDS if re.search(words, name, re.I)), "other")


def load_protocols():
    return json.loads(config.resource("protocols.json").read_text(encoding="utf-8"))


class DevPropKey(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", wintypes.ULONG)]


def propkey(guid, pid):
    key = DevPropKey()
    key.fmtid[:] = list(int.from_bytes(bytes([b]), "little", signed=True)
                        for b in uuid.UUID(guid).bytes_le)
    key.pid = pid
    return key


KEY_CONNECTED = propkey("83DA6326-97A6-4088-9453-A1923F573B29", 15)
KEY_BATTERY = propkey("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)
KEY_NAME = propkey("B725F130-47EF-101A-A5F1-02608C9EEBAC", 10)
CM_FILTER_ENUMERATOR, CM_FILTER_PRESENT = 0x1, 0x100
cfgmgr = ctypes.windll.cfgmgr32


def device_ids(enumerator):
    flags = CM_FILTER_ENUMERATOR | CM_FILTER_PRESENT
    size = wintypes.ULONG()
    if cfgmgr.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), enumerator, flags):
        return []
    buffer = ctypes.create_unicode_buffer(size.value)
    if cfgmgr.CM_Get_Device_ID_ListW(enumerator, buffer, size, flags):
        return []
    return [item for item in buffer[:].split("\0") if item]


def get_property(devinst, key):
    prop_type, data = wintypes.ULONG(), (ctypes.c_ubyte * 8)()
    size = wintypes.ULONG(ctypes.sizeof(data))
    if cfgmgr.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(prop_type),
                                       data, ctypes.byref(size), 0):
        return None
    return data[0]


def get_text_property(devinst, key):
    prop_type, data = wintypes.ULONG(), ctypes.create_unicode_buffer(256)
    size = wintypes.ULONG(ctypes.sizeof(data))
    if cfgmgr.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(prop_type),
                                       data, ctypes.byref(size), 0):
        return None
    return data.value


def bluetooth_nodes():
    """Devuelve {MAC: nodo} de lo emparejado con Windows."""
    nodes = {}
    for enumerator in ("BTHLE", "BTHENUM"):
        for instance in device_ids(enumerator):
            upper = instance.upper()
            if "\\DEV_" not in upper:
                continue
            devinst = wintypes.DWORD()
            if not cfgmgr.CM_Locate_DevNodeW(ctypes.byref(devinst), instance, 0):
                nodes.setdefault(upper.split("\\DEV_")[1][:12], devinst)
    return nodes


def read_bluetooth_node(devinst):
    if not get_property(devinst, KEY_CONNECTED):
        return None
    return get_property(devinst, KEY_BATTERY), False


class BluetoothReader:
    """Propiedad de batería que Windows mantiene para lo que publica el estándar BLE."""

    interval = retry = 5
    needs_activity = False
    grace = 0  # Segundos sin respuesta antes de darlo por apagado.

    def __init__(self, device):
        self.mac = device["ref"]

    def read(self):
        node = bluetooth_nodes().get(self.mac)
        return read_bluetooth_node(node) if node else None


class LogitechReader:
    """HID++ 2.0 por un receptor LIGHTSPEED: Unified Battery, o Battery Status en modelos antiguos.

    Solo contesta mientras el ratón se usa; por eso exige actividad en el PC.
    Enchufado por cable para cargar, el receptor deja de verlo: se le pregunta
    directamente por el cable (índice 0xFF), y ahí contesta siempre.
    """

    interval, retry = 10, 5
    needs_activity = True
    # Si aun así se pierde alguna respuesta, no desaparece de la bandeja: solo
    # se da por apagado tras 2 minutos sin contestar.
    grace = 120
    RECEIVER, WIRED = 1, 0xFF  # Índice HID++ del ratón según la vía.
    FEATURES = ((0x1004, 1, 2), (0x1000, 0, 2))  # (función, consulta, byte de estado)

    def __init__(self, device=None):
        self.pid = int(device["ref"], 16) if device else None
        self.feature = None
        self.sw = 0
        self.device = self.RECEIVER
        self.via = None

    def _call(self, dev, idx, fn, params=b"", timeout=2.0):
        # Despierto contesta en menos de 10 ms; dormido, el receptor tarda en
        # despertarlo y se han medido hasta 1,5 s.
        # Cada petición lleva su propio id: una respuesta atrasada de otra
        # petición llegó a leerse como batería (7 % en vez de 57 %).
        self.sw = self.sw % 15 + 1
        expected = (fn << 4) | self.sw
        dev.write((bytes([0x11, self.device, idx, expected]) + params).ljust(20, b"\0"))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            raw = dev.read(20)
            if not raw:
                time.sleep(0.003)
                continue
            r = bytes(raw)
            if r[1] == self.device and r[2] == 0xFF and r[3] == idx and r[4] == expected:
                return None
            if r[1] == self.device and r[2] == idx and r[3] == expected:
                return r[4:]
        return None

    def _find_feature(self, dev):
        for feature, query, status in self.FEATURES:
            reply = self._call(dev, 0, 0, feature.to_bytes(2, "big"))
            if reply and reply[0]:
                return reply[0], query, status
        return None

    @staticmethod
    def _path(pid):
        return next((d["path"] for d in hid.enumerate(LOGI_VID, pid)
                     if d["usage_page"] == 0xFF00 and d["usage"] == 2), None)

    def read(self):
        path, via = self._path(self.pid), self.pid
        if path is None:
            path, via = next(((p, pid) for pid in LOGI_WIRED if (p := self._path(pid))), (None, None))
        if path is None:
            return None
        if via != self.via:  # Ha cambiado de receptor a cable o al revés.
            self.via, self.feature = via, None
            self.device = self.RECEIVER if via == self.pid else self.WIRED
            self.needs_activity = via == self.pid
        dev = hid.device()
        dev.open_path(path)
        try:
            dev.set_nonblocking(True)
            self.feature = self.feature or self._find_feature(dev)
            if not self.feature:
                return None
            index, query, status = self.feature
            reply = self._call(dev, index, query)
            if not reply or not 0 <= reply[0] <= 100:
                self.feature = None
                return None
            return reply[0], reply[status] in (1, 2, 3, 4)
        finally:
            dev.close()


class DualsenseReader:
    interval = retry = 5
    needs_activity = False
    grace = 0

    def __init__(self, device=None):
        pass

    def read(self):
        """Con el mando por USB, Windows sigue listando su interfaz Bluetooth, que ya
        no envía nada: hay que probarlas todas, no quedarse con la primera."""
        interfaces = [d for d in hid.enumerate(SONY_VID) if d["product_id"] in SONY_PIDS]
        interfaces.sort(key=lambda d: d.get("bus_type") != 1)  # USB primero.
        for interface in interfaces:
            reading = self._read_interface(interface["path"])
            if reading:
                return reading
        return None

    @staticmethod
    def _read_interface(path):
        dev = hid.device()
        try:
            dev.open_path(path)
        except OSError:
            return None
        try:
            asked = False
            empty = 0
            for _ in range(20):
                r = dev.read(100, 150)
                if not r:
                    empty += 1
                    if empty >= 3:
                        return None
                    continue
                if r[0] == 0x31 and len(r) >= 78:
                    status = r[54]
                elif r[0] == 0x01 and len(r) >= 64:
                    status = r[53]
                elif r[0] == 0x01 and not asked:
                    # En Bluetooth arranca en modo reducido, sin batería: pedir la
                    # calibración lo pasa al informe completo 0x31.
                    dev.get_feature_report(0x05, 41)
                    asked = True
                    continue
                else:
                    continue
                charge, mode = status & 0x0F, status >> 4
                if mode == 2:
                    return 100, True
                if mode in (0, 1) and charge <= 10:
                    return min(charge * 10 + 5, 100), mode == 1
            return None
        finally:
            dev.close()


class ProtocolReader:
    """Modelo descrito en protocols.json: consultas de pregunta y respuesta por HID."""

    interval = retry = 5
    needs_activity = False
    grace = 0

    def __init__(self, device):
        self.model = load_protocols()[device["ref"]]

    @staticmethod
    def find_path(model):
        pids = {int(pid, 16) for pid in model["pids"]}
        return next((d["path"] for d in hid.enumerate(int(model["vid"], 16))
                     if d["product_id"] in pids and d["usage_page"] == int(model["usage_page"], 16)),
                    None)

    def _query(self, dev, step):
        dev.write(bytes.fromhex(step["send"]).ljust(self.model["size"], b"\0"))
        reply = list(bytes.fromhex(step["reply"]))
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            r = dev.read(self.model["size"], 300)
            if r[:len(reply)] == reply:  # Algunos también envían avisos sueltos.
                return r[step["byte"]]
        return None

    def read(self):
        model = self.model
        path = self.find_path(model)
        if path is None:
            return None
        dev = hid.device()
        try:
            dev.open_path(path)
        except OSError:
            return None
        try:
            connected = self._query(dev, model["online"])
            if connected is None or connected in model["online"]["off"]:
                return None
            charging = self._query(dev, model["charging"]) in model["charging"]["on"]
            if charging and not model.get("level_while_charging"):
                return None, True
            level = self._query(dev, model["level"])
            return (level, charging) if level is not None and level <= 100 else None
        finally:
            dev.close()


READERS = {"bluetooth": BluetoothReader, "logitech": LogitechReader,
           "dualsense": DualsenseReader, "protocol": ProtocolReader}


def candidate(name, driver=None, ref="", kind=None, reading=None, note="", supported=True):
    level, charging = reading or (None, False)
    return {"name": name, "driver": driver or "", "ref": ref, "kind": kind or guess_kind(name),
            "level": -1 if level is None else level, "charging": charging,
            "online": reading is not None, "note": note, "supported": supported}


def safe_read(reader):
    try:
        return reader.read()
    except (OSError, ValueError):
        return None


def find_usb(hid_devices):
    found, known = [], set()
    if any(d["vendor_id"] == SONY_VID and d["product_id"] in SONY_PIDS for d in hid_devices):
        found.append(candidate("DualSense", "dualsense", "dualsense", "gamepad",
                               safe_read(DualsenseReader())))
        known.update((SONY_VID, pid) for pid in SONY_PIDS)
    for pid, tested in LOGI_RECEIVERS.items():
        known.add((LOGI_VID, pid))
        if any(d["vendor_id"] == LOGI_VID and d["product_id"] == pid for d in hid_devices):
            reader = LogitechReader({"ref": f"{pid:04X}"})
            found.append(candidate("Ratón Logitech", "logitech", f"{pid:04X}", "mouse", safe_read(reader),
                                   "Muévelo para que conteste." if tested
                                   else "Receptor sin probar. Muévelo para que conteste."))
    known.update((LOGI_VID, pid) for pid in LOGI_WIRED)  # Se leen como el ratón de su receptor.
    for ref, model in load_protocols().items():
        vid = int(model["vid"], 16)
        known.update((vid, int(pid, 16)) for pid in model["pids"])
        if ProtocolReader.find_path(model):
            found.append(candidate(model["name"], "protocol", ref, model["kind"],
                                   safe_read(ProtocolReader({"ref": ref})),
                                   "" if model.get("tested") else "Modelo sin probar."))
    return found, known


def find_unsupported(hid_devices, known):
    seen, found = set(), []
    for d in hid_devices:
        ids = (d["vendor_id"], d["product_id"])
        name = d["product_string"] or ""
        if ids in known or ids in seen or not PERIPHERAL.search(name):
            continue
        seen.add(ids)
        found.append(candidate(name, note=f"Sin protocolo conocido ({ids[0]:04X}:{ids[1]:04X}).",
                               supported=False))
    return found


def find_bluetooth(hid_names):
    found = []
    for mac, node in bluetooth_nodes().items():
        name = get_text_property(node, KEY_NAME) or f"Bluetooth {mac}"
        reading = read_bluetooth_node(node)
        has_battery = get_property(node, KEY_BATTERY) is not None
        if name in hid_names and not has_battery:
            continue  # Ya sale como dispositivo USB/HID.
        if has_battery or reading is None:
            note = f"…{mac[-4:]}" + ("" if has_battery else ". Conéctalo para ver si da batería.")
            found.append(candidate(name, "bluetooth", mac, reading=reading, note=note))
        else:
            found.append(candidate(name, note=f"…{mac[-4:]}. Conectado, pero no publica batería.",
                                   supported=False))
    return found


def discover():
    """Lista lo que hay en el PC, compatible o no."""
    hid_devices = hid.enumerate()
    usb, known = find_usb(hid_devices)
    names = {d["product_string"] for d in hid_devices if (d["vendor_id"], d["product_id"]) in known}
    bluetooth = find_bluetooth(names)
    # Un mando Bluetooth sale también como HID: se queda la entrada de Bluetooth.
    seen = {c["name"] for c in bluetooth}
    others = [d for d in hid_devices if d["product_string"] not in seen]
    return usb + bluetooth + find_unsupported(others, known)
