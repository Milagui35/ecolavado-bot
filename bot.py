#!/usr/bin/env python3
"""Bot de Instagram para Mi Eco Lavado.

Recibe los mensajes directos de Instagram (webhook de Meta), consulta el sistema
de la lavandería en SOLO LECTURA (/api/catalogo, /api/cotizar, /api/politicas),
le pide la respuesta a Claude y la envía de vuelta por Instagram.

Solo usa la librería estándar de Python (sin pip install).

Uso:
    python3 bot.py                    # arranca el servidor
    python3 bot.py bloquear <id>      # el bot deja de responder a esa cuenta
    python3 bot.py desbloquear <id>
    python3 bot.py probar-sistema     # revisa que el sistema de precios responda
"""

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from datetime import timedelta
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prompt import ahora_hn, construir_prompt

log = logging.getLogger("ecolavado-bot")

# --------------------------------------------------------------------------
# Configuración (variables de entorno, nunca en el código)
# --------------------------------------------------------------------------


def env(nombre, defecto=""):
    return os.environ.get(nombre, defecto).strip()


def cargar_env_file(ruta=".env"):
    """Carga un archivo .env sencillo (CLAVE=valor) si existe."""
    if not os.path.exists(ruta):
        return
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Config:
    def __init__(self):
        self.puerto = int(env("PORT", "8081"))
        self.ig_verify_token = env("IG_VERIFY_TOKEN")
        self.ig_app_secret = env("IG_APP_SECRET")
        self.ig_access_token = env("IG_ACCESS_TOKEN")
        self.ig_graph_version = env("IG_GRAPH_VERSION", "v23.0")
        # Ruta "Unified Login" (token EAA...): graph.facebook.com + ID de la cuenta de Instagram.
        # Ruta "Instagram Login" (token IGAA...): IG_API_BASE=https://graph.instagram.com y sin ID.
        self.ig_api_base = env("IG_API_BASE", "https://graph.facebook.com").rstrip("/")
        self.ig_account_id = env("IG_ACCOUNT_ID")
        self.anthropic_key = env("ANTHROPIC_API_KEY")
        self.modelo = env("CLAUDE_MODEL", "claude-haiku-5-5")
        self.sistema_url = env("SISTEMA_URL", "https://64-177-50-255.sslip.io").rstrip("/")
        self.bot_token = env("BOT_TOKEN")
        self.telegram_token = env("TELEGRAM_BOT_TOKEN")
        self.telegram_chat = env("TELEGRAM_CHAT_ID")
        self.max_msgs_dia = int(env("MAX_MSGS_DIA", "20"))
        self.handoff_horas = float(env("HANDOFF_HORAS", "12"))
        self.dryclean_precio_base = env("DRYCLEAN_PRECIO_BASE", "1") == "1"
        self.db_path = env("DB_PATH", "bot.db")
        self.bot_activo = env("BOT_ACTIVO", "1") == "1"
        self.log_dias = int(env("LOG_DIAS", "60"))  # días que se conserva el registro del panel
        self.telefono = "+504 3227-5543"


CFG = Config()

AVISO_MEDIA = ("Recibimos su archivo. Una persona de nuestro equipo lo revisará. "
               f"Si es urgente, puede escribirnos o llamarnos al WhatsApp {CFG.telefono}.")
MSG_FALLA = (f"Disculpe, en este momento no puedo responderle. Por favor comuníquese con nuestro "
             f"personal al WhatsApp o llamada {CFG.telefono}. Con gusto le atenderemos.")

MSG_LIMITE = ("Hemos llegado al límite de mensajes de este chat por hoy. Para seguir con su consulta, "
              "escríbanos o llámenos al WhatsApp +504 3227-5543, con mucho gusto le atenderemos. ¡Gracias por escribirnos!")

# --------------------------------------------------------------------------
# Base de datos local (historial corto, duplicados, límites)
# --------------------------------------------------------------------------

_db_lock = threading.Lock()


def db():
    con = sqlite3.connect(CFG.db_path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS vistos (mid TEXT PRIMARY KEY, ts REAL);
        CREATE TABLE IF NOT EXISTS mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, rol TEXT, texto TEXT, ts REAL);
        CREATE INDEX IF NOT EXISTS ix_msg ON mensajes(uid, ts);
        CREATE TABLE IF NOT EXISTS usuarios (
            uid TEXT PRIMARY KEY, fuera_tema INTEGER DEFAULT 0, handoff_hasta REAL DEFAULT 0,
            bloqueado INTEGER DEFAULT 0, aviso_media_ts REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS diario (uid TEXT, dia TEXT, n INTEGER, PRIMARY KEY (uid, dia));
        -- Registro para el panel de métricas (se conserva LOG_DIAS días)
        CREATE TABLE IF NOT EXISTS registro (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, uid TEXT, tipo TEXT, texto TEXT, extra TEXT);
        CREATE INDEX IF NOT EXISTS ix_reg_ts ON registro(ts);
        CREATE INDEX IF NOT EXISTS ix_reg_uid ON registro(uid, ts);
        CREATE TABLE IF NOT EXISTS perfiles (uid TEXT PRIMARY KEY, nombre TEXT, usuario TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT);
        """)
        con.execute("PRAGMA journal_mode=WAL")


def purgar():
    """No guardamos conversaciones más de 3 días."""
    limite = time.time() - 3 * 86400
    with _db_lock, db() as con:
        con.execute("DELETE FROM mensajes WHERE ts < ?", (limite,))
        con.execute("DELETE FROM vistos WHERE ts < ?", (limite,))
        con.execute("DELETE FROM diario WHERE dia < ?",
                    ((ahora_hn() - timedelta(days=3)).strftime("%Y-%m-%d"),))
    if CFG.log_dias > 0:
        with _db_lock, db() as con:
            con.execute("DELETE FROM registro WHERE ts < ?", (time.time() - CFG.log_dias * 86400,))


def log_evento(uid, tipo, texto="", extra=None):
    """Guarda un evento para el panel. Nunca debe romper la conversación."""
    try:
        with _db_lock, db() as con:
            con.execute("INSERT INTO registro(ts, uid, tipo, texto, extra) VALUES (?, ?, ?, ?, ?)",
                        (time.time(), uid, tipo, texto or "",
                         json.dumps(extra, ensure_ascii=False) if extra is not None else None))
    except Exception:
        log.exception("No se pudo guardar el evento %s", tipo)


def get_ajuste(clave, defecto=""):
    with _db_lock, db() as con:
        f = con.execute("SELECT valor FROM ajustes WHERE clave = ?", (clave,)).fetchone()
    return f["valor"] if f else defecto


def guardar_perfil(uid):
    """Trae el nombre y usuario de Instagram del cliente (si Meta lo permite)."""
    try:
        with _db_lock, db() as con:
            if con.execute("SELECT 1 FROM perfiles WHERE uid = ?", (uid,)).fetchone():
                return
        url = (f"{CFG.ig_api_base}/{CFG.ig_graph_version}/{urllib.parse.quote(uid)}"
               "?fields=name,username")
        st, r = http_json(url, headers={"Authorization": f"Bearer {CFG.ig_access_token}"}, timeout=10)
        nombre, usuario_ig = (r.get("name") or ""), (r.get("username") or "")
        if st != 200:
            log.info("Sin perfil para %s (%s)", uid, st)
        with _db_lock, db() as con:
            con.execute("INSERT OR REPLACE INTO perfiles(uid, nombre, usuario, ts) VALUES (?, ?, ?, ?)",
                        (uid, nombre, usuario_ig, time.time()))
    except Exception:
        log.exception("Error al traer el perfil")


def ya_visto(mid):
    if not mid:
        return False
    with _db_lock, db() as con:
        try:
            con.execute("INSERT INTO vistos(mid, ts) VALUES (?, ?)", (mid, time.time()))
            return False
        except sqlite3.IntegrityError:
            return True


def usuario(uid):
    with _db_lock, db() as con:
        con.execute("INSERT OR IGNORE INTO usuarios(uid) VALUES (?)", (uid,))
        return dict(con.execute("SELECT * FROM usuarios WHERE uid = ?", (uid,)).fetchone())


def set_usuario(uid, **campos):
    usuario(uid)
    cols = ", ".join(f"{k} = ?" for k in campos)
    with _db_lock, db() as con:
        con.execute(f"UPDATE usuarios SET {cols} WHERE uid = ?", (*campos.values(), uid))


def contar_hoy(uid):
    dia = ahora_hn().strftime("%Y-%m-%d")
    with _db_lock, db() as con:
        con.execute("INSERT OR IGNORE INTO diario(uid, dia, n) VALUES (?, ?, 0)", (uid, dia))
        con.execute("UPDATE diario SET n = n + 1 WHERE uid = ? AND dia = ?", (uid, dia))
        return con.execute("SELECT n FROM diario WHERE uid = ? AND dia = ?", (uid, dia)).fetchone()["n"]


def guardar_msg(uid, rol, texto):
    with _db_lock, db() as con:
        con.execute("INSERT INTO mensajes(uid, rol, texto, ts) VALUES (?, ?, ?, ?)",
                    (uid, rol, texto, time.time()))


def historial(uid, max_msgs=12, max_horas=24):
    """Últimos mensajes (texto) alternando roles, empezando por 'user'."""
    desde = time.time() - max_horas * 3600
    with _db_lock, db() as con:
        filas = con.execute(
            "SELECT rol, texto FROM mensajes WHERE uid = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
            (uid, desde, max_msgs)).fetchall()
    out = []
    for f in reversed(filas):
        if out and out[-1]["role"] == f["rol"]:
            out[-1]["content"] += "\n" + f["texto"]
        else:
            out.append({"role": f["rol"], "content": f["texto"]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


# --------------------------------------------------------------------------
# Llamadas HTTP (separadas para poder simularlas en las pruebas)
# --------------------------------------------------------------------------


def http_json(url, datos=None, headers=None, timeout=30):
    headers = dict(headers or {})
    body = None
    if datos is not None:
        body = json.dumps(datos).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(cuerpo)
        except ValueError:
            return e.code, {"error": cuerpo[:300]}


def sistema_get(ruta):
    return http_json(CFG.sistema_url + ruta, headers={"X-Bot-Token": CFG.bot_token})


def sistema_post(ruta, datos):
    return http_json(CFG.sistema_url + ruta, datos, headers={"X-Bot-Token": CFG.bot_token})


def claude(system, messages, tools):
    body = {"model": CFG.modelo, "max_tokens": 1000, "system": system,
            "messages": messages, "tools": tools}
    st, resp = http_json("https://api.anthropic.com/v1/messages", body, timeout=60, headers={
        "x-api-key": CFG.anthropic_key, "anthropic-version": "2023-06-01"})
    if st != 200:
        raise RuntimeError(f"Anthropic API {st}: {str(resp)[:300]}")
    return resp


def enviar_ig(uid, texto):
    """Envía un mensaje directo (se parte en trozos si es largo)."""
    url = f"{CFG.ig_api_base}/{CFG.ig_graph_version}/{CFG.ig_account_id or 'me'}/messages"
    for trozo in partir(texto):
        st, resp = http_json(url, {"recipient": {"id": uid}, "message": {"text": trozo}},
                             headers={"Authorization": f"Bearer {CFG.ig_access_token}"})
        if st != 200:
            log.error("No se pudo enviar a %s: %s %s", uid, st, str(resp)[:300])
            return False
    return True


def telegram(texto):
    if not (CFG.telegram_token and CFG.telegram_chat):
        return False
    st, resp = http_json(f"https://api.telegram.org/bot{CFG.telegram_token}/sendMessage",
                         {"chat_id": CFG.telegram_chat, "text": texto})
    return st == 200 and bool(resp.get("ok"))


def partir(texto, largo=900):
    texto = texto.strip()
    if len(texto) <= largo:
        return [texto] if texto else []
    trozos, actual = [], ""
    for parrafo in texto.split("\n"):
        if len(actual) + len(parrafo) + 1 > largo and actual:
            trozos.append(actual.strip())
            actual = ""
        while len(parrafo) > largo:
            trozos.append(parrafo[:largo])
            parrafo = parrafo[largo:]
        actual += parrafo + "\n"
    if actual.strip():
        trozos.append(actual.strip())
    return trozos


# --------------------------------------------------------------------------
# Herramientas que Claude puede usar
# --------------------------------------------------------------------------

TOOLS = [
    {"name": "buscar_catalogo",
     "description": "Busca productos y servicios del catálogo oficial (edredones, planchado, "
                    "tratamientos, dry clean, etc.) por palabra clave. Devuelve precios vigentes "
                    "con ISV incluido. Para servicios por peso usa 'cotizar'.",
     "input_schema": {"type": "object", "properties": {
         "consulta": {"type": "string", "description": "Palabra clave, ej. 'edredon', 'planchado'"}},
         "required": ["consulta"]}},
    {"name": "cotizar",
     "description": "Calcula el precio oficial (con ISV) de un servicio. Por peso: servicio + libras. "
                    "Precio fijo: servicio + cantidad. Puede devolver 'candidatos' si el nombre es "
                    "ambiguo o ok:false si no se puede determinar.",
     "input_schema": {"type": "object", "properties": {
         "servicio": {"type": "string", "description": "Ej. 'Solo Lavado', 'Full Service', 'Edredon Queen'"},
         "libras": {"type": "number"},
         "cantidad": {"type": "integer"},
         "producto_id": {"type": "integer"}}}},
    {"name": "obtener_politicas",
     "description": "Devuelve el texto oficial de las políticas de prendas de Mi Eco Lavado.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "escalar_a_humano",
     "description": "Envía una consulta al personal de Mi Eco Lavado para atención humana. "
                    "Devuelve enviado=true solo si el aviso realmente se envió.",
     "input_schema": {"type": "object", "properties": {
         "nombre": {"type": "string"},
         "contacto": {"type": "string", "description": "Número de WhatsApp del cliente"},
         "motivo": {"type": "string"},
         "servicio": {"type": "string"},
         "resumen": {"type": "string", "description": "Descripción breve de la situación"}},
         "required": ["motivo"]}},
    {"name": "marcar_fuera_de_tema",
     "description": "Llamar cuando el mensaje no tenga que ver con la lavandería.",
     "input_schema": {"type": "object", "properties": {}}},
]

_cache = {}
CACHE_SEG = 300


def con_cache(clave, fn):
    ahora = time.time()
    if clave in _cache and ahora - _cache[clave][0] < CACHE_SEG:
        return _cache[clave][1]
    valor = fn()
    if valor is not None:
        _cache[clave] = (ahora, valor)
    return valor


def normalizar(s):
    s = unicodedata.normalize("NFD", str(s).lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def lista_de_productos(data):
    """Encuentra la lista de productos sin depender del nombre exacto de la llave."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def es_precio_base(p):
    return bool(p.get("es_precio_base"))


CLAVES_PRECIO = re.compile(r"(precio|price|total|isv|monto)", re.I)


def limpiar_producto(p):
    """Quita tablas largas y, si está configurado, los precios de dry clean."""
    out = {}
    for k, v in p.items():
        if k == "tabla_libras_precio" and isinstance(v, list):
            out["cobro_por_peso"] = (f"Se cobra por peso ({len(v)} tramos). "
                                     "Usar la herramienta cotizar con las libras.")
            continue
        if es_precio_base(p) and not CFG.dryclean_precio_base and CLAVES_PRECIO.search(k):
            continue
        out[k] = v
    if es_precio_base(p):
        out["aviso"] = ("Precio BASE. La prenda se valora en persona y el precio final puede variar."
                        if CFG.dryclean_precio_base else
                        "Dry Clean: no dar precio; se valora en persona en la lavandería.")
    return out


def tool_buscar_catalogo(consulta):
    st, data = con_cache("catalogo", lambda: _ok(sistema_get("/api/catalogo"))) or (0, None)
    productos = lista_de_productos(data)
    if not productos:
        return {"ok": False, "error": "No se pudo leer el catálogo en este momento."}
    palabras = [w for w in normalizar(consulta).split() if len(w) > 1]
    encontrados = []
    for p in productos:
        texto = normalizar(json.dumps(p, ensure_ascii=False))
        if all(w in texto for w in palabras):
            encontrados.append(limpiar_producto(p))
    return {"ok": True, "coincidencias": len(encontrados), "productos": encontrados[:12]}


def _ok(resp):
    st, data = resp
    return (st, data) if st == 200 else None


def _resolver_ambiguo(datos, data):
    """Resuelve solos los casos donde el nombre coincide con varios productos pero no hay duda:
    - con libras: si entre los candidatos solo uno se cobra por libra, cotiza ese;
    - sin libras: si todos son de precio fijo y cuestan lo mismo (ej. un edredón lavado como
      Solo Lavado o como Full Service), cotiza uno; el precio es el mismo."""
    if not (isinstance(data, dict) and data.get("candidatos") and "producto_id" not in datos):
        return data
    resp = con_cache("catalogo", lambda: _ok(sistema_get("/api/catalogo")))
    por_id = {p.get("id"): p for p in lista_de_productos(resp[1])} if resp else {}
    ids = [c.get("id") for c in data["candidatos"] if isinstance(c, dict)]
    prods = [por_id.get(i) for i in ids]
    if "libras" in datos:
        por_libra = [i for i in ids if por_id.get(i, {}).get("tipo") == "por_libra"]
        if len(por_libra) != 1:
            return data
        nuevo_datos = {"producto_id": por_libra[0], "libras": datos["libras"]}
    else:
        if not ids or any(p is None or p.get("tipo") != "fijo" for p in prods):
            return data
        if len({p.get("precio") for p in prods}) != 1:
            return data
        nuevo_datos = {"producto_id": ids[0], "cantidad": datos.get("cantidad") or 1}
    st, nuevo = sistema_post("/api/cotizar", nuevo_datos)
    return nuevo if st == 200 else data


def tool_cotizar(args):
    datos = {k: v for k, v in args.items()
             if k in ("servicio", "libras", "cantidad", "producto_id") and v not in (None, "")}
    if not datos:
        return {"ok": False, "error": "Falta indicar el servicio."}
    st, data = sistema_post("/api/cotizar", datos)
    if st in (401, 403):
        log.error("El token del bot fue rechazado por el sistema (%s)", st)
        return {"ok": False, "error": "Sistema de precios no disponible."}
    data = _resolver_ambiguo(datos, data)
    if isinstance(data, dict) and not CFG.dryclean_precio_base and data.get("es_precio_base"):
        data = {k: v for k, v in data.items()
                if not CLAVES_PRECIO.search(k) and k != "desglose"}
        data["aviso"] = "Dry Clean: no dar precio; se valora en persona en la lavandería."
    return data


def tool_politicas():
    resp = con_cache("politicas", lambda: _ok(sistema_get("/api/politicas")))
    if not resp:
        return {"ok": False, "error": "No se pudieron leer las políticas."}
    data = resp[1] if isinstance(resp[1], dict) else {}
    texto = data.get("texto_completo")
    if not texto:
        texto = "\n".join(f"{p.get('numero', '')}. {p.get('texto', '')}"
                          for p in data.get("politicas", []) if isinstance(p, dict))
    if not texto:
        return {"ok": False, "error": "No se pudieron leer las políticas."}
    return {"ok": True, "politicas": texto}


def tool_escalar(uid, args, ctx):
    texto = (
        "NUEVA CONSULTA PARA ATENCIÓN HUMANA\n\n"
        f"Cliente: {args.get('nombre') or 'No indicó'}\n\n"
        f"Contacto: {args.get('contacto') or 'No indicó'}\n\n"
        f"Motivo: {args.get('motivo') or ''}\n\n"
        f"Servicio: {args.get('servicio') or 'No indicado'}\n\n"
        f"Detalle: {args.get('resumen') or ''}\n\n"
        f"Origen: Instagram (ID de la conversación: {uid})\n\n"
        "Requiere seguimiento por parte del personal."
    )
    detalle = {k: args.get(k) for k in ("nombre", "contacto", "motivo", "servicio", "resumen")}
    if telegram(texto):
        set_usuario(uid, handoff_hasta=time.time() + CFG.handoff_horas * 3600)
        ctx["escalado"] = True
        log_evento(uid, "escalado", args.get("motivo") or "", {**detalle, "aviso": True})
        return {"enviado": True}
    log_evento(uid, "escalado", args.get("motivo") or "", {**detalle, "aviso": False})
    log.error("No se pudo enviar el aviso de escalamiento")
    return {"enviado": False,
            "instruccion": f"NO digas que se envió. Dale al cliente el {CFG.telefono}."}


def ejecutar_tool(nombre, args, uid, ctx):
    try:
        if nombre == "buscar_catalogo":
            log_evento(uid, "busco", args.get("consulta", ""))
            return tool_buscar_catalogo(args.get("consulta", ""))
        if nombre == "cotizar":
            r = tool_cotizar(args)
            if isinstance(r, dict):
                log_evento(uid, "cotizo", args.get("servicio") or "", {
                    "ok": bool(r.get("ok")), "producto": r.get("producto"), "tipo": r.get("tipo"),
                    "libras": args.get("libras"), "cantidad": args.get("cantidad"),
                    "total": r.get("total")})
            return r
        if nombre == "obtener_politicas":
            return tool_politicas()
        if nombre == "escalar_a_humano":
            return tool_escalar(uid, args, ctx)
        if nombre == "marcar_fuera_de_tema":
            n = usuario(uid)["fuera_tema"] + 1
            set_usuario(uid, fuera_tema=n)
            log_evento(uid, "fuera_tema", extra={"veces": n})
            if n >= 2:
                ctx["silenciar"] = True
            return {"veces": n, "silenciar": n >= 2}
    except Exception:
        log.exception("Error en herramienta %s", nombre)
    return {"ok": False, "error": "No disponible en este momento."}


# --------------------------------------------------------------------------
# Lógica de la conversación
# --------------------------------------------------------------------------


def responder(uid, texto):
    """Devuelve el texto a enviar, o None si el bot debe quedarse callado."""
    msgs = historial(uid)
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + texto
    else:
        msgs.append({"role": "user", "content": texto})
    sistema = construir_prompt(CFG.dryclean_precio_base)
    ctx = {"silenciar": False, "escalado": False}

    for _ in range(6):
        resp = claude(sistema, msgs, TOOLS)
        uso = resp.get("usage") or {}
        log_evento(uid, "uso", extra={"i": uso.get("input_tokens", 0), "o": uso.get("output_tokens", 0)})
        if resp.get("stop_reason") == "tool_use":
            msgs.append({"role": "assistant", "content": resp["content"]})
            resultados = []
            for b in resp["content"]:
                if b.get("type") == "tool_use":
                    salida = ejecutar_tool(b["name"], b.get("input") or {}, uid, ctx)
                    resultados.append({"type": "tool_result", "tool_use_id": b["id"],
                                       "content": json.dumps(salida, ensure_ascii=False)})
            msgs.append({"role": "user", "content": resultados})
            continue
        if ctx["silenciar"]:
            return None
        out = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text").strip()
        log.info("Respuesta de Claude: %s caracteres, stop_reason=%s", len(out), resp.get("stop_reason"))
        return out or None
    return MSG_FALLA


TIPOS_IGNORADOS = {"share", "ig_reel", "reel", "story_mention", "story", "ig_post", "like_heart"}
TIPOS_MEDIA = {"image", "audio", "video", "file"}

_locks = {}
_locks_guard = threading.Lock()


def lock_de(uid):
    with _locks_guard:
        return _locks.setdefault(uid, threading.Lock())


def procesar_evento(ev):
    """Procesa un evento 'messaging' del webhook de Instagram."""
    msg = ev.get("message")
    if not msg or msg.get("is_echo"):
        log.info("Evento ignorado (%s)", "eco propio" if msg else "sin mensaje: " + ",".join(sorted(ev.keys())))
        return
    uid = str((ev.get("sender") or {}).get("id", ""))
    log.info("Mensaje entrante de %s (texto=%s, adjuntos=%s)", uid, bool(msg.get("text")),
             len(msg.get("attachments") or []))
    if not uid or ya_visto(msg.get("mid")):
        return
    if not CFG.bot_activo or get_ajuste("pausado") == "1":
        log.info("Bot pausado: no se responde")
        return

    with lock_de(uid):
        u = usuario(uid)
        if u["bloqueado"]:
            return
        n_hoy = contar_hoy(uid)
        if n_hoy > CFG.max_msgs_dia:
            log.info("Límite diario alcanzado para %s", uid)
            log_evento(uid, "limite")
            return
        if n_hoy == CFG.max_msgs_dia:
            # Último mensaje del día: no se llama a la IA, se le indica cómo seguir por WhatsApp.
            log.info("Último mensaje del día para %s: se envía el aviso de WhatsApp", uid)
            texto_in = (msg.get("text") or "").strip()
            if texto_in:
                log_evento(uid, "in", texto_in)
            log_evento(uid, "limite", "ultimo")
            if enviar_ig(uid, MSG_LIMITE):
                guardar_msg(uid, "assistant", MSG_LIMITE)
                log_evento(uid, "out", MSG_LIMITE)
            return

        texto = (msg.get("text") or "").strip()
        if not texto:
            tipos = {a.get("type") for a in (msg.get("attachments") or [])}
            log_evento(uid, "media", ",".join(sorted(t for t in tipos if t)))
            if tipos & TIPOS_MEDIA and not (tipos & TIPOS_IGNORADOS) \
                    and time.time() - u["aviso_media_ts"] > 6 * 3600:
                set_usuario(uid, aviso_media_ts=time.time())
                enviar_ig(uid, AVISO_MEDIA)
            return  # reels, stickers, publicaciones compartidas: se ignoran

        log_evento(uid, "in", texto)
        threading.Thread(target=guardar_perfil, args=(uid,), daemon=True).start()
        if u["handoff_hasta"] > time.time():
            log.info("Silencio por atención humana para %s", uid)
            return  # ya lo está atendiendo una persona

        guardar_msg(uid, "user", texto)
        try:
            respuesta = responder(uid, texto)
        except Exception as e:
            log.exception("Fallo al generar respuesta para %s", uid)
            log_evento(uid, "error", "claude", {"detalle": str(e)[:200]})
            respuesta = MSG_FALLA
        if respuesta:
            if enviar_ig(uid, respuesta):
                guardar_msg(uid, "assistant", respuesta)
                log_evento(uid, "out", respuesta)
            else:
                log_evento(uid, "error", "envio", {"detalle": "Instagram rechazó el envío"})


def procesar_payload(payload):
    eventos = []
    for entrada in payload.get("entry", []):
        eventos.extend(entrada.get("messaging", []))
        for cambio in entrada.get("changes", []):  # formato alterno del webhook
            if cambio.get("field") == "messages" and isinstance(cambio.get("value"), dict):
                eventos.append(cambio["value"])
            else:
                log.info("Cambio ignorado (campo: %s)", cambio.get("field"))
    if not eventos:
        log.info("Webhook sin mensajes (objeto: %s)", payload.get("object"))
    for ev in eventos:
        try:
            procesar_evento(ev)
        except Exception:
            log.exception("Error procesando evento")


# --------------------------------------------------------------------------
# Servidor web (webhook)
# --------------------------------------------------------------------------


def firma_valida(cuerpo, cabecera):
    if not CFG.ig_app_secret or not cabecera or not cabecera.startswith("sha256="):
        return False
    esperada = hmac.new(CFG.ig_app_secret.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, cabecera.split("=", 1)[1])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _responder(self, codigo, cuerpo=b"", tipo="text/plain"):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/salud":
            return self._responder(200, b"ok")
        if url.path == "/webhook":
            q = urllib.parse.parse_qs(url.query)
            ok = (q.get("hub.mode", [""])[0] == "subscribe" and CFG.ig_verify_token
                  and hmac.compare_digest(q.get("hub.verify_token", [""])[0], CFG.ig_verify_token))
            if ok:
                return self._responder(200, q.get("hub.challenge", [""])[0].encode())
            return self._responder(403, b"verificacion fallida")
        if url.path in ("/privacidad", "/eliminar-datos", "/terminos"):
            import legal
            pagina = {"/privacidad": legal.PRIVACIDAD, "/eliminar-datos": legal.ELIMINAR,
                      "/terminos": legal.TERMINOS}[url.path]
            return self._responder(200, pagina.encode("utf-8"), "text/html; charset=utf-8")
        self._responder(404, b"no encontrado")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/webhook":
            return self._responder(404, b"no encontrado")
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > 1_000_000:
            return self._responder(413, b"muy grande")
        cuerpo = self.rfile.read(largo)
        if not firma_valida(cuerpo, self.headers.get("X-Hub-Signature-256")):
            log.warning("Firma inválida: petición rechazada")
            return self._responder(403, b"firma invalida")
        try:
            payload = json.loads(cuerpo.decode("utf-8"))
        except ValueError:
            return self._responder(400, b"json invalido")
        log.info("Webhook recibido de Meta (%s bytes)", largo)
        self._responder(200, b"EVENT_RECEIVED")  # responder rápido a Meta
        threading.Thread(target=procesar_payload, args=(payload,), daemon=True).start()


def faltantes():
    req = {"IG_VERIFY_TOKEN": CFG.ig_verify_token, "IG_APP_SECRET": CFG.ig_app_secret,
           "IG_ACCESS_TOKEN": CFG.ig_access_token, "ANTHROPIC_API_KEY": CFG.anthropic_key,
           "BOT_TOKEN": CFG.bot_token}
    return [k for k, v in req.items() if not v]


def probar_sistema():
    """Muestra la forma de las respuestas del sistema (sin imprimir el token)."""
    st, cat = sistema_get("/api/catalogo")
    print("GET /api/catalogo ->", st)
    prods = lista_de_productos(cat)
    print("  productos:", len(prods))
    if prods:
        print("  ejemplo (llaves):", sorted(prods[0].keys()))
        print("  ejemplo:", json.dumps(limpiar_producto(prods[0]), ensure_ascii=False)[:400])
    for datos in ({"servicio": "Solo Lavado", "libras": 15},
                  {"servicio": "Full Service", "libras": 15},
                  {"servicio": "Edredon Queen", "cantidad": 1}):
        r = tool_cotizar(dict(datos))
        print("cotizar", datos, "->", json.dumps(r, ensure_ascii=False)[:300])
    st, pol = sistema_get("/api/politicas")
    print("GET /api/politicas ->", st, "políticas:", len(pol.get("politicas", [])) if isinstance(pol, dict) else "?")


def main():
    cargar_env_file()
    global CFG
    CFG = Config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd in ("bloquear", "desbloquear") and len(sys.argv) > 2:
            set_usuario(sys.argv[2], bloqueado=1 if cmd == "bloquear" else 0)
            print("Listo:", cmd, sys.argv[2])
            return
        if cmd == "probar-sistema":
            return probar_sistema()
        print(__doc__)
        return
    falta = faltantes()
    if falta:
        sys.exit("Faltan variables de entorno: " + ", ".join(falta))
    if not (CFG.telegram_token and CFG.telegram_chat):
        log.warning("Sin Telegram configurado: los escalamientos NO se podrán avisar "
                    "(el bot le dará el teléfono al cliente).")
    purgar()

    def purga_periodica():
        while True:
            time.sleep(6 * 3600)
            try:
                purgar()
            except Exception:
                log.exception("Error al purgar")

    threading.Thread(target=purga_periodica, daemon=True).start()
    log.info("Bot escuchando en el puerto %s (modelo %s)", CFG.puerto, CFG.modelo)
    ThreadingHTTPServer(("0.0.0.0", CFG.puerto), Handler).serve_forever()


if __name__ == "__main__":
    main()
