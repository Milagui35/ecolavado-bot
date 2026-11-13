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
        bot.guardar_perfil = lambda uid: None
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
        bot.CFG.max_msgs_dia = 3
        self.claude_script = [self.texto("1"), self.texto("2"), self.texto("NO DEBE USARSE")]
        for i in range(5):
            bot.procesar_evento(self.evento(texto=f"m{i}"))
        # 2 respuestas normales + 1 último mensaje con el WhatsApp; después, silencio
        self.assertEqual(len(self.enviados), 3)
        self.assertEqual([e[1] for e in self.enviados[:2]], ["1", "2"])
        self.assertEqual(self.enviados[2][1], bot.MSG_LIMITE)
        self.assertIn("+504 3227-5543", self.enviados[2][1])

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
        r = bot.tool_politicas()
        self.assertTrue(r["ok"])
        self.assertIn("Revise su ropa.", r["politicas"])

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


class TestAmbiguo(Base):
    def test_libras_resuelve_unico_por_libra(self):
        cat = {"productos": [
            {"id": 68, "nombre": "Solo Lavado LBS", "tipo": "por_libra"},
            {"id": 71, "nombre": "Solo Lavado- Edredon Queen", "tipo": "fijo", "precio": 250.0}]}
        llamadas = []

        def sget(ruta):
            return 200, cat

        def spost(ruta, datos):
            llamadas.append(datos)
            if "producto_id" in datos:
                return 200, {"ok": True, "producto": "Solo Lavado LBS", "total": 375.0}
            return 200, {"ok": False, "error": "Hay varios servicios que coinciden; indique cuál.",
                         "candidatos": [{"id": 68, "nombre": "Solo Lavado LBS"},
                                        {"id": 71, "nombre": "Solo Lavado- Edredon Queen"}]}
        bot.sistema_get, bot.sistema_post = sget, spost
        r = bot.tool_cotizar({"servicio": "Solo Lavado", "libras": 15})
        self.assertTrue(r["ok"])
        self.assertEqual(llamadas[-1], {"producto_id": 68, "libras": 15})

    def _montar(self, precios):
        cat = {"productos": [{"id": 71, "nombre": "Solo Lavado- Edredon Queen", "tipo": "fijo", "precio": precios[0]},
                             {"id": 3, "nombre": "FULL SERVICE- Edredon Queen", "tipo": "fijo", "precio": precios[1]}]}
        llamadas = []

        def spost(ruta, datos):
            llamadas.append(datos)
            if "producto_id" in datos:
                return 200, {"ok": True, "total": 250.0 * datos["cantidad"]}
            return 200, {"ok": False, "candidatos": [{"id": 71}, {"id": 3}]}
        bot.sistema_get, bot.sistema_post = (lambda r: (200, cat)), spost
        return llamadas

    def test_edredon_mismo_precio_se_cotiza_solo(self):
        llamadas = self._montar([250.0, 250.0])
        r = bot.tool_cotizar({"servicio": "Edredon Queen", "cantidad": 2})
        self.assertTrue(r["ok"])
        self.assertEqual(llamadas[-1], {"producto_id": 71, "cantidad": 2})

    def test_edredon_distinto_precio_sigue_ambiguo(self):
        self._montar([250.0, 300.0])
        r = bot.tool_cotizar({"servicio": "Edredon Queen", "cantidad": 1})
        self.assertFalse(r["ok"])


class TestFormatoAlterno(Base):
    def test_changes_messages_se_procesa(self):
        self.claude_script = [self.texto("Hola, bienvenido.")]
        ev = self.evento(texto="Hola")
        bot.procesar_payload({"object": "instagram", "entry": [
            {"changes": [{"field": "messages", "value": ev}]}]})
        self.assertEqual(self.enviados[0][1], "Hola, bienvenido.")


class TestRegistro(Base):
    def tipos(self):
        with bot.db() as con:
            return [f["tipo"] for f in con.execute("SELECT tipo FROM registro ORDER BY id")]

    def test_registra_entrada_salida_y_uso(self):
        self.claude_script = [dict(self.texto("Con gusto."), usage={"input_tokens": 10, "output_tokens": 5})]
        bot.procesar_evento(self.evento(texto="Hola"))
        self.assertIn("in", self.tipos())
        self.assertIn("out", self.tipos())
        self.assertIn("uso", self.tipos())

    def test_pausa_global_no_responde(self):
        with bot.db() as con:
            con.execute("INSERT OR REPLACE INTO ajustes(clave, valor) VALUES ('pausado', '1')")
        bot.procesar_evento(self.evento(texto="Hola"))
        self.assertEqual(self.enviados, [])

    def test_purga_registro_viejo(self):
        bot.CFG.log_dias = 1
        with bot.db() as con:
            con.execute("INSERT INTO registro(ts, uid, tipo) VALUES (?, 'x', 'in')", (time.time() - 5 * 86400,))
        bot.log_evento("y", "in", "reciente")
        bot.purgar()
        with bot.db() as con:
            n = con.execute("SELECT COUNT(*) c FROM registro").fetchone()["c"]
        self.assertEqual(n, 1)
