#!/bin/bash
# Se instala como /usr/local/sbin/ecolavado-update (propiedad de root) y lo ejecuta systemd cuando
# el panel crea actualizar.flag. Baja el código como usuario "ecobot" y solo usa root para reiniciar.
D=/opt/ecolavado-bot
LOG=$D/actualizacion.log
rm -f "$D/actualizar.flag"
if [ -f "$LOG" ]; then tail -n 150 "$LOG" > "$LOG.tmp" && cat "$LOG.tmp" > "$LOG"; rm -f "$LOG.tmp"; fi
exec >> "$LOG" 2>&1
echo "== $(date '+%F %T') inicio =="
cd "$D" || exit 1
EC="runuser -u ecobot --"
ANTES=$($EC git rev-parse --short HEAD)
if ! $EC env GIT_SSH_COMMAND="ssh -i /home/ecobot/.ssh/ecobot_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=/home/ecobot/.ssh/known_hosts" git pull --ff-only; then
  echo "RESULTADO: ERROR al bajar el código desde GitHub"; exit 1
fi
if ! $EC python3 -m py_compile bot.py panel.py prompt.py legal.py; then
  echo "RESULTADO: ERROR el código nuevo tiene fallas; se vuelve a la versión anterior"
  $EC git reset --hard "$ANTES"; exit 1
fi
DESPUES=$($EC git rev-parse --short HEAD)
systemctl restart ecolavado-bot
echo "RESULTADO: OK $ANTES -> $DESPUES (bot reiniciado)"
systemctl restart --no-block ecolavado-panel
