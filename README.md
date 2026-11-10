# Litio

Batería de ratones, cascos, mandos y teclados inalámbricos en Windows: iconos en la bandeja y teclas del Stream Deck, sin los programas de cada fabricante.

Son tres piezas: un programa de fondo ligero (`battery_tray.py`) que lee los dispositivos, una ventana de ajustes en Qt (`settings_app.py`) que solo se carga al abrirla y un plugin de Stream Deck opcional.

## Uso

- **Instalar:** el `Litio-X.Y.Z-setup.exe` de la [última release](https://github.com/cali-sb/litio/releases/latest), sin permisos de administrador. El primer arranque abre los ajustes en la pestaña Buscar.
- **Actualizar:** es automático. El programa busca en las releases de GitHub un minuto después de arrancar y luego cada 6 horas. Si hay una versión nueva, la descarga, comprueba su SHA-256, la instala en silencio y vuelve a abrirse. En *General* se puede desactivar; entonces la versión nueva se instala con el botón de esa pestaña o desde el menú de la bandeja.
- **Bandeja:** un icono por dispositivo mientras está encendido; al cargar, un rayo amarillo sustituye al dibujo y el número sigue subiendo. Apagado, desaparece. Si no queda ninguno sale el rayo de Litio, que solo sirve para abrir el menú. Con clic derecho, las casillas de la lista eligen qué dispositivos pueden mostrarse, y «Ajustes…» abre la ventana.
- **Ajustes:**
  - *Dispositivos:* nombre, tipo, colores y quitar.
  - *Buscar:* lo que hay en el PC y si es compatible.
  - *General:* API del Stream Deck, aviso de batería baja y actualizaciones. Los cambios se aplican solos.
- **Colores:** cada dispositivo usa color fijo o según nivel (verde desde 30 %, ámbar desde 15 %, rojo por debajo). Con color fijo, por debajo del aviso el número pasa a blanco sobre rojo.
- **Stream Deck:** acción «Batería» en la categoría «Litio», con un desplegable para elegir dispositivo. El anillo marca el nivel, el rayo sustituye al icono mientras carga y el gris indica el último dato. Las teclas por dispositivo de la versión 1 siguen funcionando, aunque esas acciones ya no salen en la lista.
- **API local:** `http://127.0.0.1:47631/battery.json`, solo en este PC y solo para el Stream Deck: rechaza a las webs abiertas en el navegador. Se puede desactivar; sin ella las teclas muestran `--`.
- **Datos:** `%LOCALAPPDATA%\battery-tray` (`settings.json`, `state.json` y `battery-tray.log`). Desinstalar los conserva.
- **Nombre:** hasta la 2.1.0 se llamaba «Batería». La 2.2.0 se mueve sola a `Programs\Litio` y borra la carpeta y el menú antiguos. Los nombres internos (carpeta de datos, mutex, clave de arranque y UUID del plugin) siguen igual, para que las versiones viejas se actualicen encima y las teclas del Stream Deck no se pierdan.

## Compatibilidad

| Tipo | Cómo se lee | Cada cuánto | Notas |
|---|---|---|---|
| Bluetooth estándar | Propiedad de batería de Windows | 5 s | Sirve cualquier aparato que publique el servicio de batería BLE. La precisión es la que dé cada uno: los mandos Xbox dan el porcentaje de 1 en 1 (se han visto 88 y 83), y `0x2A19` leído directamente coincide |
| Ratones Logitech con receptor LIGHTSPEED | HID++ 2.0, funciones `0x1004` o `0x1000` | 10 s con actividad en el PC; si no contesta o carga, cada 5 s | Solo se pregunta con actividad en el PC. Dormido tarda hasta 1,5 s en contestar, así que se espera 2 s, y solo se da por apagado tras 2 minutos sin respuesta. Probado con PRO X 2 y receptor `C54D`; el resto de receptores y `0x1000` están sin probar |
| DualSense y DualSense Edge | Informe HID `0x31` (Bluetooth) o `0x01` (USB) | 5 s | Tramos de 10 %: el mando no da más precisión. Muestra el punto medio (85 es de 80 a 89). Probado con DualSense; Edge sin probar |
| Modelos de `protocols.json` | Consultas HID declaradas en el archivo | 5 s | HyperX Cloud Alpha Wireless probado; Alpha 2 Wireless sin probar. Porcentaje real |

La pestaña Buscar marca lo que no se ha probado. Solo se envían consultas a modelos identificados por VID:PID; a un aparato desconocido nunca se le manda nada.

## Añadir un modelo

Si el modelo responde a preguntas por HID, basta una entrada en `protocols.json`, sin tocar código:

```json
"mi_modelo": {
  "name": "Nombre visible",
  "kind": "headset",
  "tested": false,
  "vid": "03F0", "pids": ["098D"], "usage_page": "FF43", "size": 31,
  "online":   { "send": "21 BB 03", "reply": "21 BB 03", "byte": 3, "off": [1] },
  "charging": { "send": "21 BB 0C", "reply": "21 BB 0C", "byte": 3, "on": [1] },
  "level":    { "send": "21 BB 0B", "reply": "21 BB 0B", "byte": 3 }
}
```

- `send` son los bytes que se envían, y `reply`, cómo empieza la respuesta válida. `byte` es dónde está el dato.
- `off` y `on` son los valores de apagado y de cargando.
- `level_while_charging: true` si el nivel es fiable mientras carga.

De dónde sacar el protocolo, de más fácil a más difícil:

1. **Proyectos abiertos:** leer sus fuentes para copiar los bytes. [HeadsetControl](https://github.com/Sapd/HeadsetControl) (cascos de muchas marcas, ninguno Razer), [Solaar](https://github.com/pwr-Solaar/Solaar) (Logitech), [OpenRazer](https://github.com/openrazer/openrazer) (ratones y teclados Razer) y [rivalcfg](https://github.com/flozz/rivalcfg) (ratones SteelSeries). Conviene escribir el protocolo aquí en vez de incluir su código: HeadsetControl es GPL-3.
2. **Capturar el tráfico:** con Wireshark y USBPcap, mientras el programa del fabricante muestra la batería. Se repite con niveles distintos y se busca el byte que cambia.
3. **SDK del fabricante,** si lo ofrece. Obliga a tener instalado su programa, que es lo que este proyecto evita.

Si el aparato usa otro esquema, como los informes `feature` de Razer, hace falta una clase en `devices.py` y su entrada en `READERS`. No hay que marcar `tested: true` hasta probarlo con el aparato delante.

## Cosas que no son evidentes

- **HID++:** cada petición lleva su propio id y solo se acepta la respuesta con ese id. Sin eso, una respuesta atrasada de otra función se leyó como batería: el 7 de un falso «7 %» era la longitud del nombre «PRO X 2».
- **DualSense por USB:** Windows sigue listando su interfaz Bluetooth, que ya no envía nada. Hay que probar todas las interfaces, USB primero; con la primera de la lista, el valor se queda congelado.
- **DualSense por Bluetooth:** arranca en un modo reducido sin batería. Para leerla se pide el informe de calibración `0x05`, que lo pasa al modo completo hasta que se apague. Steam y los juegos con soporte nativo ya lo hacen, pero un juego antiguo que use el modo reducido podría ver mal los controles.
- **Iconos ocultos:** Windows mete en el desplegable «^» los iconos de un ejecutable nuevo. `promote_tray` pone `IsPromoted` en su entrada de `NotifyIconSettings` solo si no existe, para respetar lo que elija el usuario después. La entrada es por ejecutable, no por icono.
- **pystray:** `TrayIcon` dibuja al tamaño real de la bandeja y evita reutilizar la imagen de antes de esconderse. Toca métodos internos, así que la versión va fijada.
- **Una sola instancia:** la garantiza un mutex con nombre, no el puerto, porque la API se puede apagar. El instalador usa los mismos nombres en `AppMutex`.
- **API solo para el Stream Deck:** una web que intenta leerla manda siempre la cabecera `Origin` y el plugin no, así que se rechaza toda petición que la lleve. También se exige `Host: 127.0.0.1:47631`, porque si un dominio apunta a 127.0.0.1 (DNS rebinding) el navegador ya no manda `Origin`. Cada combinación rechazada se apunta una vez en el registro.
- **Teclas al momento:** el Stream Deck frena los temporizadores del plugin a uno por minuto cuando lleva un rato en segundo plano, así que preguntar cada segundo no funciona. El plugin pide `battery.json?since=N` y el programa no contesta hasta que el estado cambia o pasan 25 s; al llegar la respuesta, el plugin lanza la siguiente. Las respuestas de red no se frenan.
- **Actualizar sin carreras:** solo el programa de fondo busca e instala; la ventana lo lee de `update.json` y le pide cosas con `update-request`. El programa lanza el instalador y se cierra. Al ver `/update=1`, el instalador espera a que se suelten los mutex antes de su comprobación de `AppMutex`, que en silencio abortaría. Luego lo reabre a través del Explorador, para que no herede las variables de PyInstaller del proceso viejo.

## Desarrollo

Desde la carpeta del repo:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\pythonw.exe battery_tray.py              # Programa de fondo
.venv\Scripts\pythonw.exe battery_tray.py --settings   # Ventana
.\build.ps1                                            # Instalador en build\installer
```

`build.ps1` necesita Inno Setup 6. El `.exe` instalado y el de desarrollo no pueden correr a la vez. Desde el código fuente el programa avisa de versiones nuevas, pero no se instala.

### Publicar una versión

1. Subir el número en `version.py`, la única fuente: la leen el programa, el instalador y la etiqueta.
2. Hacer commit en `main`.
3. Ejecutar `.\release.ps1 -Notes "Qué cambia."`.

El script compila y sube `main`. Después crea la etiqueta `X.Y.Z` y publica la release con el instalador; para eso hace falta `gh` con sesión iniciada. Los programas instalados la encuentran solos. El actualizador ignora borradores, pre-releases y etiquetas que no sean `X.Y.Z`, y solo acepta un asset cuyo nombre contenga `setup` y acabe en `.exe`.

Para probar el plugin sin instalador, enlazar la carpeta y reiniciar el Stream Deck:

```powershell
New-Item -ItemType Junction -Path "$env:APPDATA\Elgato\StreamDeck\Plugins\com.cali.battery.sdPlugin" -Target "$PWD\streamdeck\com.cali.battery.sdPlugin"
```

- **Dibujo de la tecla:** `key.js`. Hay que reiniciar el Stream Deck para verlo.
- **Iconos:** el rayo de Litio se define una vez en `brand.py`. De ahí salen el del programa (`installer\make_icon.py`), los del plugin (`streamdeck\make_icons.py`) y el de la bandeja sin dispositivos. En SVG está repetido en `settings.qml` y `key.js`.

## Quitar

Desde «Instalar o quitar programas» de Windows. Los datos de `%LOCALAPPDATA%\battery-tray` no se borran.
