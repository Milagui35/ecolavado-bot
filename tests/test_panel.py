import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot  # noqa: E402
import panel  # noqa: E402

CLAVE = "claveSegura123"


def sembrar(ruta_db):
    """Datos de ejemplo en la base (misma estructura que crea el bot)."""
    bot.CFG = bot.Config()
    bot.CFG.db_path = ruta_db
    bot.init_db()
    ahora = time.time()
    ev = [
        (ahora - 3600, "111", "in", "Hola, cuánto cuesta lavar 15 libras?", None),
        (ahora - 3590, "111", "busco", "lavado", None),
        (ahora - 3585, "111", "cotizo", "Full Service", {"ok": True, "producto": "Full Service - Lavado y Empacado",
                                                          "libras": 15, "total": 510.0}),
        (ahora - 3580, "111", "uso", "", {"i": 1200, "o": 150}),
        (ahora - 3580, "111", "out", "Son L510.00 con ISV.", None),
        (ahora - 7200, "222", "in", "<script>alert(1)</script> quiero hablar con alguien", None),
        (ahora - 7195, "222", "escalado", "reclamo", {"nombre": "Ana", "contacto": "9999-0000", "motivo": "reclamo",
                                                    "aviso": False}),
        (ahora - 86400 * 2, "333", "in", "Hola", None),
        (ahora - 86400 * 2, "333", "fuera_tema", "", {"veces": 1}),
        (ahora - 86400 * 2, "333", "error", "claude", {"detalle": "boom"}),
    ]
    with bot.db() as con:
        for ts, uid, tipo, texto, extra in ev:
            con.execute("INSERT INTO registro(ts, uid, tipo, texto, extra) VALUES (?, ?, ?, ?, ?)",
                        (ts, uid, tipo, texto, json.dumps(extra) if extra else None))
        con.execute("INSERT INTO perfiles(uid, nombre, usuario, ts) VALUES ('111', 'María López', 'maria', ?)", (ahora,))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ruta = os.path.join(self.dir, "t.db")
        sembrar(self.ruta)
        panel.CFG = panel.Config()
        panel.CFG.password = CLAVE
        panel.CFG.db_path = self.ruta
        panel.CFG.cookie_secure = False
        panel.CFG.sistemas_file = os.path.join(os.path.dirname(__file__), "..", "sistemas.json")
        panel.PAUSA_FALLO = 0
        panel._intentos.clear()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.puerto = self.srv.server_address[1]
        self.cookie = None

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def pedir(self, metodo, ruta, datos=None, cookie=True, ip=None):
        con = http.client.HTTPConnection("127.0.0.1", self.puerto, timeout=10)
        h = {}
        if cookie and self.cookie:
            h["Cookie"] = "sesion=" + self.cookie
        if ip:
            h["X-Forwarded-For"] = ip
        cuerpo = None
        if datos is not None:
            cuerpo = urllib.parse.urlencode(datos)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        con.request(metodo, ruta, body=cuerpo, headers=h)
        r = con.getresponse()
        texto = r.read().decode("utf-8")
        return r.status, dict(r.getheaders()), texto

    def entrar(self):
        st, hd, _ = self.pedir("POST", "/login", {"clave": CLAVE}, cookie=False)
        self.assertEqual(st, 303)
        self.cookie = hd["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def csrf(self):
        return panel.csrf_de(self.cookie)


class TestAcceso(Base):
    def test_sin_sesion_redirige_a_login(self):
        st, hd, _ = self.pedir("GET", "/", cookie=False)
        self.assertEqual((st, hd["Location"]), (303, "/login"))

    def test_login_malo_y_bueno(self):
        st, _, t = self.pedir("POST", "/login", {"clave": "mala"}, cookie=False)
        self.assertEqual(st, 401)
        self.entrar()
        st, hd, t = self.pedir("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("Resumen", t)
        self.assertIn("default-src 'none'", hd["Content-Security-Policy"])

    def test_bloqueo_por_intentos(self):
        for _ in range(panel.MAX_FALLOS):
            self.pedir("POST", "/login", {"clave": "x"}, cookie=False, ip="9.9.9.9")
        st, _, _ = self.pedir("POST", "/login", {"clave": CLAVE}, cookie=False, ip="9.9.9.9")
        self.assertEqual(st, 429)
        st, _, _ = self.pedir("POST", "/login", {"clave": CLAVE}, cookie=False, ip="8.8.8.8")
        self.assertEqual(st, 303)

    def test_cookie_falsa_no_sirve(self):
        self.cookie = "9999999999.abc"
        st, hd, _ = self.pedir("GET", "/chats")
        self.assertEqual(st, 303)

    def test_post_sin_csrf_se_rechaza(self):
        self.entrar()
        st, _, _ = self.pedir("POST", "/pausa", {"valor": "1"})
        self.assertEqual(st, 403)
        with bot.db() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM ajustes WHERE clave='pausado'").fetchone())

    def test_salud_sin_login(self):
        st, _, t = self.pedir("GET", "/salud", cookie=False)
        self.assertEqual((st, t), (200, "ok"))


class TestPaginas(Base):
    def setUp(self):
        super().setUp()
        self.entrar()

    def test_resumen_muestra_cifras(self):
        st, _, t = self.pedir("GET", "/?p=7")
        self.assertEqual(st, 200)
        self.assertIn("Personas que escribieron", t)
        self.assertIn("Full Service - Lavado y Empacado", t)
        self.assertIn("<svg", t)

    def test_periodos_validos(self):
        for p in ("hoy", "7", "30", "60", "raro"):
            st, _, _ = self.pedir("GET", f"/?p={p}")
            self.assertEqual(st, 200, p)

    def test_chats_y_busqueda(self):
        st, _, t = self.pedir("GET", "/chats")
        self.assertIn("María López", t)
        st, _, t = self.pedir("GET", "/chats?q=maria")
        self.assertIn("María López", t)
        self.assertNotIn("…222", t)
        st, _, t = self.pedir("GET", "/chats?filtro=pendientes")
        self.assertIn("Espera a una persona", t)

    def test_xss_en_mensajes_de_clientes(self):
        for ruta in ("/chat/222", "/chats", "/escalaciones"):
            st, _, t = self.pedir("GET", ruta)
            self.assertNotIn("<script>alert(1)</script>", t, ruta)
        st, _, t = self.pedir("GET", "/chat/222")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", t)

    def test_chat_inexistente(self):
        st, _, _ = self.pedir("GET", "/chat/nadie")
        self.assertEqual(st, 404)

    def test_sistemas(self):
        st, _, t = self.pedir("GET", "/sistemas")
        self.assertIn("Mi Eco Lavado", t)
        self.assertIn('rel="noopener noreferrer"', t)


class TestAcciones(Base):
    def setUp(self):
        super().setUp()
        self.entrar()

    def test_marcar_atendido_quita_pendiente(self):
        with bot.db() as con:
            pend, _ = panel.pendientes_escalacion(con)
        self.assertEqual(len(pend), 1)
        st, _, _ = self.pedir("POST", "/accion", {"csrf": self.csrf(), "uid": "222", "accion": "atendido",
                                                  "next": "/escalaciones"})
        self.assertEqual(st, 303)
        with bot.db() as con:
            pend, hechas = panel.pendientes_escalacion(con)
        self.assertEqual((len(pend), len(hechas)), (0, 1))

    def test_bloquear_reanudar_y_pausa(self):
        self.pedir("POST", "/accion", {"csrf": self.csrf(), "uid": "111", "accion": "bloquear"})
        with bot.db() as con:
            self.assertEqual(con.execute("SELECT bloqueado FROM usuarios WHERE uid='111'").fetchone()[0], 1)
            con.execute("UPDATE usuarios SET handoff_hasta = ? WHERE uid='111'", (time.time() + 999,))
        self.pedir("POST", "/accion", {"csrf": self.csrf(), "uid": "111", "accion": "reanudar"})
        with bot.db() as con:
            self.assertEqual(con.execute("SELECT handoff_hasta FROM usuarios WHERE uid='111'").fetchone()[0], 0)
        self.pedir("POST", "/pausa", {"csrf": self.csrf(), "valor": "1"})
        bot.CFG.db_path = self.ruta
        self.assertEqual(bot.get_ajuste("pausado"), "1")

    def test_accion_invalida(self):
        st, _, _ = self.pedir("POST", "/accion", {"csrf": self.csrf(), "uid": "111", "accion": "borrar"})
        self.assertEqual(st, 400)


class TestMetricas(Base):
    def test_cuentas(self):
        desde, hasta, _, dias = panel.rango_periodo("7")
        with bot.db() as con:
            m = panel.metricas(con, desde, hasta, dias)
        self.assertEqual(m["n_personas"], 3)
        self.assertEqual(m["cotizaciones"], 1)
        self.assertEqual(m["escalaciones"], 1)
        self.assertEqual(m["errores"], 1)
        self.assertEqual(m["tok_in"], 1200)
        self.assertEqual(m["productos"]["Full Service - Lavado y Empacado"], 1)


if __name__ == "__main__":
    unittest.main()
