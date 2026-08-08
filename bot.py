import logging
import asyncio
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, time as dtime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telegram.error import TelegramError

# ─────────────────────────────────────────────
#  CONFIGURAZIONE
# ─────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
DATABASE_URL   = os.environ["DATABASE_URL"]
GROUP_ID       = -1003839666195
ADMIN_IDS      = [390056974, 6345602422]

TIMEOUT_MINUTI          = 5    # minuti per fornire il nick al primo ingresso
TIMEOUT_CORREZIONE_MIN  = 30   # minuti per ri-fornire il nick dopo "correggi"

ORA_RESOCONTO = dtime(16, 0)   # ore 16:00 ogni giorno
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#  DATABASE (PostgreSQL / Neon)
# ══════════════════════════════════════════════

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS utenti (
                    telegram_id   BIGINT PRIMARY KEY,
                    username      TEXT,
                    nickname      TEXT,
                    stato         TEXT DEFAULT 'in_attesa',
                    note_admin    TEXT,
                    data_ingresso TEXT
                )
            """)
            # Tabella log giornaliero entrate/uscite
            cur.execute("""
                CREATE TABLE IF NOT EXISTS log_movimenti (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    username    TEXT,
                    nickname    TEXT,
                    tipo        TEXT,   -- 'entrata' o 'uscita'
                    data        TEXT
                )
            """)
        conn.commit()


def upsert_utente(telegram_id: int, username: str, nickname: str = None,
                  stato: str = "in_attesa", note: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO utenti
                    (telegram_id, username, nickname, stato, note_admin, data_ingresso)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username      = EXCLUDED.username,
                    nickname      = COALESCE(EXCLUDED.nickname, utenti.nickname),
                    stato         = EXCLUDED.stato,
                    note_admin    = COALESCE(EXCLUDED.note_admin, utenti.note_admin),
                    data_ingresso = COALESCE(utenti.data_ingresso, EXCLUDED.data_ingresso)
            """, (telegram_id, username, nickname, stato, note,
                  datetime.now().isoformat()))
        conn.commit()


def get_utente(telegram_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM utenti WHERE telegram_id = %s", (telegram_id,)
            )
            return cur.fetchone()


def rimuovi_utente_db(telegram_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM utenti WHERE telegram_id = %s", (telegram_id,))
        conn.commit()


def tutti_utenti():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM utenti")
            return cur.fetchall()


def log_movimento(telegram_id: int, username: str, nickname: str, tipo: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO log_movimenti (telegram_id, username, nickname, tipo, data)
                VALUES (%s, %s, %s, %s, %s)
            """, (telegram_id, username, nickname or "", tipo, datetime.now().isoformat()))
        conn.commit()


def get_movimenti_oggi():
    oggi = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM log_movimenti WHERE data LIKE %s", (f"{oggi}%",)
            )
            return cur.fetchall()


def svuota_log_oggi():
    oggi = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM log_movimenti WHERE data LIKE %s", (f"{oggi}%",))
        conn.commit()


# ══════════════════════════════════════════════
#  STATO IN MEMORIA
# ══════════════════════════════════════════════
# pending[user_id] = {
#   "job": job | None,
#   "chat_id": int,
#   "correzione": bool,
#   "benvenuto_msg_id": int | None   ← ID del messaggio "scrivi il nick"
# }
pending: dict = {}

# verifica_pending[user_id] = {"nickname": str, "username": str}
verifica_pending: dict = {}


# ══════════════════════════════════════════════
#  HELPER: kick
# ══════════════════════════════════════════════

async def kick_user(context: ContextTypes.DEFAULT_TYPE,
                    chat_id: int, user_id: int):
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await asyncio.sleep(1)
        await context.bot.unban_chat_member(chat_id, user_id)
    except TelegramError as e:
        logger.warning(f"Impossibile kickare {user_id}: {e}")


# ══════════════════════════════════════════════
#  TIMEOUT: kick se non risponde entro N minuti
# ══════════════════════════════════════════════

async def timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    user_id  = context.job.data["user_id"]
    chat_id  = context.job.data["chat_id"]
    username = context.job.data.get("username", "")

    if user_id not in pending:
        return

    info = pending.pop(user_id)
    await kick_user(context, chat_id, user_id)

    # Cancella il messaggio "scrivi il nick" se presente
    bmid = info.get("benvenuto_msg_id")
    if bmid:
        try:
            await context.bot.delete_message(chat_id, bmid)
        except TelegramError:
            pass

    mention = f'<a href="tg://user?id={user_id}">@{username or user_id}</a>'
    motivo  = "correzione del nickname" if info.get("correzione") else "nickname"
    try:
        await context.bot.send_message(
            chat_id,
            f"⏰ {mention} è stato rimosso per non aver fornito il {motivo} "
            f"entro il tempo limite.",
            parse_mode="HTML"
        )
    except TelegramError:
        pass

    # Log uscita
    row = get_utente(user_id)
    log_movimento(user_id, username, row[2] if row else "", "uscita")
    rimuovi_utente_db(user_id)
    logger.info(f"Utente {user_id} kickato per timeout.")


# ══════════════════════════════════════════════
#  HANDLER: nuovo membro nel gruppo
# ══════════════════════════════════════════════

async def nuovo_membro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        user_id  = member.id
        username = member.username or ""

        upsert_utente(user_id, username, stato="in_attesa")
        log_movimento(user_id, username, "", "entrata")

        # Messaggio nel gruppo — unico canale garantito
        sent = await context.bot.send_message(
            GROUP_ID,
            f"👋 Benvenuto/a nel gruppo, {member.mention_html()}!\n\n"
            f"⚠️ Per rimanere devi scrivere il tuo <b>nickname</b> "
            f"entro <b>{TIMEOUT_MINUTI} minuti</b> qui sotto.\n"
            f"Se non lo fai sarai rimosso automaticamente.",
            parse_mode="HTML"
        )

        job = context.job_queue.run_once(
            timeout_callback,
            when=TIMEOUT_MINUTI * 60,
            data={"user_id": user_id, "chat_id": GROUP_ID, "username": username},
            name=f"timeout_{user_id}"
        )
        pending[user_id] = {
            "job": job,
            "chat_id": GROUP_ID,
            "correzione": False,
            "benvenuto_msg_id": sent.message_id
        }
        logger.info(f"Nuovo membro {user_id} (@{username}), timer avviato.")


# ══════════════════════════════════════════════
#  HANDLER: ricezione nickname nel gruppo
# ══════════════════════════════════════════════

async def ricevi_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id
    text    = update.message.text.strip()

    if text.startswith("/"):
        return
    if user_id not in pending:
        return

    nickname      = text
    username      = user.username or ""
    info          = pending[user_id]
    is_correzione = info.get("correzione", False)

    # Cancella timer
    job = info.get("job")
    if job:
        job.schedule_removal()

    # Cancella il messaggio "scrivi il nick"
    bmid = info.get("benvenuto_msg_id")
    if bmid:
        try:
            await context.bot.delete_message(GROUP_ID, bmid)
        except TelegramError:
            pass

    del pending[user_id]

    upsert_utente(user_id, username, nickname=nickname, stato="in_verifica")
    verifica_pending[user_id] = {"nickname": nickname, "username": username}

    # Conferma nel gruppo
    tipo_testo = "Nickname corretto" if is_correzione else "Nickname"
    conf = await context.bot.send_message(
        GROUP_ID,
        f"✅ {user.mention_html()}, {tipo_testo} <b>{nickname}</b> ricevuto!\n"
        "Un admin lo verificherà a breve. ⏳",
        parse_mode="HTML"
    )

    # Notifica admin in privato
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accetta", callback_data=f"accetta:{user_id}"),
            InlineKeyboardButton("✏️ Correggi", callback_data=f"correggi:{user_id}"),
        ],
        [
            InlineKeyboardButton("❌ Rifiuta & Kick", callback_data=f"rifiuta:{user_id}"),
        ]
    ])

    tipo_label = "🔄 <b>Correzione nickname</b>" if is_correzione else "🔔 <b>Verifica nickname</b>"
    msg = (
        f"{tipo_label}\n\n"
        f"👤 Utente: {user.mention_html()}\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📛 Username: @{username}\n"
        f"🏷 Nickname fornito: <b>{nickname}</b>"
    )

    # Salva msg_id della conferma per poterlo sostituire dopo
    verifica_pending[user_id]["conf_msg_id"] = conf.message_id

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id, msg,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except TelegramError as e:
            logger.warning(f"Non riesco a contattare admin {admin_id}: {e}")


# ══════════════════════════════════════════════
#  HANDLER: callback bottoni admin
# ══════════════════════════════════════════════

async def callback_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user

    if admin.id not in ADMIN_IDS:
        await query.answer("⛔ Non sei un admin abilitato. Get OUT", show_alert=True)
        return

    await query.answer()
    action, uid_str = query.data.split(":", 1)
    user_id  = int(uid_str)

    info     = verifica_pending.get(user_id, {})
    nickname = info.get("nickname", "N/D")
    username = info.get("username", "")
    conf_mid = info.get("conf_msg_id")

    # ── ACCETTA ──────────────────────────────
    if action == "accetta":
        upsert_utente(user_id, username, nickname=nickname, stato="approvato")
        verifica_pending.pop(user_id, None)

        # Aggiorna messaggio admin
        await query.edit_message_text(
            f"✅ Nickname <b>{nickname}</b> per @{username} "
            f"approvato da {admin.mention_html()}.",
            parse_mode="HTML"
        )

        # Cancella il messaggio "in verifica" nel gruppo e manda benvenuto
        if conf_mid:
            try:
                await context.bot.delete_message(GROUP_ID, conf_mid)
            except TelegramError:
                pass

        try:
            mention = f'<a href="tg://user?id={user_id}">{username or user_id}</a>'
            await context.bot.send_message(
                GROUP_ID,
                f"Benvenuto/a, @{username}!\n"

                f"Il Movimento <b>ZERO VOX</> ti dà ufficialmente il benvenuto tra i suoi tesserati.\n"

                f"Sei entrato/a a far parte <b>dell'Assemblea dei Comuni</>, la sezione riservata a tutti i tesserati del partito: il cuore democratico della nostra organizzazione.\n"

                f"📋 <b>Ricordati</> di:\n"
                f"— Leggere lo <b>Statuto</> e i <b>Regolamenti</> interni\n"
                f"— Rispettare le deliberazioni degli organi del partito\n"
                f"— Partecipare attivamente alla vita politica del <b>Movimento</>\n"

                f"<quote>«La voce di chi non ne ha»</>\n"

                f"🪪 <b>Nickname</>: {nickname}\n",
                parse_mode="HTML"
            )
        except TelegramError:
            pass

    # ── CORREGGI ─────────────────────────────
    elif action == "correggi":
        upsert_utente(user_id, username, stato="correzione_richiesta")

        # Timer 30 minuti per correzione
        # Prima rimuovi eventuali job precedenti
        for job in context.job_queue.get_jobs_by_name(f"timeout_{user_id}"):
            job.schedule_removal()

        job = context.job_queue.run_once(
            timeout_callback,
            when=TIMEOUT_CORREZIONE_MIN * 60,
            data={"user_id": user_id, "chat_id": GROUP_ID,
                  "username": username, "correzione": True},
            name=f"timeout_{user_id}"
        )

        # Cancella messaggio "in verifica"
        if conf_mid:
            try:
                await context.bot.delete_message(GROUP_ID, conf_mid)
            except TelegramError:
                pass

        # Notifica nel gruppo con mention — UNICO canale garantito
        mention = f'<a href="tg://user?id={user_id}">@{username or user_id}</a>'
        sent = await context.bot.send_message(
            GROUP_ID,
            f"✏️ {mention}, il tuo nickname <b>{nickname}</b> non è scritto correttamente.\n\n"
            f"Scrivi il nickname corretto qui nel gruppo entro "
            f"<b>{TIMEOUT_CORREZIONE_MIN} minuti</b>, altrimenti sarai rimosso.",
            parse_mode="HTML"
        )

        pending[user_id] = {
            "job": job,
            "chat_id": GROUP_ID,
            "correzione": True,
            "benvenuto_msg_id": sent.message_id
        }

        # Aggiorna messaggio admin
        await query.edit_message_text(
            f"✏️ Correzione richiesta per @{username} "
            f"da {admin.mention_html()}. Attende risposta entro {TIMEOUT_CORREZIONE_MIN} min.",
            parse_mode="HTML"
        )

    # ── RIFIUTA ──────────────────────────────
    elif action == "rifiuta":
        await query.edit_message_text(
            f"❌ Stai per rifiutare @{username}.\n\n"
            "Scrivi una <b>nota</b> da mostrare nel gruppo (opzionale), "
            "oppure <code>-</code> per procedere senza nota.",
            parse_mode="HTML"
        )
        context.user_data["rifiuta_target"] = user_id


# ══════════════════════════════════════════════
#  HANDLER: nota rifiuto (messaggio privato admin)
# ══════════════════════════════════════════════

async def ricevi_nota_rifiuto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nota = update.message.text.strip()
    if nota == "-":
        nota = ""

    target   = context.user_data.pop("rifiuta_target")
    info     = verifica_pending.get(target, {})
    nickname = info.get("nickname", "N/D")
    username = info.get("username", "")
    conf_mid = info.get("conf_msg_id")

    upsert_utente(target, username, stato="rifiutato", note=nota)
    verifica_pending.pop(target, None)

    # Kick
    await kick_user(context, GROUP_ID, target)

    # Cancella msg "in verifica"
    if conf_mid:
        try:
            await context.bot.delete_message(GROUP_ID, conf_mid)
        except TelegramError:
            pass

    # Notifica nel gruppo con mention
    mention = f'<a href="tg://user?id={target}">@{username or target}</a>'
    msg_gruppo = f"🚫 {mention} è stato rimosso dal gruppo."
    if nota:
        msg_gruppo += f"\n📝 Motivo: {nota}"
    try:
        await context.bot.send_message(GROUP_ID, msg_gruppo, parse_mode="HTML")
    except TelegramError:
        pass

    # Conferma all'admin
    await update.message.reply_text(
        f"✅ Utente @{username} (ID: <code>{target}</code>) rimosso.\n"
        f"Nota: {nota or 'nessuna'}",
        parse_mode="HTML"
    )

    log_movimento(target, username, nickname, "uscita")
    rimuovi_utente_db(target)


# ══════════════════════════════════════════════
#  HANDLER: messaggi privati degli admin
# ══════════════════════════════════════════════

async def messaggio_privato_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    if update.effective_chat.type != "private":
        return
    if context.user_data.get("rifiuta_target"):
        await ricevi_nota_rifiuto(update, context)


# ══════════════════════════════════════════════
#  COMANDO /listautenti
# ══════════════════════════════════════════════

async def cmd_listautenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    rows = tutti_utenti()
    if not rows:
        await update.message.reply_text("📋 Database vuoto.")
        return

    testo = "📋 <b>Utenti nel database:</b>\n\n"
    for tid, username, nickname, stato, note, data_ingresso in rows:
        testo += (
            f"• <code>{tid}</code> @{username} → <b>{nickname}</b> [{stato}]"
            + (f"\n  📝 {note}" if note else "")
            + "\n"
        )

    for chunk in [testo[i:i+4000] for i in range(0, len(testo), 4000)]:
        await update.message.reply_text(chunk, parse_mode="HTML")


# ══════════════════════════════════════════════
#  COMANDO /rimuoviutente
# ══════════════════════════════════════════════

async def cmd_rimuoviutente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ Uso:\n"
            "<code>/rimuoviutente &lt;ID&gt;</code>\n"
            "oppure: <code>/rimuoviutente @username</code>",
            parse_mode="HTML"
        )
        return

    arg = context.args[0].lstrip("@")

    with get_conn() as conn:
        with conn.cursor() as cur:
            if arg.isdigit():
                cur.execute(
                    "SELECT * FROM utenti WHERE telegram_id = %s", (int(arg),)
                )
            else:
                cur.execute(
                    "SELECT * FROM utenti WHERE username = %s", (arg,)
                )
            row = cur.fetchone()

    if not row:
        await update.message.reply_text("❌ Utente non trovato nel database.")
        return

    tid, username, nickname, stato, note, data_ingresso = row

    await kick_user(context, GROUP_ID, tid)

    # Notifica nel gruppo
    mention = f'<a href="tg://user?id={tid}">@{username or tid}</a>'
    try:
        await context.bot.send_message(
            GROUP_ID,
            f"🚫 {mention} è stato rimosso dal gruppo da un amministratore.",
            parse_mode="HTML"
        )
    except TelegramError:
        pass

    log_movimento(tid, username, nickname, "uscita")
    rimuovi_utente_db(tid)

    await update.message.reply_text(
        f"✅ Utente rimosso:\n"
        f"• ID: <code>{tid}</code>\n"
        f"• Username: @{username}\n"
        f"• Nickname: {nickname}\n"
        f"• Stato precedente: {stato}",
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════
#  COMANDO /aggiungiutente
# ══════════════════════════════════════════════

async def cmd_aggiungiutente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return

    # Uso: /aggiungiutente <ID> <@username> <nickname>
    if len(context.args) < 3:
        await update.message.reply_text(
            "ℹ️ Uso: <code>/aggiungiutente &lt;ID&gt; &lt;@username&gt; &lt;nickname&gt;</code>\n\n"
            "Esempio:\n<code>/aggiungiutente 123456789 @mario MarioRossi</code>",
            parse_mode="HTML"
        )
        return

    id_arg       = context.args[0]
    username_arg = context.args[1].lstrip("@")
    nickname_arg = " ".join(context.args[2:])

    if not id_arg.lstrip("-").isdigit():
        await update.message.reply_text("❌ L'ID deve essere un numero.")
        return

    tid = int(id_arg)

    upsert_utente(tid, username_arg, nickname=nickname_arg, stato="approvato")
    log_movimento(tid, username_arg, nickname_arg, "entrata")

    await update.message.reply_text(
        f"✅ Utente aggiunto manualmente:\n"
        f"• ID: <code>{tid}</code>\n"
        f"• Username: @{username_arg}\n"
        f"• Nickname: <b>{nickname_arg}</b>",
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════
#  RESOCONTO GIORNALIERO alle 16:00
# ══════════════════════════════════════════════

async def resoconto_giornaliero(context: ContextTypes.DEFAULT_TYPE):
    movimenti = get_movimenti_oggi()

    entrate = [m for m in movimenti if m[4] == "entrata"]
    uscite  = [m for m in movimenti if m[4] == "uscita"]

    oggi = datetime.now().strftime("%d/%m/%Y")
    testo = f"📊 <b>Resoconto giornaliero — {oggi}</b>\n\n"

    testo += f"📥 <b>Entrate oggi ({len(entrate)}):</b>\n"
    if entrate:
        for _, tid, username, nickname, tipo, data in entrate:
            testo += f"  • @{username} (<code>{tid}</code>) → <b>{nickname or 'nick non ancora assegnato'}</b>\n"
    else:
        testo += "  Nessuna entrata.\n"

    testo += f"\n📤 <b>Uscite oggi ({len(uscite)}):</b>\n"
    if uscite:
        for _, tid, username, nickname, tipo, data in uscite:
            testo += f"  • @{username} (<code>{tid}</code>) → <b>{nickname or 'N/D'}</b>\n"
    else:
        testo += "  Nessuna uscita.\n"

    testo += f"\n👥 <b>Totale utenti approvati nel DB:</b> {len([u for u in tutti_utenti() if u[3] == 'approvato'])}"

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, testo, parse_mode="HTML")
        except TelegramError as e:
            logger.warning(f"Impossibile inviare resoconto ad admin {admin_id}: {e}")


# ══════════════════════════════════════════════
#  HEALTH SERVER (collegato allo stato reale del bot)
# ══════════════════════════════════════════════

last_update_time = datetime.now()
WATCHDOG_TIMEOUT_SECONDS = 180  # nessun heartbeat riuscito da 3 min -> bot bloccato


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        elapsed = (datetime.now() - last_update_time).total_seconds()
        if elapsed <= WATCHDOG_TIMEOUT_SECONDS:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            # Bot bloccato: rispondo errore cosi' l'Health Check di Render lo rileva e riavvia il servizio
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"BOT NOT RESPONDING")

    def log_message(self, format, *args):
        pass  # silenzia i log HTTP per non intasare i log del bot


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# ══════════════════════════════════════════════
#  WATCHDOG (riavvia il processo se il bot si blocca)
# ══════════════════════════════════════════════


def run_watchdog():
    while True:
        import time as _time
        _time.sleep(30)
        elapsed = (datetime.now() - last_update_time).total_seconds()
        if elapsed > WATCHDOG_TIMEOUT_SECONDS:
            logger.error(
                f"Watchdog: nessun update ricevuto da {int(elapsed)}s, il bot sembra bloccato. Chiudo il processo."
            )
            os._exit(1)


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    global last_update_time
    try:
        await context.bot.get_me()
        last_update_time = datetime.now()
    except Exception:
        logger.exception("Heartbeat: chiamata a Telegram fallita.")


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════

def main():
    init_db()

    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=run_watchdog, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Heartbeat: verifica ogni 60s che il bot sia ancora connesso a Telegram
    app.job_queue.run_repeating(heartbeat_job, interval=60, first=10)

    # Nuovi membri nel gruppo
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, nuovo_membro
    ))

    # Nickname nel gruppo (testo da utenti in pending)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        ricevi_nickname
    ))

    # Bottoni inline admin
    app.add_handler(CallbackQueryHandler(callback_admin))

    # Messaggi privati admin (nota rifiuto)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        messaggio_privato_admin
    ))

    # Comandi admin
    app.add_handler(CommandHandler("listautenti",    cmd_listautenti))
    app.add_handler(CommandHandler("rimuoviutente",  cmd_rimuoviutente))
    app.add_handler(CommandHandler("aggiungiutente", cmd_aggiungiutente))

    # Resoconto giornaliero alle 16:00
    app.job_queue.run_daily(
        resoconto_giornaliero,
        time=ORA_RESOCONTO,
        name="resoconto_daily"
    )

    logger.info("Bot avviato.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Il bot si è fermato per un errore non gestito, chiudo il processo.")
        os._exit(1)
    logger.error("run_polling è terminato inaspettatamente, chiudo il processo.")
    os._exit(1)
