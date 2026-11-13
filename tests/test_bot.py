import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["IG_APP_SECRET"] = "secreto"
os.environ["IG_VERIFY_TOKEN"] = "verif"

import bot  # noqa: E402
import prompt  # noqa: E402

CATALOGO = {"productos": [
    {"id": 1, "name": "Edredon Queen - Full Service", "price": 400.0, "price_mode": "fixed"},
    {"id": 2, "name": "Solo Lavado", "price_mode": "by_lb",
     "tabla_libras_precio": [[1, 90], [2, 180], [15, 375]]},
    {"id": 3, "name": "Dry Clean - Saco", "price": 350.0, "es_precio_base": True,
     "nota": "base"},
]}


class Base(unittest.TestCase):
    def setUp(self):
        bot.CFG = bot.Config()
        bot.CFG.db_path = os.path.join(tempfile.mkdtemp(), "t.db")
        bot.CFG.max_msgs_dia = 30
        bot._cache.clear()
        bot.init_db()
        self.enviados = []
        self.avisos = []
        self.llamadas_sistema = []
        self.claude_script = []
        bot.enviar_ig = lambda uid, t: (self.enviados.append((uid, t)) or True)
        bot.telegram = lambda t: (self.avisos.append(t) or True)

        def sget(ruta):
            self.llamadas_sistema.append(("GET", ruta))
            if ruta == "/api/catalogo":
                return 200, CATALOGO
            if ruta == "/api/politicas":
                return 200, {"politicas": [{"numero": 1, "texto": "Revise su ropa."}]}
            return 404, {}

        def spost(ruta, datos):
            self.llamadas_sistema.append(("POST", ruta, datos))
            return 200, {"ok": True, "total": 375, "isv_incluido": 48.91}

        bot.sistema_get, bot.sistema_post = sget, spost
        self.vistos_msgs = []

        def fake_claude(system, messages, tools):
            self.vistos_msgs.append((system, json.loads(json.dumps(messages))))
            return self.claude_script.pop(0)

        bot.claude = fake_claude

    def evento(self, uid="u1", texto="hola", mid=None, attachments=None, echo=False):
        m = {"mid": mid or f"m{time.time_ns()}"}
        if texto is not None:
            m["text"] = texto
        if attachments:
            m["attachments"] = [{"type": t} for t in attachments]
        if echo:
            m["is_echo"] = True
        return {"sender": {"id": uid}, "message": m}

    @staticmethod
    def texto(t):
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": t}]}

    @staticmethod
    def uso(nombre, args, id_="t1"):
        return {"stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": id_, "name": nombre, "input": args}]}


class TestSeguridad(Base):
    def test_firma(self):
        cuerpo = b'{"a":1}'
        buena = "sha256=" + hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
        self.assertTrue(bot.firma_valida(cuerpo, buena))
        self.assertFalse(bot.firma_valida(cuerpo, "sha256=00"))
        self.assertFalse(bot.firma_valida(cuerpo, None))
        self.assertFalse(bot.firma_valida(b"otro", buena))


class TestFlujo(Base):
    def test_respuesta_simple(self):
        self.claude_script = [self.texto("¡Bienvenido!")]
        bot.procesar_evento(self.evento(texto="hola"))
        self.assertEqual(self.enviados, [("u1", "¡Bienvenido!")])

    def test_duplicado(self):
        self.claude_script = [self.texto("a"), self.texto("b")]
        e = self.evento(mid="igual")
        bot.procesar_evento(e)
        bot.procesar_evento(e)
        self.assertEqual(len(self.enviados), 1)

    def test_eco_y_reels_ignorados(self):
        bot.procesar_evento(self.evento(echo=True))
        bot.procesar_evento(self.evento(texto=None, attachments=["ig_reel"]))
        bot.procesar_evento(self.evento(texto=None, attachments=["share"]))
        self.assertEqual(self.enviados, [])

    def test_foto_avisa_una_vez(self):
        bot.procesar_evento(self.evento(texto=None, attachments=["image"]))
        bot.procesar_evento(self.evento(texto=None, attachments=["image"]))
        self.assertEqual(len(self.enviados), 1)
        self.assertIn("revisará", self.enviados[0][1])

    def test_historial_se_envia(self):
        self.claude_script = [self.texto("uno"), self.texto("dos")]
        bot.procesar_evento(self.evento(texto="primero"))
        bot.procesar_evento(self.evento(texto="segundo"))
        msgs = self.vistos_msgs[1][1]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])

    def test_limite_diario(self):
        bot.CFG.max_msgs_dia = 2
        self.claude_script = [self.texto("1"), self.texto("2"), self.texto("3")]
        for i in range(4):
            bot.procesar_evento(self.evento(texto=f"m{i}"))
        self.assertEqual(len(self.enviados), 2)

    def test_bloqueado(self):
        bot.set_usuario("u1", bloqueado=1)
        bot.procesar_evento(self.evento())
        self.assertEqual(self.enviados, [])

    def test_error_de_claude_da_telefono(self):
        def roto(*a, **k):
            raise RuntimeError("caído")
        bot.claude = roto
        bot.procesar_evento(self.evento())
        self.assertIn("+504 3227-5543", self.enviados[0][1])

    def test_bot_apagado(self):
        bot.CFG.bot_activo = False
        bot.procesar_evento(self.evento())
        self.assertEqual(self.enviados, [])


class TestHerramientas(Base):
    def test_cotizar_llega_al_sistema(self):
        self.claude_script = [self.uso("cotizar", {"servicio": "Solo Lavado", "libras": 15}),
                              self.texto("Son L375 con ISV.")]
        bot.procesar_evento(self.evento(texto="15 libras de lavado?"))
        self.assertIn(("POST", "/api/cotizar", {"servicio": "Solo Lavado", "libras": 15}),
                      self.llamadas_sistema)
        self.assertEqual(self.enviados[0][1], "Son L375 con ISV.")
        # el resultado de la herramienta se le devolvió a Claude
        ultimo = self.vistos_msgs[1][1][-1]
        self.assertEqual(ultimo["content"][0]["type"], "tool_result")
        self.assertIn("375", ultimo["content"][0]["content"])

    def test_catalogo_acentos_y_tablas(self):
        r = bot.tool_buscar_catalogo("edredón")
        self.assertEqual(r["coincidencias"], 1)
        r = bot.tool_buscar_catalogo("solo lavado")
        self.assertNotIn("tabla_libras_precio", r["productos"][0])
        self.assertIn("cobro_por_peso", r["productos"][0])

    def test_drycleaner_precio_base(self):
        r = bot.tool_buscar_catalogo("dry clean")["productos"][0]
        self.assertEqual(r["price"], 350.0)
        self.assertIn("BASE", r["aviso"])

    def test_drycleaner_sin_precio(self):
        bot.CFG.dryclean_precio_base = False
        r = bot.tool_buscar_catalogo("dry clean")["productos"][0]
        self.assertNotIn("price", r)
        self.assertIn("no dar precio", r["aviso"])

    def test_politicas(self):
        self.assertEqual(bot.tool_politicas()["politicas"][0]["numero"], 1)

    def test_sistema_caido(self):
        bot.sistema_get = lambda r: (500, {})
        r = bot.tool_buscar_catalogo("x")
        self.assertFalse(r["ok"])


class TestEscalamiento(Base):
    def test_enviado_activa_silencio(self):
        self.claude_script = [
            self.uso("escalar_a_humano", {"nombre": "Ana", "contacto": "9999-0000",
                                           "motivo": "Prenda dañada"}),
            self.texto("Su consulta fue enviada.")]
        bot.procesar_evento(self.evento(texto="me dañaron un vestido"))
        self.assertEqual(len(self.avisos), 1)
        self.assertIn("NUEVA CONSULTA PARA ATENCIÓN HUMANA", self.avisos[0])
        self.assertIn("Ana", self.avisos[0])
        # mientras lo atiende una persona, el bot calla
        bot.procesar_evento(self.evento(texto="hola?"))
        self.assertEqual(len(self.enviados), 1)

    def test_falla_no_afirma_envio(self):
        bot.telegram = lambda t: False
        self.claude_script = [self.uso("escalar_a_humano", {"motivo": "reembolso"}),
                              self.texto("Llame al +504 3227-5543")]
        bot.procesar_evento(self.evento(texto="quiero reembolso"))
        resultado = self.vistos_msgs[1][1][-1]["content"][0]["content"]
        self.assertIn('"enviado": false', resultado)
        self.assertIn("NO digas que se envió", resultado)
        # y no queda en modo silencio
        self.assertEqual(bot.usuario("u1")["handoff_hasta"], 0)


class TestFueraDeTema(Base):
    def test_segunda_vez_silencio(self):
        self.claude_script = [self.uso("marcar_fuera_de_tema", {}),
                              self.texto("Le ayudo con servicios u horarios."),
                              self.uso("marcar_fuera_de_tema", {}),
                              self.texto("texto que NO debe enviarse")]
        bot.procesar_evento(self.evento(texto="cuéntame un chiste"))
        bot.procesar_evento(self.evento(texto="otro chiste"))
        self.assertEqual(len(self.enviados), 1)


class TestHorario(unittest.TestCase):
    def hn(self, y, m, d, h, mi=0):
        return datetime(y, m, d, h, mi, tzinfo=prompt.HN_TZ)

    def test_domingo_antes_y_despues(self):
        self.assertIn("NO atiende", prompt.contexto_tiempo(self.hn(2026, 11, 8, 10)))
        self.assertIn("ABIERTA", prompt.contexto_tiempo(self.hn(2026, 11, 15, 10)))
        self.assertIn("CERRADA", prompt.contexto_tiempo(self.hn(2026, 11, 15, 19)))

    def test_sabado_y_tarde(self):
        self.assertIn("CERRADA", prompt.contexto_tiempo(self.hn(2026, 11, 14, 15)))
        t = prompt.contexto_tiempo(self.hn(2026, 11, 12, 17))
        self.assertIn("ABIERTA", t)
        self.assertIn("4:30", t)

    def test_prompt_dryclean(self):
        self.assertIn("precio base", prompt.construir_prompt(True))
        self.assertIn("NO des ningún precio", prompt.construir_prompt(False))


class TestUtilidades(unittest.TestCase):
    def test_partir(self):
        largo = ("linea " * 60 + "\n") * 6
        trozos = bot.partir(largo, 900)
        self.assertTrue(all(len(t) <= 900 for t in trozos))
        self.assertGreater(len(trozos), 1)
        self.assertEqual(bot.partir("   "), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
