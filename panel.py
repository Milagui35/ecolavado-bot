#!/usr/bin/env python3
"""Panel web del bot de Instagram de Mi Eco Lavado.

Muestra métricas, conversaciones y escalaciones leyendo la misma base de datos del bot
(bot.db). Corre como un servicio aparte, así que si el panel falla el bot no se entera.

Solo usa la librería estándar de Python. No carga nada de internet: sin scripts, sin
fuentes ni librerías externas.

Uso:
    python3 panel.py             # arranca el panel (puerto PANEL_PORT, por defecto 8082)
"""

import contextlib
import hashlib
import hmac
import html
import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prompt import HN_TZ, horario_de

log = logging.getLogger("ecolavado-panel")

DIAS_CORTO = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES_CORTO = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------


def env(nombre, defecto=""):
    return os.environ.get(nombre, defecto).strip()


def cargar_env_file(ruta=".env"):
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
        self.puerto = int(env("PANEL_PORT", "8082"))
        self.password = env("PANEL_PASSWORD")
        self.db_path = env("DB_PATH", "bot.db")
        self.cookie_secure = env("PANEL_COOKIE_SECURE", "1") == "1"
        self.sistemas_file = env("SISTEMAS_FILE", "sistemas.json")
        self.sesion_dias = 30


CFG = Config()

# --------------------------------------------------------------------------
# Sesión, CSRF y límite de intentos
# --------------------------------------------------------------------------


def _clave():
    return hashlib.sha256(("panel|" + CFG.password).encode()).digest()


def _firma(texto):
    return hmac.new(_clave(), texto.encode(), hashlib.sha256).hexdigest()


def crear_sesion():
    exp = int(time.time()) + CFG.sesion_dias * 86400
    return f"{exp}.{_firma('s|' + str(exp))}"


def sesion_valida(token):
    try:
        exp, firma = (token or "").split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(firma, _firma("s|" + exp))
    except ValueError:
        return False


def csrf_de(token):
    return _firma("csrf|" + (token or ""))


_intentos = {}
_intentos_lock = threading.Lock()
MAX_FALLOS = 5
PAUSA_FALLO = 1  # segundos de espera tras una contraseña incorrecta
BLOQUEO_SEG = 15 * 60


def login_bloqueado(ip):
    with _intentos_lock:
        n, hasta = _intentos.get(ip, (0, 0))
        return hasta > time.time()


def registrar_intento(ip, ok):
    with _intentos_lock:
        if ok:
            _intentos.pop(ip, None)
            return
        n, hasta = _intentos.get(ip, (0, 0))
        n += 1
        _intentos[ip] = (n, time.time() + BLOQUEO_SEG if n >= MAX_FALLOS else 0)
        if n >= MAX_FALLOS:
            _intentos[ip] = (0, time.time() + BLOQUEO_SEG)


# --------------------------------------------------------------------------
# Base de datos
# --------------------------------------------------------------------------


@contextlib.contextmanager
def db():
    """Abre la base, confirma los cambios y siempre la cierra."""
    con = sqlite3.connect(CFG.db_path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def asegurar_tablas():
    """El bot crea estas tablas; por si el panel arranca antes, las creamos igual."""
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS registro (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, uid TEXT, tipo TEXT, texto TEXT, extra TEXT);
        CREATE INDEX IF NOT EXISTS ix_reg_ts ON registro(ts);
        CREATE INDEX IF NOT EXISTS ix_reg_uid ON registro(uid, ts);
        CREATE TABLE IF NOT EXISTS perfiles (uid TEXT PRIMARY KEY, nombre TEXT, usuario TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT);
        CREATE TABLE IF NOT EXISTS usuarios (
            uid TEXT PRIMARY KEY, fuera_tema INTEGER DEFAULT 0, handoff_hasta REAL DEFAULT 0,
            bloqueado INTEGER DEFAULT 0, aviso_media_ts REAL DEFAULT 0);
        """)
        con.execute("PRAGMA journal_mode=WAL")


def extra_de(fila):
    try:
        return json.loads(fila["extra"]) if fila["extra"] else {}
    except (ValueError, TypeError):
        return {}


# --------------------------------------------------------------------------
# Utilidades de fecha y texto
# --------------------------------------------------------------------------


def a_hn(ts):
    return datetime.fromtimestamp(ts, HN_TZ)


def hora12(dt):
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'a. m.' if dt.hour < 12 else 'p. m.'}"


def fmt_ts(ts):
    dt = a_hn(ts)
    return f"{DIAS_CORTO[dt.weekday()]} {dt.day} {MESES_CORTO[dt.month - 1]}, {hora12(dt)}"


def hace(ts):
    seg = max(0, time.time() - ts)
    if seg < 90:
        return "ahora"
    if seg < 3600:
        return f"hace {int(seg // 60)} min"
    if seg < 86400:
        return f"hace {int(seg // 3600)} h"
    return f"hace {int(seg // 86400)} d"


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def n_fmt(n):
    return f"{int(n):,}"


def inicio_dia(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def rango_periodo(periodo):
    """Devuelve (desde_ts, hasta_ts, etiqueta, dias)."""
    ahora = datetime.now(HN_TZ)
    hoy0 = inicio_dia(ahora)
    if periodo == "hoy":
        return hoy0.timestamp(), (hoy0 + timedelta(days=1)).timestamp(), "Hoy", 1
    dias = {"30": 30, "60": 60}.get(periodo, 7)
    desde = hoy0 - timedelta(days=dias - 1)
    return desde.timestamp(), (hoy0 + timedelta(days=1)).timestamp(), f"Últimos {dias} días", dias


# --------------------------------------------------------------------------
# Métricas
# --------------------------------------------------------------------------


def abierto_en(dt):
    h = horario_de(dt)
    if h is None:
        return False
    dec = dt.hour + dt.minute / 60
    return h[0] <= dec < h[1]


def pendientes_escalacion(con):
    """Escalaciones sin marcar como atendidas después."""
    filas = con.execute(
        "SELECT id, ts, uid, tipo, texto, extra FROM registro WHERE tipo IN ('escalado', 'atendido') "
        "ORDER BY ts").fetchall()
    ultimo_atendido = {}
    for f in filas:
        if f["tipo"] == "atendido":
            ultimo_atendido[f["uid"]] = f["ts"]
    pend, hechas = [], []
    for f in filas:
        if f["tipo"] != "escalado":
            continue
        (hechas if ultimo_atendido.get(f["uid"], 0) > f["ts"] else pend).append(f)
    return pend[::-1], hechas[::-1]


def metricas(con, desde, hasta, dias):
    ev = con.execute("SELECT ts, uid, tipo, texto, extra FROM registro WHERE ts >= ? AND ts < ? "
                     "ORDER BY ts", (desde, hasta)).fetchall()
    primeros = {f["uid"]: f["m"] for f in con.execute(
        "SELECT uid, MIN(ts) m FROM registro WHERE tipo = 'in' GROUP BY uid")}
    m = {
        "personas": set(), "entrantes": 0, "salientes": 0, "escalaciones": 0, "fuera_tema": 0,
        "errores": 0, "cotizaciones": 0, "fuera_horario": 0, "tok_in": 0, "tok_out": 0,
        "media": 0, "limite": 0,
    }
    por_dia = defaultdict(set)
    msgs_dia = Counter()
    por_hora_dia = Counter()    # mensajes por hora del día (todo el periodo)
    por_hora_hoy = Counter()
    productos = Counter()
    busquedas = Counter()
    hoy0 = inicio_dia(datetime.now(HN_TZ))
    for f in ev:
        t = f["tipo"]
        dt = a_hn(f["ts"])
        if t == "in":
            m["entrantes"] += 1
            m["personas"].add(f["uid"])
            por_dia[dt.date()].add(f["uid"])
            msgs_dia[dt.date()] += 1
            por_hora_dia[dt.hour] += 1
            if dt >= hoy0:
                por_hora_hoy[dt.hour] += 1
            if not abierto_en(dt):
                m["fuera_horario"] += 1
        elif t == "out":
            m["salientes"] += 1
        elif t == "escalado":
            m["escalaciones"] += 1
        elif t == "fuera_tema":
            m["fuera_tema"] += 1
        elif t == "error":
            m["errores"] += 1
        elif t == "media":
            m["media"] += 1
        elif t == "limite":
            m["limite"] += 1
        elif t == "uso":
            x = extra_de(f)
            m["tok_in"] += int(x.get("i") or 0)
            m["tok_out"] += int(x.get("o") or 0)
        elif t == "cotizo":
            x = extra_de(f)
            if x.get("ok"):
                m["cotizaciones"] += 1
                productos[x.get("producto") or f["texto"] or "(sin nombre)"] += 1
        elif t == "busco":
            q = (f["texto"] or "").strip().lower()
            if q:
                busquedas[q] += 1
    m["nuevos"] = sum(1 for u in m["personas"] if desde <= primeros.get(u, 0) < hasta)
    m["n_personas"] = len(m["personas"])
    m["pct_fuera_horario"] = (100 * m["fuera_horario"] / m["entrantes"]) if m["entrantes"] else 0
    m["por_dia"] = por_dia
    m["msgs_dia"] = msgs_dia
    m["por_hora_dia"] = por_hora_dia
    m["por_hora_hoy"] = por_hora_hoy
    m["productos"] = productos
    m["busquedas"] = busquedas
    return m


# --------------------------------------------------------------------------
# Gráficos SVG (sin librerías ni JavaScript)
# --------------------------------------------------------------------------


def _techo(v):
    if v <= 4:
        return 4
    p = 10 ** int(math.floor(math.log10(v)))
    for k in (1, 2, 2.5, 5, 10):
        if v <= k * p:
            return int(math.ceil(k * p))
    return int(10 * p)


def svg_barras(etiquetas, valores, unidad, ancho=680, alto=200, titulo=""):
    n = len(valores)
    techo = _techo(max(valores + [1]))
    ml, mr, mt, mb = 34, 6, 8, 24
    w, h = ancho - ml - mr, alto - mt - mb
    paso = w / max(n, 1)
    bw = max(3, min(26, paso * 0.62))
    base = mt + h
    partes = [f'<svg class="grafico" viewBox="0 0 {ancho} {alto}" role="img" '
              f'aria-label="{esc(titulo)}" preserveAspectRatio="xMidYMid meet">']
    for frac in (0, 0.5, 1):
        y = base - h * frac
        v = techo * frac
        partes.append(f'<line class="rejilla" x1="{ml}" x2="{ancho - mr}" y1="{y:.1f}" y2="{y:.1f}"/>')
        partes.append(f'<text class="eje" x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end">'
                      f'{int(v) if v == int(v) else v}</text>')
    salto = max(1, math.ceil(n / 12))
    for i, (e, v) in enumerate(zip(etiquetas, valores)):
        cx = ml + paso * i + paso / 2
        x = cx - bw / 2
        hb = h * v / techo
        if v > 0:
            top = base - hb
            r = min(4, hb)
            d = (f"M{x:.1f},{base:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} "
                 f"H{x + bw - r:.1f} Q{x + bw:.1f},{top:.1f} {x + bw:.1f},{top + r:.1f} V{base:.1f} Z")
            partes.append(f'<g class="barra"><title>{esc(e)}: {n_fmt(v)} {esc(unidad)}</title>'
                          f'<path d="{d}"/><rect class="zona" x="{cx - paso / 2:.1f}" y="{mt}" '
                          f'width="{paso:.1f}" height="{h}"/></g>')
        if i % salto == 0:
            partes.append(f'<text class="eje" x="{cx:.1f}" y="{alto - 6}" text-anchor="middle">{esc(e)}</text>')
    partes.append(f'<line class="base" x1="{ml}" x2="{ancho - mr}" y1="{base}" y2="{base}"/>')
    partes.append("</svg>")
    return "".join(partes)


def barras_horizontales(pares, unidad):
    if not pares:
        return '<p class="vacio">Todavía no hay datos.</p>'
    mx = max(v for _, v in pares) or 1
    filas = []
    for nombre, v in pares:
        filas.append(f'<li><span class="nom">{esc(nombre)}</span>'
                     f'<span class="pista"><span class="relleno" style="width:{100 * v / mx:.1f}%"></span></span>'
                     f'<span class="val">{n_fmt(v)}<span class="u"> {esc(unidad)}</span></span></li>')
    return '<ul class="hbar">' + "".join(filas) + "</ul>"


def tabla_datos(encabezados, filas):
    cab = "".join(f"<th>{esc(h)}</th>" for h in encabezados)
    cuerpo = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in f) + "</tr>" for f in filas)
    return (f'<details class="tabla"><summary>Ver como tabla</summary>'
            f'<table><thead><tr>{cab}</tr></thead><tbody>{cuerpo}</tbody></table></details>')


# --------------------------------------------------------------------------
# Plantilla HTML
# --------------------------------------------------------------------------

CSS = """
:root{color-scheme:light;--fondo:#f4f3f0;--sup:#fcfcfb;--borde:#e3e1dc;--tx:#0b0b0b;--tx2:#52514e;
--tx3:#7b7a75;--serie:#2a78d6;--serie-suave:#cde2fb;--ok:#0f7b4b;--aviso:#9a6a00;--mal:#b42318;
--entra:#ffffff;--sale:#e8f1fc}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--fondo:#121211;--sup:#1a1a19;--borde:#2c2c2a;
--tx:#f0efec;--tx2:#c3c2b7;--tx3:#8d8c85;--serie:#3987e5;--serie-suave:#184f95;--ok:#4cc38a;--aviso:#e2b24a;
--mal:#f97066;--entra:#242423;--sale:#16314f}}
*{box-sizing:border-box}
body{margin:0;background:var(--fondo);color:var(--tx);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--serie)}
header.top{background:var(--sup);border-bottom:1px solid var(--borde);position:sticky;top:0;z-index:5}
.top .fila{max-width:1080px;margin:0 auto;padding:10px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.marca{font-weight:700;letter-spacing:-.01em}
.marca span{color:var(--tx3);font-weight:500}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{padding:6px 12px;border-radius:8px;text-decoration:none;color:var(--tx2);font-weight:500}
nav a.act{background:var(--serie-suave);color:var(--tx)}
.der{margin-left:auto;display:flex;gap:10px;align-items:center}
main{max-width:1080px;margin:0 auto;padding:18px 16px 60px}
h1{font-size:22px;margin:4px 0 14px;letter-spacing:-.01em}
h2{font-size:16px;margin:0 0 10px}
.panel{background:var(--sup);border:1px solid var(--borde);border-radius:14px;padding:16px;margin-bottom:16px}
.rejilla-kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--sup);border:1px solid var(--borde);border-radius:14px;padding:14px}
.kpi .n{font-size:28px;font-weight:700;letter-spacing:-.02em;line-height:1.1}
.kpi .t{color:var(--tx2);font-size:13px;margin-top:4px}
.kpi .s{color:var(--tx3);font-size:12px;margin-top:2px}
.dos{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.grafico{width:100%;height:auto;display:block}
.grafico .rejilla{stroke:var(--borde);stroke-width:1}
.grafico .base{stroke:var(--tx3);stroke-width:1}
.grafico .eje{fill:var(--tx3);font-size:11px}
.grafico .barra path{fill:var(--serie)}
.grafico .barra .zona{fill:transparent}
.grafico .barra:hover path{opacity:.8}
.periodos{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
.periodos a{padding:5px 12px;border:1px solid var(--borde);border-radius:999px;text-decoration:none;color:var(--tx2);background:var(--sup)}
.periodos a.act{background:var(--tx);color:var(--sup);border-color:var(--tx)}
.hbar{list-style:none;margin:0;padding:0}
.hbar li{display:grid;grid-template-columns:minmax(110px,48%) 1fr auto;gap:10px;align-items:center;padding:5px 0}
.hbar .nom{overflow-wrap:anywhere;line-height:1.3}
.hbar .pista{background:var(--borde);height:8px;border-radius:4px;overflow:hidden}
.hbar .relleno{display:block;height:100%;background:var(--serie);border-radius:4px}
.hbar .val{font-variant-numeric:tabular-nums;font-weight:600}
.hbar .u{color:var(--tx3);font-weight:400;font-size:12px}
.vacio{color:var(--tx3);margin:6px 0}
details.tabla{margin-top:8px;color:var(--tx2);font-size:13px}
details.tabla summary{cursor:pointer}
table{border-collapse:collapse;width:100%;margin-top:8px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--borde);font-size:13px}
.lista{list-style:none;margin:0;padding:0}
.lista li a.fila-chat{display:grid;grid-template-columns:1fr auto;gap:4px 12px;padding:12px 4px;border-bottom:1px solid var(--borde);
text-decoration:none;color:inherit}
.lista li:last-child a{border-bottom:0}
.lista .quien{font-weight:600}
.lista .prev{color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;grid-column:1}
.lista .cuando{color:var(--tx3);font-size:12px;text-align:right}
.etq{display:inline-block;font-size:11px;font-weight:600;padding:1px 8px;border-radius:999px;margin-left:6px;
border:1px solid currentColor;vertical-align:middle}
.etq.aviso{color:var(--aviso)}.etq.mal{color:var(--mal)}.etq.ok{color:var(--ok)}.etq.gris{color:var(--tx3)}
.busq{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
input[type=text],input[type=password],select{font:inherit;padding:8px 10px;border:1px solid var(--borde);border-radius:8px;
background:var(--sup);color:var(--tx)}
button,.btn{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--borde);background:var(--sup);color:var(--tx);
cursor:pointer;text-decoration:none;display:inline-block}
button.pri{background:var(--serie);border-color:var(--serie);color:#fff;font-weight:600}
button.mal{color:var(--mal)}
.hilo{display:flex;flex-direction:column;gap:8px;max-width:720px}
.burb{padding:9px 12px;border-radius:14px;max-width:85%;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--borde)}
.burb.in{align-self:flex-start;background:var(--entra);border-bottom-left-radius:4px}
.burb.out{align-self:flex-end;background:var(--sale);border-bottom-right-radius:4px}
.burb small{display:block;color:var(--tx3);font-size:11px;margin-top:3px}
.sis{align-self:center;color:var(--tx2);font-size:12px;background:var(--sup);border:1px dashed var(--borde);padding:3px 10px;border-radius:999px}
.acciones{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.acciones form{margin:0}
.esc{border-left:4px solid var(--aviso)}
.esc dl{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:8px 0}
.esc dt{color:var(--tx3)}.esc dd{margin:0;overflow-wrap:anywhere}
.nota{color:var(--tx2);font-size:13px}
.login{max-width:340px;margin:12vh auto;padding:0 16px}
.err{color:var(--mal);margin:8px 0}
.sistemas{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.sistemas .tarjeta{display:block;background:var(--sup);border:1px solid var(--borde);border-radius:14px;padding:14px;
text-decoration:none;color:inherit}
.sistemas .tarjeta:hover{border-color:var(--serie)}
.sistemas .url{color:var(--serie);font-size:13px;overflow-wrap:anywhere}
"""


def pagina(titulo, cuerpo, activo="", token=None, pausado=False):
    enlaces = [("", "Resumen", "/"), ("chats", "Chats", "/chats"),
               ("escalaciones", "Escalaciones", "/escalaciones"), ("sistemas", "Mis sistemas", "/sistemas")]
    nav = "".join(f'<a href="{u}" class="{"act" if a == activo else ""}">{esc(t)}</a>' for a, t, u in enlaces)
    if token is not None:
        estado = ('<span class="etq aviso">Bot en pausa</span>' if pausado else '<span class="etq ok">Bot activo</span>')
        boton_pausa = (
            f'<form method="post" action="/pausa"><input type="hidden" name="csrf" value="{csrf_de(token)}">'
            f'<input type="hidden" name="valor" value="{0 if pausado else 1}">'
            f'<button type="submit">{"Reanudar bot" if pausado else "Pausar bot"}</button></form>')
        salir = (f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf_de(token)}">'
                 f'<button type="submit">Salir</button></form>')
        der = f'<div class="der">{estado}{boton_pausa}{salir}</div>'
    else:
        nav, der = "", ""
    return (f'<!doctype html><html lang="es"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><title>{esc(titulo)} · Mi Eco Lavado</title>'
            f'<style>{CSS}</style></head><body><header class="top"><div class="fila">'
            f'<div class="marca">Mi Eco Lavado <span>· Bot de Instagram</span></div><nav>{nav}</nav>{der}'
            f'</div></header><main>{cuerpo}</main></body></html>')


# --------------------------------------------------------------------------
# Páginas
# --------------------------------------------------------------------------


def kpi(num, titulo, sub=""):
    return (f'<div class="kpi"><div class="n">{esc(num)}</div><div class="t">{esc(titulo)}</div>'
            + (f'<div class="s">{esc(sub)}</div>' if sub else "") + "</div>")


def vista_resumen(con, periodo):
    desde, hasta, etiqueta, dias = rango_periodo(periodo)
    m = metricas(con, desde, hasta, dias)
    pend, _ = pendientes_escalacion(con)

    ops = [("hoy", "Hoy"), ("7", "7 días"), ("30", "30 días"), ("60", "60 días")]
    sel = periodo if periodo in ("hoy", "30", "60") else "7"
    chips = "".join(f'<a href="/?p={k}" class="{"act" if k == sel else ""}">{t}</a>' for k, t in ops)

    alerta = ""
    if pend:
        alerta = (f'<div class="panel esc"><h2>{len(pend)} cliente(s) esperan a una persona</h2>'
                  f'<p class="nota">El bot los dirigió al equipo. <a href="/escalaciones">Ver y marcar como atendidos</a></p></div>')

    kpis = "".join([
        kpi(n_fmt(m["n_personas"]), "Personas que escribieron", f'{m["nuevos"]} nuevas'),
        kpi(n_fmt(m["entrantes"]), "Mensajes de clientes", f'{m["salientes"]} respuestas del bot'),
        kpi(n_fmt(m["cotizaciones"]), "Cotizaciones de precio", "calculadas con tu sistema"),
        kpi(n_fmt(m["escalaciones"]), "Pasaron a una persona", f"{len(pend)} pendientes"),
        kpi(f'{m["pct_fuera_horario"]:.0f}%', "Escribieron fuera de horario",
            f'{m["fuera_horario"]} mensajes con la lavandería cerrada'),
        kpi(n_fmt(m["fuera_tema"]), "Mensajes fuera de tema", f'{m["media"]} archivos, reels o fotos'),
        kpi(n_fmt(m["errores"]), "Fallos del bot", "respuestas que no se pudieron enviar"),
        kpi(n_fmt(m["tok_in"] + m["tok_out"]), "Tokens de IA usados", f'{n_fmt(m["tok_in"])} entrada / {n_fmt(m["tok_out"])} salida'),
    ])

    hoy = datetime.now(HN_TZ).date()
    if periodo == "hoy":
        etqs = [f"{h % 12 or 12}{'a' if h < 12 else 'p'}" for h in range(24)]
        vals = [m["por_hora_hoy"].get(h, 0) for h in range(24)]
        g1 = svg_barras(etqs, vals, "mensajes", titulo="Mensajes de clientes por hora, hoy")
        t1 = "Mensajes de clientes por hora (hoy)"
        tabla1 = tabla_datos(["Hora", "Mensajes"], zip(etqs, vals))
    else:
        dts = [hoy - timedelta(days=dias - 1 - i) for i in range(dias)]
        etqs = [f"{d.day}/{d.month}" for d in dts]
        vals = [len(m["por_dia"].get(d, ())) for d in dts]
        g1 = svg_barras(etqs, vals, "personas", titulo="Personas distintas que escribieron por día")
        t1 = "Personas distintas que escribieron cada día"
        tabla1 = tabla_datos(["Día", "Personas"], zip(etqs, vals))

    hetq = [f"{h % 12 or 12}{'a' if h < 12 else 'p'}" for h in range(24)]
    hval = [m["por_hora_dia"].get(h, 0) for h in range(24)]
    g2 = svg_barras(hetq, hval, "mensajes", titulo="Mensajes por hora del día")
    tabla2 = tabla_datos(["Hora", "Mensajes"], zip(hetq, hval))

    top_prod = barras_horizontales(m["productos"].most_common(8), "veces")
    top_busq = barras_horizontales(m["busquedas"].most_common(8), "veces")

    cuerpo = (
        f'<h1>Resumen · {esc(etiqueta)}</h1><div class="periodos">{chips}</div>{alerta}'
        f'<div class="rejilla-kpi">{kpis}</div>'
        f'<div class="dos"><div class="panel"><h2>{esc(t1)}</h2>{g1}{tabla1}</div>'
        f'<div class="panel"><h2>¿A qué hora escriben?</h2>{g2}{tabla2}'
        f'<p class="nota">Hora de Honduras, todos los días del periodo juntos.</p></div></div>'
        f'<div class="dos"><div class="panel"><h2>Servicios que más se cotizaron</h2>{top_prod}</div>'
        f'<div class="panel"><h2>Lo que más buscan los clientes</h2>{top_busq}</div></div>'
        f'<p class="nota">Las conversaciones se conservan 60 días en el servidor (configurable con LOG_DIAS).</p>'
    )
    return "Resumen", cuerpo


def nombre_visible(p, uid):
    if p and (p["nombre"] or p["usuario"]):
        return p["nombre"] or "@" + p["usuario"], ("@" + p["usuario"]) if p["usuario"] else ""
    return f"Cliente …{uid[-5:]}", ""


def estado_etiquetas(u, pend_uids):
    et = []
    if u and u["bloqueado"]:
        et.append('<span class="etq mal">Bloqueado</span>')
    if u and u["handoff_hasta"] and u["handoff_hasta"] > time.time():
        et.append('<span class="etq aviso">Bot callado (atención humana)</span>')
    return "".join(et)


def vista_chats(con, q, filtro):
    pend, _ = pendientes_escalacion(con)
    pend_uids = {f["uid"] for f in pend}
    filas = con.execute(
        "SELECT uid, MAX(ts) ultimo, SUM(tipo = 'in') n_in, SUM(tipo = 'escalado') n_esc, "
        "SUM(tipo = 'fuera_tema') n_ft, SUM(tipo = 'error') n_err FROM registro "
        "WHERE tipo IN ('in','out','escalado','fuera_tema','error') GROUP BY uid ORDER BY ultimo DESC "
        "LIMIT 400").fetchall()
    perfiles = {p["uid"]: p for p in con.execute("SELECT * FROM perfiles")}
    usuarios = {u["uid"]: u for u in con.execute("SELECT * FROM usuarios")}
    ql = (q or "").strip().lower()
    hoy0 = inicio_dia(datetime.now(HN_TZ)).timestamp()
    textos = {}
    if ql:
        for f in con.execute("SELECT DISTINCT uid FROM registro WHERE tipo IN ('in','out') AND lower(texto) LIKE ?",
                             (f"%{ql}%",)):
            textos[f["uid"]] = True
    items = []
    for f in filas:
        uid = f["uid"]
        p = perfiles.get(uid)
        nombre, usuario_ig = nombre_visible(p, uid)
        if ql and not (ql in nombre.lower() or ql in usuario_ig.lower() or ql in uid or uid in textos):
            continue
        if filtro == "pendientes" and uid not in pend_uids:
            continue
        if filtro == "fuera_tema" and not f["n_ft"]:
            continue
        if filtro == "errores" and not f["n_err"]:
            continue
        if filtro == "hoy" and f["ultimo"] < hoy0:
            continue
        ult = con.execute("SELECT tipo, texto FROM registro WHERE uid = ? AND tipo IN ('in','out') "
                          "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        prev = (("Cliente: " if ult["tipo"] == "in" else "Bot: ") + (ult["texto"] or "")) if ult else ""
        et = estado_etiquetas(usuarios.get(uid), pend_uids)
        if uid in pend_uids:
            et += '<span class="etq aviso">Espera a una persona</span>'
        if f["n_err"]:
            et += '<span class="etq mal">Fallo</span>'
        sub = f' <span class="nota">{esc(usuario_ig)}</span>' if usuario_ig else ""
        items.append(
            f'<li><a class="fila-chat" href="/chat/{urllib.parse.quote(uid)}"><span class="quien">{esc(nombre)}{sub}{et}</span>'
            f'<span class="cuando">{esc(hace(f["ultimo"]))}</span>'
            f'<span class="prev">{esc(prev[:140])}</span><span class="cuando">{n_fmt(f["n_in"])} mensajes</span></a></li>')
    opciones = [("", "Todos"), ("hoy", "Con actividad hoy"), ("pendientes", "Esperan a una persona"),
                ("fuera_tema", "Con mensajes fuera de tema"), ("errores", "Con fallos")]
    sel = "".join(f'<option value="{k}"{" selected" if k == (filtro or "") else ""}>{esc(t)}</option>' for k, t in opciones)
    form = (f'<form class="busq" method="get" action="/chats"><input type="text" name="q" value="{esc(q or "")}" '
            f'placeholder="Buscar por nombre, usuario o texto"><select name="filtro">{sel}</select>'
            f'<button type="submit">Buscar</button></form>')
    lista = ('<ul class="lista">' + "".join(items) + "</ul>") if items else '<p class="vacio">No hay conversaciones con ese filtro.</p>'
    return "Chats", f'<h1>Conversaciones</h1>{form}<div class="panel">{lista}</div>'


def linea_sistema(f):
    t, x = f["tipo"], extra_de(f)
    if t == "cotizo":
        if x.get("ok"):
            cant = f' · {x["libras"]} lb' if x.get("libras") else (f' · x{x["cantidad"]}' if x.get("cantidad") else "")
            total = f' → L{x["total"]:,.2f}' if isinstance(x.get("total"), (int, float)) else ""
            return f'Cotizó: {x.get("producto") or f["texto"]}{cant}{total}'
        return f'Intentó cotizar "{f["texto"]}" (sin resultado)'
    if t == "busco":
        return f'Buscó en el catálogo: "{f["texto"]}"'
    if t == "escalado":
        return "Pasó a una persona: " + (f["texto"] or "sin motivo") + ("" if x.get("aviso") else " (sin aviso al equipo)")
    if t == "fuera_tema":
        return "Mensaje fuera de tema"
    if t == "media":
        return f'Envió un archivo ({f["texto"] or "adjunto"})'
    if t == "error":
        return "Fallo: " + (x.get("detalle") or f["texto"] or "")
    if t == "atendido":
        return "Marcado como atendido por el equipo"
    if t == "limite":
        return "Alcanzó el límite diario de mensajes"
    return ""


def vista_chat(con, uid, token):
    ev = con.execute("SELECT id, ts, uid, tipo, texto, extra FROM registro WHERE uid = ? AND tipo != 'uso' "
                     "ORDER BY ts, id", (uid,)).fetchall()
    if not ev:
        return None
    p = con.execute("SELECT * FROM perfiles WHERE uid = ?", (uid,)).fetchone()
    u = con.execute("SELECT * FROM usuarios WHERE uid = ?", (uid,)).fetchone()
    nombre, usuario_ig = nombre_visible(p, uid)
    partes = []
    dia_prev = None
    for f in ev:
        dia = a_hn(f["ts"]).date()
        if dia != dia_prev:
            partes.append(f'<div class="sis">{DIAS_CORTO[dia.weekday()]} {dia.day} {MESES_CORTO[dia.month - 1]}</div>')
            dia_prev = dia
        hora = hora12(a_hn(f["ts"]))
        if f["tipo"] in ("in", "out"):
            partes.append(f'<div class="burb {f["tipo"]}">{esc(f["texto"])}<small>'
                          f'{"Cliente" if f["tipo"] == "in" else "Bot"} · {esc(hora)}</small></div>')
        else:
            linea = linea_sistema(f)
            if linea:
                partes.append(f'<div class="sis">{esc(linea)} · {esc(hora)}</div>')
    bloqueado = bool(u and u["bloqueado"])
    callado = bool(u and u["handoff_hasta"] and u["handoff_hasta"] > time.time())

    def boton(accion, texto, clase=""):
        return (f'<form method="post" action="/accion"><input type="hidden" name="csrf" value="{csrf_de(token)}">'
                f'<input type="hidden" name="uid" value="{esc(uid)}"><input type="hidden" name="accion" value="{accion}">'
                f'<button class="{clase}" type="submit">{esc(texto)}</button></form>')
    acciones = [boton("atendido", "Marcar como atendido")]
    if callado:
        acciones.append(boton("reanudar", "Que el bot vuelva a responder"))
    acciones.append(boton("desbloquear", "Desbloquear") if bloqueado else boton("bloquear", "Bloquear (el bot no responde)", "mal"))
    cab = (f'<h1>{esc(nombre)} <span class="nota">{esc(usuario_ig)}</span></h1>'
           f'<p class="nota">ID de Instagram: {esc(uid)} · {estado_etiquetas(u, set())}</p>'
           f'<div class="acciones">{"".join(acciones)}</div><p><a href="/chats">← Volver a los chats</a></p>')
    return "Chats", cab + '<div class="panel"><div class="hilo">' + "".join(partes) + "</div></div>"


def vista_escalaciones(con, token):
    pend, hechas = pendientes_escalacion(con)
    perfiles = {p["uid"]: p for p in con.execute("SELECT * FROM perfiles")}

    def tarjeta(f, con_acciones):
        x = extra_de(f)
        nombre, usuario_ig = nombre_visible(perfiles.get(f["uid"]), f["uid"])
        filas = [("Cliente dijo llamarse", x.get("nombre")), ("Contacto", x.get("contacto")),
                 ("Motivo", x.get("motivo") or f["texto"]), ("Servicio", x.get("servicio")), ("Detalle", x.get("resumen"))]
        dl = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in filas if v)
        aviso = "" if x.get("aviso") else '<span class="etq mal">No se avisó al equipo (sin Telegram)</span>'
        acc = ""
        if con_acciones:
            acc = (f'<div class="acciones"><a class="btn" href="/chat/{urllib.parse.quote(f["uid"])}">Ver conversación</a>'
                   f'<form method="post" action="/accion"><input type="hidden" name="csrf" value="{csrf_de(token)}">'
                   f'<input type="hidden" name="uid" value="{esc(f["uid"])}"><input type="hidden" name="accion" value="atendido">'
                   f'<input type="hidden" name="next" value="/escalaciones"><button class="pri" type="submit">Marcar como atendido</button></form></div>')
        return (f'<div class="panel esc"><strong>{esc(nombre)}</strong> <span class="nota">{esc(usuario_ig)} · {esc(fmt_ts(f["ts"]))} '
                f'({esc(hace(f["ts"]))})</span> {aviso}<dl>{dl}</dl>{acc}</div>')
    p1 = "".join(tarjeta(f, True) for f in pend) or '<p class="vacio">No hay nadie esperando. Todo al día.</p>'
    p2 = "".join(tarjeta(f, False) for f in hechas[:15])
    nota = ('<p class="nota">Si Telegram no está configurado, el equipo no recibe aviso: aquí es donde se ven los clientes '
            'que el bot pasó a una persona. El bot les da el teléfono +504 3227-5543.</p>')
    cuerpo = (f'<h1>Escalaciones</h1>{nota}<h2>Esperan a una persona ({len(pend)})</h2>{p1}'
              + (f'<h2>Atendidas recientemente</h2>{p2}' if p2 else ""))
    return "Escalaciones", cuerpo


def cargar_sistemas():
    try:
        with open(CFG.sistemas_file, encoding="utf-8") as f:
            datos = json.load(f)
        return [s for s in datos if isinstance(s, dict)]
    except (OSError, ValueError):
        return []


def vista_sistemas():
    tarjetas = []
    for s in cargar_sistemas():
        url = str(s.get("url") or "")
        seguro = url.startswith(("https://", "http://"))
        cuerpo = (f'<strong>{esc(s.get("nombre"))}</strong><div class="nota">{esc(s.get("descripcion"))}</div>'
                  f'<div class="url">{esc(url)}</div>'
                  + (f'<div class="nota">{esc(s.get("servidor"))}</div>' if s.get("servidor") else ""))
        if seguro:
            tarjetas.append(f'<a class="tarjeta" href="{esc(url)}" target="_blank" rel="noopener noreferrer">{cuerpo}</a>')
        else:
            tarjetas.append(f'<div class="tarjeta">{cuerpo}</div>')
    lista = ('<div class="sistemas">' + "".join(tarjetas) + "</div>") if tarjetas else \
        '<p class="vacio">Agrega tus sistemas en sistemas.json.</p>'
    return "Mis sistemas", f'<h1>Mis sistemas</h1>{lista}'


# --------------------------------------------------------------------------
# Servidor
# --------------------------------------------------------------------------

CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
       "base-uri 'none'; frame-ancestors 'none'")


def parse_cookie(cabecera):
    out = {}
    for parte in (cabecera or "").split(";"):
        if "=" in parte:
            k, v = parte.strip().split("=", 1)
            out[k] = v
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "PanelEcoLavado"

    def log_message(self, fmt, *args):  # silencio: no guardamos rutas con datos de clientes
        pass

    # --- utilidades ---
    def ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff else self.client_address[0]) or "?"

    def token(self):
        t = parse_cookie(self.headers.get("Cookie")).get("sesion")
        return t if sesion_valida(t) else None

    def enviar(self, codigo, cuerpo, tipo="text/html; charset=utf-8", extra=None):
        datos = cuerpo.encode("utf-8") if isinstance(cuerpo, str) else cuerpo
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(datos)

    def redirigir(self, destino, extra=None):
        self.send_response(303)
        self.send_header("Location", destino)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()

    def cookie(self, valor, borrar=False):
        partes = [f"sesion={valor}", "Path=/", "HttpOnly", "SameSite=Strict",
                  f"Max-Age={0 if borrar else CFG.sesion_dias * 86400}"]
        if CFG.cookie_secure:
            partes.append("Secure")
        return ("Set-Cookie", "; ".join(partes))

    def form(self):
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > 20_000:
            return {}
        crudo = self.rfile.read(largo).decode("utf-8", "replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(crudo).items()}

    def login_html(self, error=""):
        cuerpo = (f'<div class="login"><h1>Entrar al panel</h1><form method="post" action="/login">'
                  f'<p><input type="password" name="clave" placeholder="Contraseña" autofocus required '
                  f'style="width:100%"></p>' + (f'<p class="err">{esc(error)}</p>' if error else "")
                  + '<p><button class="pri" type="submit">Entrar</button></p></form></div>')
        return pagina("Entrar", cuerpo)

    def pausado(self, con):
        f = con.execute("SELECT valor FROM ajustes WHERE clave = 'pausado'").fetchone()
        return bool(f and f["valor"] == "1")

    # --- rutas ---
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        ruta, q = url.path, {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if ruta == "/salud":
            return self.enviar(200, "ok", "text/plain; charset=utf-8")
        tok = self.token()
        if ruta == "/login":
            return self.enviar(200, self.login_html())
        if not tok:
            return self.redirigir("/login")
        try:
            with db() as con:
                if ruta == "/":
                    titulo, cuerpo = vista_resumen(con, q.get("p", "7"))
                    activo = ""
                elif ruta == "/chats":
                    titulo, cuerpo = vista_chats(con, q.get("q", ""), q.get("filtro", ""))
                    activo = "chats"
                elif ruta.startswith("/chat/"):
                    r = vista_chat(con, urllib.parse.unquote(ruta[6:]), tok)
                    if r is None:
                        return self.enviar(404, pagina("No encontrado", "<h1>No encontrado</h1>", "", tok, False))
                    titulo, cuerpo = r
                    activo = "chats"
                elif ruta == "/escalaciones":
                    titulo, cuerpo = vista_escalaciones(con, tok)
                    activo = "escalaciones"
                elif ruta == "/sistemas":
                    titulo, cuerpo = vista_sistemas()
                    activo = "sistemas"
                else:
                    return self.enviar(404, pagina("No encontrado", "<h1>No encontrado</h1>", "", tok, False))
                pausado = self.pausado(con)
            self.enviar(200, pagina(titulo, cuerpo, activo, tok, pausado))
        except Exception:
            log.exception("Error en %s", ruta)
            self.enviar(500, pagina("Error", "<h1>Algo falló</h1><p>Revisa el registro del servidor.</p>", "", tok, False))

    def do_POST(self):
        ruta = urllib.parse.urlparse(self.path).path
        datos = self.form()
        if ruta == "/login":
            ip = self.ip()
            if login_bloqueado(ip):
                return self.enviar(429, self.login_html("Demasiados intentos. Espera unos minutos."))
            ok = bool(CFG.password) and hmac.compare_digest(datos.get("clave", ""), CFG.password)
            registrar_intento(ip, ok)
            if not ok:
                time.sleep(PAUSA_FALLO)
                return self.enviar(401, self.login_html("Contraseña incorrecta."))
            return self.redirigir("/", [self.cookie(crear_sesion())])
        tok = self.token()
        if not tok:
            return self.redirigir("/login")
        if not hmac.compare_digest(datos.get("csrf", ""), csrf_de(tok)):
            return self.enviar(403, "Solicitud no válida", "text/plain; charset=utf-8")
        if ruta == "/logout":
            return self.redirigir("/login", [self.cookie("", borrar=True)])
        if ruta == "/pausa":
            with db() as con:
                con.execute("INSERT OR REPLACE INTO ajustes(clave, valor) VALUES ('pausado', ?)",
                            ("1" if datos.get("valor") == "1" else "0",))
            return self.redirigir("/")
        if ruta == "/accion":
            uid, accion = datos.get("uid", ""), datos.get("accion", "")
            if not uid or accion not in ("atendido", "reanudar", "bloquear", "desbloquear"):
                return self.enviar(400, "Solicitud no válida", "text/plain; charset=utf-8")
            with db() as con:
                con.execute("INSERT OR IGNORE INTO usuarios(uid) VALUES (?)", (uid,))
                if accion == "atendido":
                    con.execute("INSERT INTO registro(ts, uid, tipo, texto) VALUES (?, ?, 'atendido', '')",
                                (time.time(), uid))
                elif accion == "reanudar":
                    con.execute("UPDATE usuarios SET handoff_hasta = 0 WHERE uid = ?", (uid,))
                else:
                    con.execute("UPDATE usuarios SET bloqueado = ? WHERE uid = ?",
                                (1 if accion == "bloquear" else 0, uid))
            destino = datos.get("next") if datos.get("next") in ("/escalaciones", "/chats") else \
                "/chat/" + urllib.parse.quote(uid)
            return self.redirigir(destino)
        self.enviar(404, "No encontrado", "text/plain; charset=utf-8")


def main():
    cargar_env_file()
    global CFG
    CFG = Config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(CFG.password) < 8:
        sys.exit("Falta PANEL_PASSWORD en el .env (mínimo 8 caracteres).")
    asegurar_tablas()
    log.info("Panel escuchando en el puerto %s", CFG.puerto)
    ThreadingHTTPServer(("127.0.0.1", CFG.puerto), Handler).serve_forever()


if __name__ == "__main__":
    main()
