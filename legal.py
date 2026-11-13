"""Páginas públicas que Meta exige para publicar la app: política de privacidad y eliminación de datos."""

_BASE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{titulo} · Mi Eco Lavado</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:720px;margin:32px auto;padding:0 16px;color:#111}}
h1{{font-size:24px}}h2{{font-size:18px;margin-top:26px}}</style></head><body>{cuerpo}</body></html>"""

PRIVACIDAD = _BASE.format(titulo="Política de privacidad", cuerpo="""
<h1>Política de privacidad</h1>
<p>Mi Eco Lavado (lavandería en Plaza McDonald's Las Torres, Tegucigalpa, Honduras) usa un asistente virtual
para responder los mensajes directos de Instagram (@miecolavado). Última actualización: noviembre de 2026.</p>
<h2>Qué información recibimos</h2>
<p>Cuando usted nos escribe por Instagram recibimos el contenido de sus mensajes, su identificador de Instagram y,
si Instagram lo permite, su nombre y nombre de usuario. Si usted nos lo da, también su nombre y número de teléfono
para que nuestro personal pueda contactarle.</p>
<h2>Para qué la usamos</h2>
<p>Únicamente para responder sus consultas sobre servicios, precios, horarios y ubicación, y para pasar su caso a
una persona de nuestro equipo cuando sea necesario. No vendemos ni compartimos su información con terceros para
publicidad.</p>
<h2>Procesamiento con inteligencia artificial</h2>
<p>Sus mensajes se procesan con un servicio de inteligencia artificial para redactar las respuestas. Las respuestas
las genera un asistente automático, y siempre puede hablar con una persona por WhatsApp.</p>
<h2>Cuánto tiempo la conservamos</h2>
<p>El contexto de la conversación se conserva unos 3 días y el historial de atención hasta 60 días. Después se
elimina automáticamente.</p>
<h2>Sus derechos</h2>
<p>Puede pedirnos que eliminemos sus datos en cualquier momento. Vea las instrucciones en
<a href="/eliminar-datos">Eliminación de datos</a>.</p>
<h2>Contacto</h2>
<p>WhatsApp / teléfono: +504 3227-5543 · Instagram: @miecolavado</p>
""")

ELIMINAR = _BASE.format(titulo="Eliminación de datos", cuerpo="""
<h1>Eliminación de datos</h1>
<p>Si desea que eliminemos los mensajes y datos que guardamos de su conversación con nuestro asistente de Instagram:</p>
<ol>
<li>Escríbanos por WhatsApp al +504 3227-5543 o por mensaje directo a @miecolavado indicando
"Eliminar mis datos" y su usuario de Instagram.</li>
<li>Eliminaremos su información dentro de los 30 días siguientes y se lo confirmaremos.</li>
</ol>
<p>Los datos se eliminan de todas formas de forma automática: el contexto a los 3 días y el historial de atención
a los 60 días.</p>
""")

TERMINOS = _BASE.format(titulo="Términos del servicio", cuerpo="""
<h1>Términos del servicio</h1>
<p>Estos términos aplican al uso del asistente virtual de Mi Eco Lavado en Instagram (@miecolavado).
Última actualización: noviembre de 2026.</p>
<h2>Qué es el asistente</h2>
<p>Es un asistente automático que responde consultas generales sobre nuestros servicios, precios, horarios y
ubicación. Sus respuestas son informativas.</p>
<h2>Precios y servicios</h2>
<p>Los precios que el asistente indica son referenciales y se confirman al momento de recibir la ropa, según el peso
real o la prenda. Las condiciones de cada orden se rigen por las políticas de Mi Eco Lavado vigentes en la
lavandería.</p>
<h2>Atención de una persona</h2>
<p>Para reclamos, reembolsos, solicitudes especiales o cualquier asunto que requiera autorización, nuestro
personal le atenderá por WhatsApp al +504 3227-5543. El asistente no puede aceptar ni resolver reclamos.</p>
<h2>Uso adecuado</h2>
<p>Le pedimos usar el servicio de forma respetuosa. Podemos dejar de responder a cuentas que envíen spam o
contenido abusivo.</p>
<h2>Privacidad</h2>
<p>El manejo de su información se explica en nuestra <a href="/privacidad">política de privacidad</a>.</p>
<h2>Contacto</h2>
<p>WhatsApp / teléfono: +504 3227-5543 · Instagram: @miecolavado</p>
""")
