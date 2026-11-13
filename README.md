# Bot de Instagram - Mi Eco Lavado

Responde los mensajes directos de Instagram con Claude Haiku 5.5. Los precios y las políticas
los lee de su sistema (solo lectura): `/api/catalogo`, `/api/cotizar`, `/api/politicas`.
Solo usa la librería estándar de Python 3: no hay nada que instalar.

## Qué hace
- Responde 24/7 con horarios, ubicación, servicios, pagos y tiempos, según el manual.
- Los precios SIEMPRE salen del sistema (nunca de memoria). Dry clean: precio base aclarando que se valora en persona (cambie con `DRYCLEAN_PRECIO_BASE=0`).
- Daños, reembolsos, artículos dudosos y cotizaciones que el sistema no pueda dar: avisa por Telegram con el formato del manual y le da el +504 3227-5543 al cliente. Solo dice "enviado" si el aviso realmente salió.
- Tras escalar, el bot calla 12 horas en esa conversación (la atiende una persona).
- Mensajes fuera de tema: una respuesta amable y luego silencio. Reels, publicaciones y stickers: se ignoran. Fotos y audios: aviso de que una persona los revisará.
- Tope de 30 mensajes por persona al día. No guarda conversaciones más de 3 días.
- Rechaza cualquier petición al webhook que no venga firmada por Meta.

## Instalación en su VPS (Vultr)
1. Copie esta carpeta a `/opt/ecolavado-bot` y cree el usuario: `sudo useradd -r ecobot`.
2. `cp .env.example .env`, llene los valores y `chmod 600 .env`; `chown -R ecobot /opt/ecolavado-bot`.
3. Pruebe la conexión con su sistema: `python3 bot.py probar-sistema`. Debe mostrar productos, un precio de Solo Lavado y las políticas.
4. `sudo cp ecolavado-bot.service /etc/systemd/system/ && sudo systemctl enable --now ecolavado-bot`.
5. Meta exige una dirección **HTTPS** para el webhook. El bot escucha en el puerto 8081 (HTTP). Ponga delante el mismo servidor web/proxy que ya da HTTPS a su sistema, con una ruta `/webhook` hacia `127.0.0.1:8081` (también `/salud` para comprobar que está vivo).

## Token del bot (del otro chat)
En la app: Ajustes, Actualizar sistema. Luego genere el token con los comandos que le dio ese chat y péguelo en `BOT_TOKEN`. Se muestra una sola vez.

## Configuración en Meta (Instagram)
1. La cuenta de Instagram debe ser **Profesional** (Business o Creator).
2. En developers.facebook.com cree una app, agregue el producto de Instagram con **inicio de sesión de Instagram** y conecte la cuenta.
3. Genere el token de acceso de esa cuenta y péguelo en `IG_ACCESS_TOKEN`. El secreto de la app va en `IG_APP_SECRET`.
4. Webhook: URL `https://SU-DOMINIO/webhook` y el mismo texto de `IG_VERIFY_TOKEN`. Suscríbase al campo `messages`.
5. En Instagram: Configuración, Mensajes y respuestas a historias, Herramientas conectadas, active **Permitir acceso a los mensajes**.
6. Permiso necesario: `instagram_business_manage_messages`. Con la app en modo desarrollo solo funcionan cuentas con rol en la app. Para atender a **clientes reales** la app debe pasar a modo Live y obtener acceso avanzado mediante revisión de Meta. Es el paso que más tarda; la revisión pide un video de demostración y una política de privacidad.
7. Meta pide avisar que es una experiencia automatizada: el saludo del bot lo hace.

Los nombres exactos de los permisos y menús de Meta cambian con frecuencia: confírmelos en su consola.

## Telegram (aviso al personal)
Cree un bot con @BotFather (da `TELEGRAM_BOT_TOKEN`), escríbale un mensaje y abra `https://api.telegram.org/bot<TOKEN>/getUpdates` para ver su `chat.id` (`TELEGRAM_CHAT_ID`). Puede ser un grupo con el personal.

## Operación
- Ver el registro: `journalctl -u ecolavado-bot -f`
- Bloquear una cuenta: `python3 bot.py bloquear <ID>` (el ID aparece en el aviso de escalamiento).
- Apagar el bot sin detener el servidor: `BOT_ACTIVO=0` y reiniciar.
- Cambiar textos o reglas: `prompt.py`.
- Pruebas: `python3 -m unittest discover -s tests -v`

## Pendientes conocidos
- Full Service de 1 a 22 libras: solo están en la base de producción; el bot las lee con `cotizar`.
- Sanitización de prendas: no existe como producto; el bot no la cotiza y escala.
- El monto del recargo por almacenamiento (desde el día 15) no está definido: el bot dice que se consulta en tienda.
- Autoservicio de 91 a 100 libras está pendiente en el sistema.

## Panel web

`panel.py` es un servicio aparte (`ecolavado-panel.service`, puerto 8082) que lee la misma base de datos del bot.
Muestra métricas, chats, escalaciones pendientes y la lista de sistemas (`sistemas.json`).
Acceso con contraseña (`PANEL_PASSWORD` en `.env`), sin JavaScript, solo HTTPS vía Caddy:

```
panel.45-77-119-178.sslip.io {
    reverse_proxy 127.0.0.1:8082
}
```

Las conversaciones se guardan `LOG_DIAS` días (60 por defecto). Pausar el bot desde el panel no detiene el servicio.

## Botón "Actualizar" del panel

El panel (página **Actualizar**) crea `actualizar.flag`; una unidad `ecolavado-update.path` lo detecta y ejecuta
`/usr/local/sbin/ecolavado-update` (copia de `update.sh`, propiedad de root), que baja el código como usuario `ecobot`
(`git pull --ff-only`), revisa que compile y reinicia el bot y el panel. Instalación única:

```
mkdir -p /home/ecobot/.ssh && cp ~/.ssh/ecobot_deploy ~/.ssh/known_hosts /home/ecobot/.ssh/
chown -R ecobot:ecobot /home/ecobot/.ssh && chmod 700 /home/ecobot/.ssh && chmod 600 /home/ecobot/.ssh/ecobot_deploy
install -m 755 -o root -g root /opt/ecolavado-bot/update.sh /usr/local/sbin/ecolavado-update
cp ecolavado-update.service ecolavado-update.path /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now ecolavado-update.path
```

Si se cambia `update.sh`, hay que volver a instalarlo con el comando `install` (no se ejecuta desde la carpeta del bot a propósito).
