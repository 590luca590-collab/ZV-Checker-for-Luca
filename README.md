# 🤖 Bot Telegram – Verifica Nickname

## Configurazione rapida

### 1. Installa le dipendenze
```bash
pip install -r requirements.txt
```

### 2. Modifica `bot.py` – sezione CONFIGURAZIONE
```python
BOT_TOKEN    = "IL_TUO_TOKEN"       # da @BotFather su Telegram
GROUP_ID     = -1001234567890       # ID del tuo gruppo
ADMIN_IDS    = [123456789, 987654321]  # ID Telegram degli admin
TIMEOUT_MINUTI = 5                  # minuti prima del kick automatico
```

#### Come ottenere il TOKEN
1. Cerca `@BotFather` su Telegram
2. Scrivi `/newbot` e segui le istruzioni
3. Copia il token che ti fornisce

#### Come ottenere il GROUP_ID
- Aggiungi `@userinfobot` al gruppo → ti darà l'ID (inizia con `-100...`)
- Oppure usa `@getidsbot`

#### Come ottenere il tuo ID Telegram
- Scrivi a `@userinfobot` in privato

### 3. Aggiungi il bot al gruppo
- Aggiungi il bot come **amministratore** del gruppo
- Permessi necessari: *Aggiungere membri*, *Eliminare messaggi*, *Bannare utenti*

### 4. Avvia il bot
```bash
python bot.py
```

---

## Comandi disponibili

| Comando | Chi può usarlo | Descrizione |
|---------|---------------|-------------|
| `/rimuovi <ID o @username>` | Admin abilitati | Rimuove l'utente dal gruppo e dal DB |
| `/lista` | Admin abilitati | Mostra tutti gli utenti nel database |

---

## Flusso completo

```
Utente entra nel gruppo
        ↓
Bot chiede il nickname (5 min di tempo)
        ↓
    ┌───────────────────────────────┐
    │ Non risponde entro 5 min?     │ → KICK automatico
    └───────────────────────────────┘
        ↓ (risponde)
Bot salva il nickname e notifica gli admin in privato
        ↓
Admin riceve 3 bottoni:
  ✅ Accetta        → utente approvato, notificato
  ✏️ Correggi       → utente deve reinserire il nickname
  ❌ Rifiuta & Kick → admin inserisce nota opzionale → KICK
```

---

## Database (SQLite – `utenti.db`)

| Campo | Tipo | Descrizione |
|-------|------|-------------|
| `telegram_id` | INTEGER | ID univoco Telegram |
| `username` | TEXT | Username (@...) |
| `nickname` | TEXT | Nickname fornito |
| `stato` | TEXT | `in_attesa`, `in_verifica`, `approvato`, `rifiutato`, `correzione_richiesta` |
| `note_admin` | TEXT | Note inserite dall'admin al rifiuto |
| `data_ingresso` | TEXT | Timestamp ingresso |

---

## Hosting gratuito

### Opzione 1 – Railway.app ⭐ (consigliata)
1. Vai su [railway.app](https://railway.app) e registrati con GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Carica i file su un repo GitHub privato
4. Aggiungi variabile d'ambiente `BOT_TOKEN` nelle impostazioni
5. Railway avvia il bot automaticamente

**Piano gratuito:** 500 ore/mese (sufficienti per uso continuo)

### Opzione 2 – Render.com
1. Vai su [render.com](https://render.com) → New **Background Worker**
2. Collega il repo GitHub
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Aggiungi `BOT_TOKEN` come variabile d'ambiente

**Piano gratuito:** sempre attivo per background workers

### Opzione 3 – Oracle Cloud Free Tier (VPS gratis a vita)
1. Registrati su [cloud.oracle.com](https://cloud.oracle.com) (carta di credito richiesta ma non addebitata)
2. Crea una VM **Always Free** (Ubuntu 22.04, 1 OCPU, 1 GB RAM)
3. Carica i file con SCP o GitHub
4. Esegui con `screen` o `systemd` per mantenerlo attivo

```bash
# Su Oracle Cloud:
sudo apt update && sudo apt install python3-pip -y
pip install -r requirements.txt
screen -S bot
python bot.py
# Ctrl+A, D per staccarsi
```

### Opzione 4 – PythonAnywhere (più semplice)
1. Vai su [pythonanywhere.com](https://www.pythonanywhere.com) → piano gratuito
2. Apri una console Bash
3. Carica i file e installa le dipendenze
4. Crea un **Task programmato** (Always-on task → richiede piano a pagamento)
⚠️ Il piano free non supporta task sempre attivi — meglio Railway o Render

---

## Note importanti
- Il bot deve essere **admin del gruppo** per poter kickare
- SQLite salva il database in locale → su Railway/Render usare un volume persistente
  oppure migrare a PostgreSQL per non perdere i dati al riavvio
- Per produzione seria: salva `BOT_TOKEN` come variabile d'ambiente, non nel codice
