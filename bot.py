import os, re, sys, time, random, sqlite3, threading, uuid, queue
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.webhook import WebhookClient

sys.stdout.reconfigure(line_buffering=True)  # show print() lines in the Render logs right away

if "SLACK_BOT_TOKEN" not in os.environ or "SLACK_APP_TOKEN" not in os.environ:
    sys.exit("Missing tokens. Run the two `export` lines first (see instructions).")

app = App(token=os.environ["SLACK_BOT_TOKEN"])

ADMINS = {"U0C7S6T72DD"}  # Slack member IDs allowed to use admin commands (also hidden from /lb)

# ---------- database ----------
SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eco.db")  # a copy shipped in the code repo

# ---------- free cloud backup of the coins (private GitHub repo) ----------
# Set GITHUB_TOKEN (fine-grained token, "Contents: read & write" on ONE private repo)
# and GITHUB_REPO ("yourname/hakari-data"). The database is restored on start and
# saved every 60 seconds when something changed, so coins survive restarts on hosts with a wiped disk.
import base64, json, shutil, signal, urllib.request, urllib.error


def _clean_repo(r):
    r = (r or "").strip()
    for p in ("https://github.com/", "http://github.com/", "github.com/"):
        if r.startswith(p):
            r = r[len(p):]
    r = r.strip("/")
    return r[:-4] if r.endswith(".git") else r


GH_TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip() or None
GH_REPO = _clean_repo(os.environ.get("GITHUB_REPO")) or None
# With the cloud backup on, the live database is a separate file that is ALWAYS loaded from the
# backup at startup. An old eco.db that sits in the code repo is never used as the live database.
DB_PATH = (os.path.join(os.path.dirname(os.path.abspath(__file__)), "hakari_live.db")
           if (GH_TOKEN and GH_REPO) else SEED_PATH)
GH_URL = f"https://api.github.com/repos/{GH_REPO}/contents/eco.db"
gh_sha = None
last_backup = None


def gh(method="GET", data=None, raw=False):
    req = urllib.request.Request(GH_URL, method=method, headers={
        "Authorization": f"Bearer {GH_TOKEN}", "User-Agent": "hakari-bot",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"})
    if data is not None:
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return body if raw else json.loads(body)


synced = False  # True only once we KNOW what is in the backup (so we never overwrite it blindly)
status = {"backup": "OFF (GITHUB_TOKEN / GITHUB_REPO missing: coins are NOT saved!)",
          "restore": "not tried", "last_save": "never", "last_error": "none"}


def repo_ok():
    req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}", headers={
        "Authorization": f"Bearer {GH_TOKEN}", "User-Agent": "hakari-bot"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except Exception:
        return False


def restore_db():
    global synced
    if not (GH_TOKEN and GH_REPO):
        print("!! BACKUP IS OFF: set GITHUB_TOKEN and GITHUB_REPO or coins reset on every restart !!")
        return
    status["backup"] = f"ON ({GH_REPO})"
    for f in (DB_PATH, DB_PATH + ".bak"):  # never trust a leftover file, the backup is the truth
        if os.path.exists(f):
            os.remove(f)
    try:
        with open(DB_PATH, "wb") as f:
            f.write(gh(raw=True))
        synced = True
        status["restore"] = "restored coins from backup"
        print("Restored coins from GitHub backup.")
    except urllib.error.HTTPError as e:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        if e.code == 404 and repo_ok():
            synced = True
            if os.path.exists(SEED_PATH):
                shutil.copyfile(SEED_PATH, DB_PATH)
                status["restore"] = "no backup yet: started from the eco.db snapshot in the code repo"
                print("No backup yet: seeded from the eco.db snapshot.")
            else:
                status["restore"] = "no backup yet (first run), starting fresh"
                print("No backup yet (first run), starting fresh.")
        else:
            sys.exit(f"!! Could not read the coin backup (HTTP {e.code}) for GITHUB_REPO='{GH_REPO}'. "
                     f"Refusing to start so coins are not overwritten. GITHUB_REPO must look like "
                     f"yourname/hakari-data, the repo must exist, and the token must be allowed on THAT repo "
                     f"with Contents: Read and write.")
    except Exception as e:
        sys.exit(f"!! Could not read the coin backup ({e!r}). Refusing to start so coins are not overwritten.")


def backup_db(force=False):
    global gh_sha, last_backup
    if not (GH_TOKEN and GH_REPO) or not synced:
        return
    try:
        tmp_path = DB_PATH + ".bak"
        with lock:
            dst = sqlite3.connect(tmp_path)
            db.backup(dst)
            dst.close()
        with open(tmp_path, "rb") as f:
            blob = f.read()
        if blob == last_backup and not force:
            return
        payload = {"message": "backup", "content": base64.b64encode(blob).decode()}
        for attempt in range(4):
            try:
                if gh_sha is None:
                    try:
                        gh_sha = gh()["sha"]
                    except urllib.error.HTTPError as e:
                        if e.code != 404:
                            raise
                if gh_sha:
                    payload["sha"] = gh_sha
                gh_sha = gh("PUT", payload)["content"]["sha"]
                last_backup = blob
                status["last_save"] = time.strftime("%H:%M:%S UTC", time.gmtime())
                status["last_error"] = "none"
                return
            except urllib.error.HTTPError as e:
                if e.code in (409, 422, 500, 502, 503, 504):
                    if e.code in (409, 422):
                        print("!! Backup changed under us: is ANOTHER copy of the bot running? "
                              "(stop your Mac's copy / old deploys)")
                    gh_sha = None  # out of date, refetch and retry
                    payload.pop("sha", None)
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        status["last_error"] = repr(e)
        print("Backup failed:", repr(e))


def backup_loop():
    while True:
        time.sleep(60)
        backup_db()


restore_db()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
lock = threading.RLock()
db.execute("CREATE TABLE IF NOT EXISTS users (uid TEXT PRIMARY KEY, cash TEXT DEFAULT '100', bank TEXT DEFAULT '0')")
db.execute("CREATE TABLE IF NOT EXISTS cooldowns (uid TEXT, action TEXT, ts REAL, PRIMARY KEY (uid, action))")
db.execute("CREATE TABLE IF NOT EXISTS items (name TEXT PRIMARY KEY, price INTEGER, description TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS inv (uid TEXT, name TEXT, amount INTEGER, PRIMARY KEY (uid, name))")
db.execute("CREATE TABLE IF NOT EXISTS log (ts REAL, uid TEXT, cash TEXT, bank TEXT, why TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, val TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS snap (uid TEXT PRIMARY KEY, cash TEXT, bank TEXT)")
for col in ("name TEXT", "last_seen REAL"):
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {col}")
    except sqlite3.OperationalError:
        pass
db.commit()

COIN = os.environ.get("COIN", "🪙")  # normal Slack coin. Set COIN in Render only if you want another icon
COOLDOWNS = {"work": 60, "crime": 60, "rob": 120, "collect": 86400}  # seconds (admins have none)
GAME_TIMEOUT = 300  # seconds before an unfinished game is cleaned up
PVP_BONUS = 1       # extra bets the house adds to the winner of a person-vs-person game (0 = zero-sum, safest vs alts)
CHAT_COOLDOWN = 30
CHAT_MIN, CHAT_MAX = 10, 30
chat_last = {}
ACTIVE_DAYS = 3     # people who haven't used the bot for this long can't be robbed
LOG_CHANNEL = os.environ.get("LOG_CHANNEL", "gamblelogs")  # every command is logged here
C_GREEN, C_RED, C_GOLD, C_BLUE, C_GREY = "#2ecc71", "#e74c3c", "#f1c40f", "#3498db", "#95a5a6"


def fmt(n):
    return f"{COIN} {int(n):,}"


def signed(n):
    return f"{'+' if n >= 0 else '-'}{fmt(abs(n))}"


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def get(uid):
    with lock:
        db.execute("INSERT OR IGNORE INTO users (uid, cash, bank) VALUES (?, '100', '0')", (uid,))
        db.commit()
        cash, bank = db.execute("SELECT cash, bank FROM users WHERE uid=?", (uid,)).fetchone()
        return int(cash), int(bank)


def change(uid, cash=0, bank=0):
    cur_cash, cur_bank = get(uid)
    new_cash = str(cur_cash + int(cash))
    new_bank = str(cur_bank + int(bank))
    why = sys._getframe(1).f_code.co_name  # which function moved the money (see /audit)
    with lock:
        db.execute("UPDATE users SET cash=?, bank=? WHERE uid=?", (new_cash, new_bank, uid))
        db.execute("INSERT INTO log VALUES (?,?,?,?,?)", (time.time(), uid, str(int(cash)), str(int(bank)), why))
        if random.random() < 0.01:
            db.execute("DELETE FROM log WHERE ts < ?", (time.time() - 7 * 86400,))
        db.commit()


def touch(uid):
    with lock:
        db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), uid))
        db.commit()


def setting(key, default):
    with lock:
        r = db.execute("SELECT val FROM settings WHERE key=?", (key,)).fetchone()
    return type(default)(r[0]) if r else default


def set_setting(key, val):
    with lock:
        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(val)))
        db.commit()


# ---------- names / mentions ----------
def clean_name(n):
    return re.sub(r"[*_~`<>&|]", "", n or "").strip()


def set_name(uid, name):
    name = clean_name(name)
    if not uid or not name:
        return
    get(uid)
    with lock:
        db.execute("UPDATE users SET name=? WHERE uid=?", (name, uid))
        db.commit()


name_cache = {}


def name_of(uid):
    """Real display name (needs the users:read scope); falls back to the saved handle."""
    if uid in name_cache:
        return name_cache[uid]
    try:
        info = app.client.users_info(user=uid)["user"]
        p = info.get("profile", {})
        n = clean_name(p.get("display_name") or p.get("real_name") or info.get("name"))
        if n:
            name_cache[uid] = n
            return n
    except Exception as e:
        print("users_info failed (is users:read granted + app reinstalled?):", repr(e))
    with lock:
        r = db.execute("SELECT name FROM users WHERE uid=?", (uid,)).fetchone()
    return r[0] if r and r[0] else "someone"


MARK = re.compile(r"⟦(\w+)⟧")


def tag(uid):
    """Placeholder for a person: becomes a real @ping or a bold display name right before sending."""
    return f"⟦{uid}⟧"


def render(obj, ping):
    if isinstance(obj, str):
        def sub(m):
            if ping:
                return f"<@{m.group(1)}>"
            n = name_of(m.group(1))
            # already inside *bold*? then plain name, otherwise we get **Name*'s
            return n if obj.count("*", 0, m.start()) % 2 else f"*{n}*"
        return MARK.sub(sub, obj)
    if isinstance(obj, dict):
        return {k: render(v, ping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, ping) for v in obj]
    return obj


# ---------- pretty messages (coloured bar like an embed) ----------
def section(text, fields=None):
    b = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if fields:
        b["fields"] = [{"type": "mrkdwn", "text": f} for f in fields]
    return b


def btn(label, action_id, value="x", style=None):
    b = {"type": "button", "text": {"type": "plain_text", "text": label, "emoji": True},
         "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    return b


HELP_BTN = btn("❓ Help", "show_help")


def embed(text, color=C_BLUE, fields=None, buttons=None, footer=None, rt="ephemeral", nolog=False):
    blocks = [section(text, fields)]
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    d = {"text": text.replace("*", "")[:300], "attachments": [{"color": color, "blocks": blocks}]}
    if rt:
        d["response_type"] = rt
    if nolog:
        d["nolog"] = True
    return d


def wire(d):
    """Slack shows `text` AND the attachment when both are sent (double message). Send only the attachment."""
    d = dict(d)
    if d.get("attachments"):
        d.pop("text", None)
    return d


def swap(respond, view):
    """Replace the message a button was clicked on."""
    v = wire(render(view, False))
    respond(replace_original=True, **v)


def only_you(respond, text):
    respond(text=text, replace_original=False, response_type="ephemeral")


# ---------- #gamblelogs: every command is written here ----------
log_q = queue.Queue()
LOG_CH = LOG_CHANNEL if re.match(r"^[CG][A-Z0-9]{8,}$", LOG_CHANNEL) else "#" + LOG_CHANNEL.lstrip("#")


def glog(uid, what, result=""):
    log_q.put(f"🧾 {tag(uid)} {what}" + (f" → {result}" if result else ""))


def _log_worker():
    while True:
        text = log_q.get()
        try:
            app.client.chat_postMessage(channel=LOG_CH, text=render(text, False).replace("\n", " | ")[:1500])
        except Exception as e:
            print(f"Could not post to {LOG_CH} (make the channel, or set LOG_CHANNEL to its ID):", repr(e))
        time.sleep(1.1)


threading.Thread(target=_log_worker, daemon=True).start()


# ---------- cooldowns, game limits ----------
def cooldown_left(uid, action, start=True):
    """Seconds left. If 0 and start=True, the cooldown (re)starts now."""
    if uid in ADMINS:
        return 0
    now = time.time()
    with lock:
        row = db.execute("SELECT ts FROM cooldowns WHERE uid=? AND action=?", (uid, action)).fetchone()
        if row and now - row[0] < COOLDOWNS[action]:
            return int(COOLDOWNS[action] - (now - row[0])) + 1
        if start:
            db.execute("INSERT OR REPLACE INTO cooldowns VALUES (?,?,?)", (uid, action, now))
            db.commit()
    return 0


def wait_text(left):
    if left >= 3600:
        return f"⏱ Wait {left // 3600}h {(left % 3600) // 60}m."
    if left >= 60:
        return f"⏱ Wait {left // 60}m {left % 60}s."
    return f"⏱ Wait {left}s."


def wait_view(action, left):
    return embed(wait_text(left), C_GREY, buttons=[btn("⏱ Remind me", "remind", action)], nolog=True)


game_hist = {}


def parse_duration(t):
    m = re.fullmatch(r"(\d+)([smh]?)", t.lower())
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)] if m else None


def game_gate(uid):
    """Max N games per window (set with /set-game-cooldown). Returns an error text or None."""
    if uid in ADMINS:
        return None
    uses, secs = setting("cd_uses", 0), setting("cd_secs", 60)
    if uses <= 0:
        return None  # no limit unless an admin sets one with /set-game-cooldown
    now = time.time()
    h = [t for t in game_hist.get(uid, []) if now - t < secs]
    if len(h) >= uses:
        return f"⏱ Slow down! Max {uses} games per {secs}s. Wait {int(secs - (now - h[0])) + 1}s."
    h.append(now)
    game_hist[uid] = h
    return None


def parse_amount(text, maximum):
    t = text.replace(",", "").lower()
    if t == "all":
        return maximum
    if t == "half":
        return maximum // 2
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", t)
    if not m:
        return None
    return int(float(m.group(1)) * {"": 1, "k": 1000, "m": 1000000}[m.group(2)])


def parse_user(text):
    m = re.match(r"<@(\w+)", text)
    return m.group(1) if m else None


def check_bet(uid, args, game):
    """Returns (bet, None) or (None, error text). Handles k/half/all and the game cooldown. No bet size limits."""
    cash, _ = get(uid)
    bet = parse_amount(args[0], cash) if args else None
    if not bet or bet <= 0 or bet > cash:
        return None, f"❌ Invalid bet. You have {fmt(cash)} cash. (use a number, `5k`, `half` or `all`)"
    gate = game_gate(uid)
    if gate:
        return None, gate
    return bet, None


def with_usage(err, usage):
    """Add the usage hint to real mistakes only, not to 'slow down' messages."""
    return err + usage if err.startswith("❌") else err


def wealth_factor(uid):
    """Payouts shrink as you get richer so nobody snowballs forever (x1 -> x0.55)."""
    c, b = get(uid)
    for limit, f in ((100_000_000, .55), (10_000_000, .65), (1_000_000, .75), (100_000, .85)):
        if c + b >= limit:
            return f
    return 1.0


def win_profit(uid, bet, mult):
    return max(1, int(bet * mult * wealth_factor(uid)))


def inv_amount(uid, name):
    with lock:
        r = db.execute("SELECT amount FROM inv WHERE uid=? AND name=?", (uid, name)).fetchone()
    return r[0] if r else 0


def inv_change(uid, name, delta):
    with lock:
        db.execute("INSERT OR IGNORE INTO inv VALUES (?,?,0)", (uid, name))
        db.execute("UPDATE inv SET amount=amount+? WHERE uid=? AND name=?", (delta, uid, name))
        db.commit()


# ---------- command registration ----------
ALIAS = {"bank": "balance", "lb": "leaderboard", "bj": "blackjack"}
PUBLIC = {"rob", "give", "roulette", "roll", "rps", "ttt"}  # everything else is only visible to you
MENTION = {"rob", "give"}                                    # public AND pings both people
NOLOG = {"ecohelp", "audit"}
cur = threading.local()  # channel / response_url of the command being handled


def cmd(name, keep_case=False):
    def deco(fn):
        def handler(ack, respond, command):
            ack()
            cname = ALIAS.get(command["command"].lstrip("/"), name)
            uid = command["user_id"]
            tokens = (command.get("text") or "").split()
            set_name(uid, command.get("user_name"))
            touch(uid)
            for t in tokens:
                m = re.match(r"<@(\w+)\|([^>]+)>", t)
                if m:
                    set_name(m.group(1), m.group(2))
            # lowercase everything except @mentions (user IDs are case-sensitive)
            args = tokens if keep_case else [t if t.startswith("<") else t.lower() for t in tokens]
            cur.channel, cur.url = command.get("channel_id"), command.get("response_url")
            if uid not in ADMINS and setting(f"ban:{uid}", 0):
                respond("🔒 You're blocked from the economy.", response_type="ephemeral")
                return
            try:
                with lock:  # one command at a time: no double-spending with fast clicks
                    out = fn(uid, args)
            except Exception as e:
                import traceback
                traceback.print_exc()
                respond("Something broke 😅 check the terminal.", response_type="ephemeral")
                return
            nolog = False
            if isinstance(out, dict):
                nolog = out.pop("nolog", False)
                respond(**wire(render(out, False)))
                text = out.get("text", "")
            else:
                private = cname not in PUBLIC or out.startswith(("❌", "⏱", "🔒"))
                respond(render(out, cname in MENTION and not private),
                        response_type="ephemeral" if private else "in_channel")
                text = out
            if cname not in NOLOG and not nolog and not text.startswith(("❌", "⏱", "🔒")):
                clean = re.sub(r"<@(\w+)(\|([^>|]+))?>", lambda m: "@" + (m.group(3) or name_of(m.group(1))),
                               " ".join(tokens))
                glog(uid, f"`/{cname} {clean}`".replace(" `", "`"), text.replace("*", ""))
        app.command("/" + name)(handler)
        for al, target in ALIAS.items():
            if target == name:
                app.command("/" + al)(handler)
        return fn
    return deco


@app.action("show_help")
def show_help(ack, body, respond):
    ack()
    uid = body["user"]["id"]
    respond(text="Hakari commands", blocks=help_blocks(uid in ADMINS), replace_original=False,
            response_type="ephemeral")


# "Remind me" buttons: pings you (only you see it) when a cooldown is over
reminders = set()


@app.action("remind")
def remind(ack, body, respond, client, action):
    ack()
    uid, act = body["user"]["id"], action["value"]
    left = cooldown_left(uid, act, start=False) if act in COOLDOWNS else 0
    if not left:
        only_you(respond, f"✅ `/{act}` is ready right now!")
        return
    if (uid, act) in reminders:
        only_you(respond, "⏱ You already have a reminder set for that.")
        return
    reminders.add((uid, act))
    channel = (body.get("channel") or {}).get("id")

    def fire():
        reminders.discard((uid, act))
        text = f"⏱ <@{uid}> your `/{act}` is ready!"
        try:
            client.chat_postEphemeral(channel=channel, user=uid, text=text)
        except Exception:
            try:
                client.chat_postMessage(channel=uid, text=text)
            except Exception as e:
                print("Reminder failed:", repr(e))
    t = threading.Timer(left, fire)
    t.daemon = True
    t.start()
    only_you(respond, f"⏱ I'll remind you in {left // 60}m {left % 60}s." if left >= 60 else f"⏱ I'll remind you in {left}s.")


# ---------- balance & money ----------
@cmd("balance")
def balance(uid, args):
    target = parse_user(args[0]) if args else uid
    if not target:
        return "❌ Usage: `/bank [@user]`"
    cash, bank = get(target)
    return embed(f"💰 *{tag(target)}'s balance*", C_BLUE, nolog=True,
                 fields=[f"*Cash*\n{fmt(cash)}", f"*Bank*\n{fmt(bank)}", f"*Net worth*\n{fmt(cash + bank)}"],
                 footer="Cash can be robbed. Bank money is safe: /deposit it!")


@cmd("deposit")
def deposit(uid, args):
    cash, _ = get(uid)
    amt = parse_amount(args[0], cash) if args else None
    if not amt or amt <= 0 or amt > cash:
        return f"❌ Usage: `/deposit <amount|half|all>` (you have {fmt(cash)} cash)"
    change(uid, cash=-amt, bank=amt)
    return f"🏦 You deposited {fmt(amt)}."


@cmd("withdraw")
def withdraw(uid, args):
    _, bank = get(uid)
    amt = parse_amount(args[0], bank) if args else None
    if not amt or amt <= 0 or amt > bank:
        return f"❌ Usage: `/withdraw <amount|half|all>` (you have {fmt(bank)} in bank)"
    change(uid, cash=amt, bank=-amt)
    return f"💵 You withdrew {fmt(amt)}."


@cmd("give")
def give(uid, args):
    if len(args) < 2 or not parse_user(args[0]):
        return "❌ Usage: `/give @user <amount|half|all>`"
    target = parse_user(args[0])
    cash, _ = get(uid)
    amt = parse_amount(args[1], cash)
    if target == uid or not amt or amt <= 0 or amt > cash:
        return f"❌ Invalid. You have {fmt(cash)} cash."
    change(uid, cash=-amt)
    change(target, cash=amt)
    return f"✅ {tag(uid)} gave {fmt(amt)} to {tag(target)}."


LB_SIZE = 10


def lb_view(uid, page):
    with lock:
        adm = list(ADMINS)
        rows = db.execute("SELECT uid, cash, bank FROM users").fetchall()
        eligible = []
        for u, c, b in rows:
            if u in adm:
                continue
            tot = int(c) + int(b)
            if tot > 0:
                eligible.append((u, tot))
        eligible.sort(key=lambda x: (-x[1], x[0]))
        total = len(eligible)
        pages = max(1, -(-total // LB_SIZE))
        page = max(1, min(page, pages))
        start = (page - 1) * LB_SIZE
        page_rows = eligible[start:start + LB_SIZE]
        mine = sum(get(uid))
        rank = sum(1 for u, tot in eligible if tot > mine) + 1
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = "\n".join(f"{medals.get(start + i, f'`{start + i}.`')} {tag(u)} • {fmt(t)}"
                      for i, (u, t) in enumerate(page_rows, 1))
    buttons = []
    if page > 1:
        buttons.append(btn("◀ Prev", "lb_page", f"{uid}:{page - 1}"))
    if page < pages:
        buttons.append(btn("Next ▶", "lb_page", f"{uid}:{page + 1}", "primary"))
    return embed(f"🏆 *Leaderboard*\n{lines or 'Nobody here yet.'}", C_GOLD, nolog=True, buttons=buttons or None,
                 footer=f"Page {page}/{pages} · " + (f"You're an admin, so you're hidden from the leaderboard · {fmt(mine)}" if uid in ADMINS
                                                  else f"Your leaderboard rank: {ordinal(rank)} · {fmt(mine)}"))


@cmd("leaderboard")
def leaderboard(uid, args):
    page = int(args[0]) if args and args[0].isdigit() else 1
    return lb_view(uid, page)


@app.action("lb_page")
def lb_page(ack, body, respond, action):
    ack()
    uid, page = action["value"].split(":")
    swap(respond, lb_view(body["user"]["id"], int(page)))


@cmd("stats")
def stats(uid, args):
    with lock:
        rows = db.execute("SELECT cash, bank FROM users").fetchall()
    cash = sum(int(c) for c, _ in rows)
    bank = sum(int(b) for _, b in rows)
    return (f"*📊 Economy stats*\nPlayers: {len(rows):,}\nTotal cash: {fmt(cash)}\n"
            f"Total bank: {fmt(bank)}\nTotal: {fmt(cash + bank)}")


# ---------- income ----------
@cmd("work")
def work(uid, args):
    left = cooldown_left(uid, "work")
    if left:
        return wait_view("work", left)
    pay = random.randint(100, 350)
    job = random.choice(["delivered pizzas 🍕", "fixed some bugs 🐛", "walked dogs 🐕",
                         "mowed lawns 🌱", "freelanced 👨‍💻", "washed cars 🚗", "tutored a kid 📚"])
    change(uid, cash=pay)
    return embed(f"You {job} and earned {fmt(pay)}.", C_GREEN, buttons=[btn("⏱ Remind me", "remind", "work")])


CRIME_WIN = ["pulled off a heist", "hacked an ATM", "sold fake sunglasses", "pickpocketed a tourist",
             "stole a cat and sold it", "ran a shady lemonade stand", "scalped concert tickets"]
CRIME_FAIL = ["trying to steal a cat", "shoplifting", "hacking the school wifi", "selling fake tickets",
              "jaywalking in front of a cop", "pickpocketing an undercover cop", "stealing a street sign"]


@cmd("crime")
def crime(uid, args):
    """60% to win 200-900. If you fail you are fined 10-20% of your cash (at least 25, the rest comes from your bank)."""
    left = cooldown_left(uid, "crime")
    if left:
        return wait_view("crime", left)
    remind = [btn("⏱ Remind me", "remind", "crime")]
    if random.random() < 0.60:
        win = random.randint(200, 900)
        change(uid, cash=win)
        return embed(f"🕵️ You {random.choice(CRIME_WIN)} and got {fmt(win)}.", C_GREEN, buttons=remind)
    cash, bank = get(uid)
    fine = max(25, min(2000, int(max(cash, 0) * random.randint(10, 20) / 100)))
    from_cash = min(max(cash, 0), fine)
    from_bank = min(bank, fine - from_cash)
    change(uid, cash=-from_cash, bank=-from_bank)
    paid = from_cash + from_bank
    return embed(f"🚨 You were caught {random.choice(CRIME_FAIL)}, and have been given a {fmt(paid)} fine!",
                 C_RED, buttons=remind)


@cmd("rob")
def rob(uid, args):
    """50% to steal 40-90% of the target's cash. If you fail you pay them 15-30% of yours (min 50).
    You need 100+ cash to try, the target must be active and hold 50+ cash."""
    if not args or not parse_user(args[0]):
        return "❌ Usage: `/rob @user`"
    target = parse_user(args[0])
    if target == uid:
        return "❌ You can't rob yourself."
    my_cash, _ = get(uid)
    if my_cash < 100 and uid not in ADMINS:
        return f"❌ You need at least {fmt(100)} cash on hand to risk a robbery (you have {fmt(my_cash)})."
    t_cash, _ = get(target)
    with lock:
        seen = db.execute("SELECT last_seen FROM users WHERE uid=?", (target,)).fetchone()[0]
    if not seen or time.time() - seen > ACTIVE_DAYS * 86400:
        return f"❌ {tag(target)} hasn't been around lately, you can only rob active players."
    if t_cash < 50:
        return f"❌ {tag(target)} only has {fmt(t_cash)} cash on hand, not worth it (needs 50+)."
    left = cooldown_left(uid, "rob")
    if left:
        return wait_view("rob", left)
    if random.random() < 0.50:
        pct = random.randint(40, 90)
        win = max(1, int(t_cash * pct / 100))
        change(target, cash=-win)
        change(uid, cash=win)
        return f"🕶️ {tag(uid)} robbed {tag(target)} and took {fmt(win)} ({pct}% of their cash)!"
    loss = min(my_cash, max(50, int(my_cash * random.randint(15, 30) / 100)))
    change(uid, cash=-loss)
    change(target, cash=loss)  # the victim gets the fine
    return f"🚔 {tag(uid)} got caught robbing {tag(target)} and had to pay them {fmt(loss)}!"


@cmd("collect")
def collect(uid, args):
    left = cooldown_left(uid, "collect")
    if left:
        return wait_view("collect", left)
    change(uid, bank=1000)
    return f"🎁 You collected your daily reward of {fmt(1000)} (to bank)."


# ---------- chatting earns coins ----------
@app.event("message")
def on_message(event):
    # ignore bots, edits, joins, DMs, etc.
    if event.get("subtype") or event.get("bot_id") or event.get("channel_type") == "im":
        return
    uid = event.get("user")
    text = (event.get("text") or "").strip()
    if not uid or len(text) < 3:
        return
    now = time.time()
    if now - chat_last.get(uid, 0) < CHAT_COOLDOWN:
        return
    chat_last[uid] = now
    if setting(f"ban:{uid}", 0):
        return
    change(uid, cash=random.randint(CHAT_MIN, CHAT_MAX))
    touch(uid)


@cmd("roll", keep_case=True)
def roll(uid, args):
    """/roll -> d6, /roll 20 -> 1-20, /roll pizza, more pizza, pineapple -> picks one"""
    text = " ".join(args).strip()
    if not text:
        n = random.randint(1, 6)
        return f"🎲 {tag(uid)} rolled *{n}* (1-6)"
    if re.fullmatch(r"\d+", text):
        top = int(text)
        if not 2 <= top <= 1_000_000_000:
            return "❌ Pick a max between 2 and 1,000,000,000."
        return f"🎲 {tag(uid)} rolled *{random.randint(1, top)}* (1-{top})"
    options = [o.strip() for o in text.split(",") if o.strip()]
    if len(options) < 2:
        return "❌ Usage: `/roll [max number]` or `/roll pizza, more pizza, pineapple`"
    options = [re.sub(r"<@\w+(\|[^>]*)?>", "someone", o) for o in options]
    return f"🎲 {tag(uid)} asks the dice… *{random.choice(options)}* wins!"


# ---------- solo games (private: only the player sees them) ----------
def refund_later(store, gid):
    """Refund a solo game nobody finished (only used where abandoning gives no advantage)."""
    def go():
        with lock:
            g = store.pop(gid, None)
            if g:
                change(g["uid"], cash=g["bet"])
    t = threading.Timer(GAME_TIMEOUT, go)
    t.daemon = True
    t.start()


# ----- blackjack -----
games = {}
SUITS = "♠♥♦♣"


def card(c):
    return f"`{ {1: 'A', 11: 'J', 12: 'Q', 13: 'K'}.get(c[0], str(c[0])) }{c[1]}`"


def show(h):
    return " ".join(card(c) for c in h)


def hand_val(h):
    t = sum(min(c[0], 10) for c in h)
    if any(c[0] == 1 for c in h) and t + 10 <= 21:
        t += 10
    return t


def bj_view(g, finished=False):
    if finished:
        label, profit = g["result"]
        color = C_GREEN if profit > 0 else (C_GREY if profit == 0 else C_RED)
        head = f"*Result:* {label}  *{signed(profit)}*"
        dealer = f"{show(g['d'])}\nValue: {hand_val(g['d'])}"
        buttons = [btn("▶️ Play Again", "bj_again", f"{g['uid']}:{g['bet0']}", "primary"), HELP_BTN]
    else:
        color = C_BLUE
        head = f"🃏 *Blackjack* · {tag(g['uid'])} · bet {fmt(g['bet'])}"
        dealer = f"{card(g['d'][0])} `?`\nValue: ?"
        buttons = [btn("Hit", "bj_hit", g["gid"], "primary"), btn("Stand", "bj_stand", g["gid"])]
        if len(g["p"]) == 2 and get(g["uid"])[0] >= g["bet"]:
            buttons.append(btn("Double Down", "bj_double", g["gid"]))
        buttons.append(HELP_BTN)
    text = f"{head}"
    if finished:
        text = f"🃏 *Blackjack* · {tag(g['uid'])} · bet {fmt(g['bet'])}\n{head}"
    return embed(text, color, nolog=True, buttons=buttons, footer=f"Cards remaining: {len(g['deck'])}",
                 fields=[f"*Your Hand*\n{show(g['p'])}\nValue: {hand_val(g['p'])}", f"*Dealer Hand*\n{dealer}"])


def bj_finish(g, natural=False):
    p = hand_val(g["p"])
    while hand_val(g["d"]) < 17 and p <= 21:
        g["d"].append(g["deck"].pop())
    d, bet, uid = hand_val(g["d"]), g["bet"], g["uid"]
    # profit multipliers: win 2x, win with 21 3x, natural blackjack 5x (scaled down for rich players)
    if p > 21:
        label, profit = "Bust", -bet
    elif natural and d != 21:
        label, profit = "Blackjack!", win_profit(uid, bet, 5)
    elif d > 21 or p > d:
        label = ("21! " if p == 21 else "") + ("Dealer busts, you win" if d > 21 else "You win")
        profit = win_profit(uid, bet, 3 if p == 21 else 2)
    elif p == d:
        label, profit = "Push", 0
    else:
        label, profit = "Dealer wins", -bet
    if bet + profit:
        change(uid, cash=bet + profit)
    games.pop(g["gid"], None)
    g["result"] = (label, profit)
    glog(uid, f"played blackjack (bet {fmt(bet)})", f"{label} {signed(profit)}")
    return g["result"]


def bj_timeout(gid):
    """Walked away from a hand? It auto-stands (no free refund for abandoning a bad hand)."""
    with lock:
        g = games.get(gid)
        if g:
            bj_finish(g)


def bj_new(uid, bet_text):
    bet, err = check_bet(uid, [bet_text], "blackjack")
    if err:
        return with_usage(err, "\nUsage: `/bj <bet|half|all>`")
    change(uid, cash=-bet)  # bet is held until the game ends
    decks = max(1, min(8, setting("bj_decks", 3)))
    deck = [(r, s) for _ in range(decks) for r in range(1, 14) for s in SUITS]
    random.shuffle(deck)
    g = {"gid": uuid.uuid4().hex[:8], "uid": uid, "bet": bet, "bet0": bet, "deck": deck,
         "p": [deck.pop(), deck.pop()], "d": [deck.pop(), deck.pop()]}
    games[g["gid"]] = g
    t = threading.Timer(GAME_TIMEOUT, bj_timeout, args=(g["gid"],))
    t.daemon = True
    t.start()
    if hand_val(g["p"]) == 21:
        bj_finish(g, natural=True)
        return bj_view(g, True)
    return bj_view(g)


@cmd("blackjack")
def blackjack(uid, args):
    if args and args[0].startswith("<"):
        return make_challenge(uid, args, "bj")  # blackjack vs a person
    return bj_new(uid, args[0] if args else "")


@app.action(re.compile(r"^bj_(hit|stand|double)$"))
def bj_action(ack, body, respond, action):
    ack()
    with lock:
        g = games.get(action["value"])
        if not g:
            only_you(respond, "That game has ended.")
            return
        if body["user"]["id"] != g["uid"]:
            only_you(respond, "❌ This isn't your game!")
            return
        kind = action["action_id"]
        if kind == "bj_hit":
            g["p"].append(g["deck"].pop())
            if hand_val(g["p"]) < 21:
                swap(respond, bj_view(g))
                return
        elif kind == "bj_double":
            if len(g["p"]) != 2 or get(g["uid"])[0] < g["bet"]:
                only_you(respond, "❌ You can't double down now.")
                return
            change(g["uid"], cash=-g["bet"])
            g["bet"] *= 2
            g["p"].append(g["deck"].pop())
        bj_finish(g)
        swap(respond, bj_view(g, True))


# ----- "play again" buttons for the quick games -----
def again_dispatch(kind, uid, bet_text):
    return {"bj": bj_new, "slots": slots_play, "hl": hl_play, "rr": rr_play}[kind](uid, bet_text)


@app.action(re.compile(r"^(bj|slots|hl|rr)_again$"))
def again_action(ack, body, respond, action):
    ack()
    kind = action["action_id"].split("_")[0]
    uid, bet = action["value"].split(":")
    with lock:
        if body["user"]["id"] != uid:
            only_you(respond, "❌ This isn't your game!")
            return
        v = again_dispatch(kind, uid, bet)
        if isinstance(v, str):
            only_you(respond, v)
        else:
            swap(respond, v)


# ----- slots -----
SLOT_SYMS = ["🍒", "🍋", "🍊", "🍇", "🔔", "💎", "7️⃣"]
SLOT_TRIPLE = {"7️⃣": 30, "💎": 15, "🔔": 8}  # every other symbol: 4x. A pair pays 1x.


def slots_play(uid, bet_text):
    bet, err = check_bet(uid, [bet_text], "slots")
    if err:
        return with_usage(err, "\nUsage: `/slots <bet|half|all>`")
    change(uid, cash=-bet)
    reels = [random.choice(SLOT_SYMS) for _ in range(3)]
    if len(set(reels)) == 1:
        label = "JACKPOT!!!" if reels[0] == "7️⃣" else "Three of a kind!"
        profit = win_profit(uid, bet, SLOT_TRIPLE.get(reels[0], 4))
    elif len(set(reels)) == 2:
        label, profit = "A pair!", win_profit(uid, bet, 1)
    else:
        label, profit = "No match", -bet
    if bet + profit:
        change(uid, cash=bet + profit)
    glog(uid, f"spun the slots (bet {fmt(bet)})", f"{' '.join(reels)} {label} {signed(profit)}")
    color = C_GREEN if profit > 0 else C_RED
    return embed(f"🎰 *Slots* · {tag(uid)} · bet {fmt(bet)}\n\n>  {'   |   '.join(reels)}\n\n*Result:* {label}  *{signed(profit)}*",
                 color, nolog=True, footer="3x 7️⃣ = 30x · 3x 💎 = 15x · 3x 🔔 = 8x · any other 3 = 4x · pair = 1x",
                 buttons=[btn("🎰 Spin Again", "slots_again", f"{uid}:{bet}", "primary"), HELP_BTN])


@cmd("slots")
def slots(uid, args):
    return slots_play(uid, args[0] if args else "")


# ----- higher / lower -----
hl_games = {}


def hl_mult(p):
    return min(0.97 / p, 12.0) if p > 0 else None


def hl_view(g):
    c = g["card"]
    ph, pl = (13 - c[0]) / 13, (c[0] - 1) / 13
    buttons = []
    if ph > 0:
        buttons.append(btn(f"⬆️ Higher  x{hl_mult(ph):.2f}", "hl_higher", g["gid"], "primary"))
    if pl > 0:
        buttons.append(btn(f"⬇️ Lower  x{hl_mult(pl):.2f}", "hl_lower", g["gid"]))
    buttons.append(HELP_BTN)
    return embed(f"🔼 *Higher or Lower* · {tag(g['uid'])} · bet {fmt(g['bet'])}\n\nCurrent card: {card(c)}\n"
                 f"Will the next card be higher or lower? (A is low, K is high, a tie loses)",
                 C_BLUE, nolog=True, buttons=buttons,
                 footer="The payout is bigger when the guess is less likely.")


def hl_play(uid, bet_text):
    bet, err = check_bet(uid, [bet_text], "higher-lower")
    if err:
        return with_usage(err, "\nUsage: `/higher-lower <bet|half|all>`")
    change(uid, cash=-bet)
    g = {"gid": uuid.uuid4().hex[:8], "uid": uid, "bet": bet, "card": (random.randint(1, 13), random.choice(SUITS))}
    hl_games[g["gid"]] = g
    refund_later(hl_games, g["gid"])
    return hl_view(g)


@cmd("higher-lower")
def higher_lower(uid, args):
    return hl_play(uid, args[0] if args else "")


@app.action(re.compile(r"^hl_(higher|lower)$"))
def hl_action(ack, body, respond, action):
    ack()
    with lock:
        g = hl_games.get(action["value"])
        if not g:
            only_you(respond, "That game has ended.")
            return
        uid, bet = g["uid"], g["bet"]
        if body["user"]["id"] != uid:
            only_you(respond, "❌ This isn't your game!")
            return
        hl_games.pop(g["gid"])
        c = g["card"]
        nxt = (random.randint(1, 13), random.choice(SUITS))
        higher = action["action_id"] == "hl_higher"
        p = (13 - c[0]) / 13 if higher else (c[0] - 1) / 13
        won = nxt[0] > c[0] if higher else nxt[0] < c[0]
        if won:
            profit = win_profit(uid, bet, hl_mult(p) - 1)
            change(uid, cash=bet + profit)
        else:
            profit = -bet
        glog(uid, f"played higher-lower (bet {fmt(bet)})", f"{card(c)} then {card(nxt)} {signed(profit)}")
        swap(respond, embed(
            f"🔼 *Higher or Lower* · {tag(uid)} · bet {fmt(bet)}\n\n{card(c)} → {card(nxt)}\n"
            f"*Result:* {'You win!' if won else 'You lose'}  *{signed(profit)}*",
            C_GREEN if won else C_RED, nolog=True,
            buttons=[btn("▶️ Play Again", "hl_again", f"{uid}:{bet}", "primary"), HELP_BTN]))


# ----- russian roulette -----
def rr_play(uid, bet_text):
    bet, err = check_bet(uid, [bet_text], "russian-roulette")
    if err:
        return with_usage(err, "\nUsage: `/russian-roulette <bet|half|all>`")
    change(uid, cash=-bet)
    if random.randint(1, 6) == 1:
        profit, text, color = -bet, "💥 *BANG!* The chamber wasn't empty.", C_RED
    else:
        profit = win_profit(uid, bet, 0.2)
        change(uid, cash=bet + profit)
        text, color = "🔫 *click…* The chamber was empty, you survive!", C_GREEN
    glog(uid, f"played russian roulette (bet {fmt(bet)})", signed(profit))
    return embed(f"{text}\n{tag(uid)} bet {fmt(bet)} · *Result:* *{signed(profit)}*", color, nolog=True,
                 footer="1 bullet in 6 chambers. Survive to win 20% of your bet.",
                 buttons=[btn("🔫 Again", "rr_again", f"{uid}:{bet}", "danger"), HELP_BTN])


@cmd("russian-roulette")
def russian_roulette(uid, args):
    return rr_play(uid, args[0] if args else "")


# ----- rock paper scissors vs the bot -----
rps_games = {}
RPS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


@cmd("rps")
def rps(uid, args):
    if args and args[0].startswith("<"):  # /rps @user [bet] -> play another person
        return make_challenge(uid, args, "rps")
    bet, err = check_bet(uid, args, "rps")
    if err:
        return with_usage(err, "\nUsage: `/rps <bet|half|all>` (vs the bot) or `/rps @user [bet]` (vs a person)")
    change(uid, cash=-bet)  # held until the game ends
    gid = uuid.uuid4().hex[:8]
    rps_games[gid] = {"gid": gid, "uid": uid, "bet": bet}
    refund_later(rps_games, gid)
    buttons = [btn(f"{e} {m.title()}", f"rps_{m}", gid) for m, e in RPS.items()]
    return embed(f"✊ *Rock Paper Scissors* · {tag(uid)} bet {fmt(bet)}\nPick your move!", C_BLUE, nolog=True,
                 buttons=buttons + [HELP_BTN])


# NOTE: patterns are anchored (^...$) so "prps_rock" (2-player buttons) never matches "rps_rock"
@app.action(re.compile(r"^rps_(rock|paper|scissors)$"))
def rps_action(ack, body, respond, action):
    ack()
    with lock:
        g = rps_games.get(action["value"])
        if not g:
            only_you(respond, "That game has ended.")
            return
        if body["user"]["id"] != g["uid"]:
            only_you(respond, "❌ This isn't your game!")
            return
        me = action["action_id"][4:]
        bot_move = random.choice(list(RPS))
        uid, bet = g["uid"], g["bet"]
        if me == bot_move:
            profit, res, color = 0, "Tie, bet returned", C_GREY
        elif BEATS[me] == bot_move:
            profit, res, color = win_profit(uid, bet, 2), "You win!", C_GREEN
        else:
            profit, res, color = -bet, "You lose", C_RED
        if bet + profit:
            change(uid, cash=bet + profit)
        rps_games.pop(action["value"], None)
        glog(uid, f"played rps vs the bot (bet {fmt(bet)})", f"{me} vs {bot_move}: {res} {signed(profit)}")
        swap(respond, embed(f"✊ *Rock Paper Scissors* · {tag(uid)} bet {fmt(bet)}\n"
                            f"You: {RPS[me]} {me}  vs  Bot: {RPS[bot_move]} {bot_move}\n*{res}*  *{signed(profit)}*",
                            color, nolog=True, buttons=[HELP_BTN]))


# ---------- roulette: shared table, everybody in the channel can join ----------
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
roulette_games = {}  # channel id -> running game
ROUL_JOIN, ROUL_MAX = 10, 60  # seconds after each bet / total


def roulette_space(space):
    """-> (label, profit multiplier, test(number)) or None"""
    if space in ("red", "black"):
        return space, 2, lambda n: n != 0 and ((n in RED) == (space == "red"))
    if space in ("odd", "even"):
        return space, 2, lambda n: n != 0 and (n % 2 == (1 if space == "odd" else 0))
    if space in ("1-18", "19-36"):
        lo, hi = map(int, space.split("-"))
        return space, 2, lambda n: lo <= n <= hi
    dozens = {"1st": (1, 12), "2nd": (13, 24), "3rd": (25, 36), "1-12": (1, 12), "13-24": (13, 24), "25-36": (25, 36)}
    if space in dozens:
        lo, hi = dozens[space]
        return f"{lo}-{hi}", 4, lambda n: lo <= n <= hi
    if space.isdigit() and 0 <= int(space) <= 36:
        return space, 70, lambda n: n == int(space)
    return None


ROUL_USAGE = ("\nUsage: `/roulette <bet|half|all> <red|black|odd|even|1st|2nd|3rd|1-12|13-24|25-36|1-18|19-36|0-36>`")


@cmd("roulette")
def roulette(uid, args):
    if len(args) < 2:
        return "❌ Pick a bet and a space." + ROUL_USAGE
    sp = roulette_space(args[1])
    if not sp:
        return "❌ Invalid space." + ROUL_USAGE
    bet, err = check_bet(uid, args, "roulette")
    if err:
        return with_usage(err, ROUL_USAGE)
    ch, now = cur.channel, time.time()
    change(uid, cash=-bet)  # held until the wheel stops
    g = roulette_games.get(ch)
    new = g is None
    if new:
        g = roulette_games[ch] = {"ch": ch, "start": now, "bets": [], "timer": None, "url": cur.url}
    g["bets"].append((uid, bet, args[1], sp))
    left = max(1, int(min(ROUL_JOIN, g["start"] + ROUL_MAX - now)))
    if g["timer"]:
        g["timer"].cancel()
    g["timer"] = threading.Timer(left, roulette_spin, args=(ch,))
    g["timer"].daemon = True
    g["timer"].start()
    players = len({b[0] for b in g["bets"]})
    pot = sum(b[1] for b in g["bets"])
    head = "🎡 *New roulette game started!*" if new else "🎡 *Roulette*"
    return embed(f"{head}\n✅ {tag(uid)} placed a bet of {fmt(bet)} on `{args[1]}`\n"
                 f"{players} player{'s' if players != 1 else ''} · {fmt(pot)} on the table · spinning in *{left}s*",
                 C_BLUE, rt="in_channel", nolog=True,
                 footer=f"Join with /roulette <bet> <space>. Time remaining: {ROUL_JOIN}s after each bet (max {ROUL_MAX}s).")


def roulette_spin(ch):
    with lock:
        g = roulette_games.pop(ch, None)
        if not g:
            return
        n = random.randint(0, 36)
        color = "green" if n == 0 else ("red" if n in RED else "black")
        results = {}
        for uid, bet, raw, (label, mult, test) in g["bets"]:
            r = results.setdefault(uid, [0, 0])
            if test(n):
                profit = win_profit(uid, bet, mult)
                change(uid, cash=bet + profit)
                r[0] += profit
            else:
                r[1] += bet
        lines = []
        for uid, (won, lost) in results.items():
            net = won - lost
            lines.append(f"{'✅' if net > 0 else ('➖' if net == 0 else '❌')} <@{uid}> *{signed(net)}*")
            glog(uid, "played roulette", f"ball on {color} {n}: {signed(net)}")
        total = sum(b[1] for b in g["bets"])
    text = f"🎡 *The ball landed on {color} {n}!*\n" + "\n".join(lines)
    body = {"text": text.replace("*", ""), "attachments": [{"color": {"red": C_RED, "black": C_GREY, "green": C_GREEN}[color],
            "blocks": [section(text), {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{len(results)} player(s) · {fmt(total)} bet in total"}]}]}]}
    try:
        app.client.chat_postMessage(channel=ch, **wire(body))
    except Exception as e:
        print("roulette result via chat.postMessage failed, trying response_url:", repr(e))
        try:
            WebhookClient(g["url"]).send(response_type="in_channel", **wire(body))
        except Exception as e2:
            print("roulette result could not be posted:", repr(e2))


# ---------- 2-player games: challenges, rock paper scissors, tic tac toe (public) ----------
challenges = {}  # waiting for the other person to accept
pvp = {}         # running 2-player games
GAME_NAMES = {"rps": "Rock Paper Scissors", "ttt": "Tic Tac Toe", "bj": "Blackjack"}


def stake_text(bet):
    return f"{fmt(bet)} each" if bet else "no bet"


def pvp_win(winner, bet):
    """Pays the winner both bets + the house bonus. Returns the gain."""
    bonus = bet * PVP_BONUS
    change(winner, cash=bet * 2 + bonus)
    return bet + bonus


def win_text(winner, loser, bet, gain=None):
    gain = bet * (1 + PVP_BONUS) if gain is None else gain
    return f"🏆 {tag(winner)} wins" + (f" *{fmt(gain)}* ({tag(loser)} loses {fmt(bet)})!" if bet else "!")


def forget_challenge(gid):
    with lock:
        challenges.pop(gid, None)


def arm(g):
    """(Re)start the inactivity timer for a running game."""
    if g.get("timer"):
        g["timer"].cancel()
    t = threading.Timer(GAME_TIMEOUT, expire, args=(g["gid"],))
    t.daemon = True
    g["timer"] = t
    t.start()


def expire(gid):
    with lock:
        g = pvp.pop(gid, None)
        if not g:
            return
        change(g["a"], cash=g["bet"])
        change(g["b"], cash=g["bet"])
    try:
        text = f"⌛ {GAME_NAMES[g['kind']]} timed out. Bets were returned."
        v = render({"text": text, "blocks": [section(text)]}, False)
        WebhookClient(g["url"]).send(replace_original=True, **v)
    except Exception as e:
        print("Could not edit expired game:", repr(e))


def make_challenge(uid, args, kind):
    usage = f"\nUsage: `/{kind} @user [bet]`"
    target = parse_user(args[0])
    if not target or target == uid:
        return "❌ Pick someone else to challenge." + usage
    cash, _ = get(uid)
    bet = 0
    if len(args) > 1:
        bet = parse_amount(args[1], cash)
        if bet is None or bet < 0 or bet > cash:
            return f"❌ Invalid bet. You have {fmt(cash)} cash." + usage
    gate = game_gate(uid)
    if gate:
        return gate
    gid = uuid.uuid4().hex[:8]
    challenges[gid] = {"kind": kind, "a": uid, "b": target, "bet": bet}
    t = threading.Timer(GAME_TIMEOUT, forget_challenge, args=(gid,))
    t.daemon = True
    t.start()
    # the challenged person is the only one who gets a real @ping
    text = f"<@{target}> {tag(uid)} challenges you to *{GAME_NAMES[kind]}* ({stake_text(bet)})!"
    return {"text": text, "response_type": "in_channel", "blocks": [
        section(text),
        {"type": "actions", "elements": [btn("Accept", "ch_accept", gid, "primary"),
                                         btn("Decline", "ch_decline", gid, "danger")]}]}


def replace(respond, text, extra_blocks=None):
    v = render({"text": text, "blocks": [section(text)] + (extra_blocks or [])}, False)
    respond(replace_original=True, **v)


@app.action(re.compile(r"^ch_(accept|decline)$"))
def ch_action(ack, body, respond, action):
    ack()
    with lock:
        gid, user = action["value"], body["user"]["id"]
        c = challenges.get(gid)
        if not c:
            only_you(respond, "That challenge expired.")
            return
        if action["action_id"] == "ch_decline":
            if user not in (c["a"], c["b"]):
                only_you(respond, "❌ This challenge isn't for you!")
                return
            challenges.pop(gid)
            replace(respond, f"❌ {tag(user)} cancelled the {GAME_NAMES[c['kind']]} challenge.")
            return
        if user != c["b"]:
            only_you(respond, "❌ This challenge isn't for you!")
            return
        bet = c["bet"]
        if get(c["a"])[0] < bet or get(c["b"])[0] < bet:
            challenges.pop(gid)
            replace(respond, "❌ Challenge cancelled: someone can't afford the bet.")
            return
        challenges.pop(gid)
        change(c["a"], cash=-bet)  # both bets are held until the game ends
        change(c["b"], cash=-bet)
        g = {"gid": gid, "kind": c["kind"], "a": c["a"], "b": c["b"], "bet": bet,
             "url": body.get("response_url"), "timer": None}
        if g["kind"] == "rps":
            g["picks"] = {}
        elif g["kind"] == "bj":
            deck = [(r, sd) for r in range(1, 14) for sd in SUITS]
            random.shuffle(deck)
            g["deck"] = deck
            g["h"] = {g["a"]: [deck.pop(), deck.pop()], g["b"]: [deck.pop(), deck.pop()]}
            g["done"] = {u for u in (g["a"], g["b"]) if hand_val(g["h"][u]) == 21}
        else:
            g["board"] = [None] * 9
            g["turn"] = g["a"]
        pvp[gid] = g
        arm(g)
        if g["kind"] == "bj" and len(g["done"]) == 2:
            pvp.pop(gid, None)
            replace(respond, bj2_result(g))
            return
        view = rps_view(g) if g["kind"] == "rps" else (bj2_view(g) if g["kind"] == "bj" else ttt_view(g))
        respond(replace_original=True, **render(view, False))


# --- 2-player rock paper scissors ---
def rps_view(g):
    picks = g["picks"]
    status = " | ".join(f"{tag(u)} {'✅ picked' if u in picks else '⏳ thinking'}" for u in (g["a"], g["b"]))
    text = f"✊ *Rock Paper Scissors* | {tag(g['a'])} vs {tag(g['b'])} ({stake_text(g['bet'])})\n{status}"
    buttons = [btn(f"{e} {m.title()}", f"prps_{m}", g["gid"]) for m, e in RPS.items()]
    return {"text": text, "blocks": [section(text), {"type": "actions", "elements": buttons}]}


@app.action(re.compile(r"^prps_(rock|paper|scissors)$"))
def prps_action(ack, body, respond, action):
    ack()
    with lock:
        g = pvp.get(action["value"])
        user = body["user"]["id"]
        if not g:
            only_you(respond, "That game has ended.")
            return
        if user not in (g["a"], g["b"]):
            only_you(respond, "❌ This isn't your game!")
            return
        if user in g["picks"]:
            only_you(respond, "You already picked, waiting for your opponent.")
            return
        move = action["action_id"][5:]
        g["picks"][user] = move
        g["url"] = body.get("response_url") or g["url"]
        if len(g["picks"]) < 2:
            arm(g)
            respond(replace_original=True, **render(rps_view(g), False))
            only_you(respond, f"You picked {RPS[move]} {move}. Waiting for your opponent...")
            return
        a, b, bet = g["a"], g["b"], g["bet"]
        pa, pb = g["picks"][a], g["picks"][b]
        if g["timer"]:
            g["timer"].cancel()
        pvp.pop(g["gid"], None)
        head = f"✊ *Rock Paper Scissors* | {tag(a)} {RPS[pa]} {pa}  vs  {tag(b)} {RPS[pb]} {pb}\n"
        if pa == pb:
            change(a, cash=bet)
            change(b, cash=bet)
            res = "Tie, bets returned."
        else:
            winner, loser = (a, b) if BEATS[pa] == pb else (b, a)
            res = win_text(winner, loser, bet, pvp_win(winner, bet))
        glog(a, f"played rps vs {tag(b)}", res.replace("🏆 ", ""))
        replace(respond, head + res)


# --- 2-player blackjack: each person has their own hand, highest total up to 21 wins ---
def bj2_state(g, u):
    v = hand_val(g["h"][u])
    return "💥 bust" if v > 21 else ("✋ stood" if u in g["done"] else "🤔 playing")


def bj2_view(g):
    text = (f"🃏 *Blackjack* | {tag(g['a'])} vs {tag(g['b'])} ({stake_text(g['bet'])})\n"
            + "\n".join(f"{tag(u)}: {bj2_state(g, u)} · {len(g['h'][u])} cards" for u in (g["a"], g["b"])))
    buttons = [btn("Hit", "pbj_hit", g["gid"], "primary"), btn("Stand", "pbj_stand", g["gid"]),
               btn("👀 My hand", "pbj_hand", g["gid"])]
    return {"text": text, "blocks": [section(text), {"type": "actions", "elements": buttons}]}


def bj2_result(g):
    """Pays out and returns the final message text."""
    a, b, bet = g["a"], g["b"], g["bet"]
    va, vb = hand_val(g["h"][a]), hand_val(g["h"][b])
    sa, sb = (0 if va > 21 else va), (0 if vb > 21 else vb)
    head = (f"🃏 *Blackjack* | {tag(a)} {show(g['h'][a])} = *{va}*  vs  "
            f"{tag(b)} {show(g['h'][b])} = *{vb}*\n")
    if sa == sb:
        change(a, cash=bet)
        change(b, cash=bet)
        res = "Tie, bets returned."
    else:
        winner, loser = (a, b) if sa > sb else (b, a)
        res = win_text(winner, loser, bet, pvp_win(winner, bet))
    glog(a, f"played blackjack vs {tag(b)}", res.replace("🏆 ", ""))
    return head + res


@app.action(re.compile(r"^pbj_(hit|stand|hand)$"))
def pbj_action(ack, body, respond, action):
    ack()
    with lock:
        g = pvp.get(action["value"])
        user = body["user"]["id"]
        if not g:
            only_you(respond, "That game has ended.")
            return
        if user not in (g["a"], g["b"]):
            only_you(respond, "❌ This isn't your game!")
            return
        h = g["h"][user]
        kind = action["action_id"][4:]
        mine = lambda: f"Your hand: {show(h)} = *{hand_val(h)}*"
        if kind == "hand":
            only_you(respond, mine())
            return
        if user in g["done"]:
            only_you(respond, f"You're done, waiting for your opponent.\n{mine()}")
            return
        g["url"] = body.get("response_url") or g["url"]
        if kind == "hit":
            h.append(g["deck"].pop())
            if hand_val(h) >= 21:
                g["done"].add(user)
        else:
            g["done"].add(user)
        busted = any(hand_val(g["h"][u]) > 21 for u in (g["a"], g["b"]))
        if busted or len(g["done"]) == 2:
            if g["timer"]:
                g["timer"].cancel()
            pvp.pop(g["gid"], None)
            replace(respond, bj2_result(g))
            return
        arm(g)
        respond(replace_original=True, **render(bj2_view(g), False))
        only_you(respond, mine())


# --- tic tac toe (board is emoji text + a dropdown, because Slack mobile stacks buttons vertically) ---
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
CELL = {None: "⬜", "X": "❌", "O": "⭕"}
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def ttt_head(g):
    return f"*Tic Tac Toe* | ❌ {tag(g['a'])} vs ⭕ {tag(g['b'])} ({stake_text(g['bet'])})"


def board_text(board, numbers=False):
    def c(i):
        return NUMS[i] if numbers and not board[i] else CELL[board[i]]
    return "\n".join("".join(c(r * 3 + k) for k in range(3)) for r in range(3))


def ttt_view(g):
    mark = "❌" if g["turn"] == g["a"] else "⭕"
    text = f"{ttt_head(g)}\nTurn: {mark} {tag(g['turn'])}\n\n{board_text(g['board'], True)}"
    options = [{"text": {"type": "plain_text", "text": f"Square {i + 1}"}, "value": f"{g['gid']}:{i}"}
               for i in range(9) if not g["board"][i]]
    blocks = [section(text), {"type": "actions", "elements": [
        {"type": "static_select", "action_id": "ttt_pick",
         "placeholder": {"type": "plain_text", "text": "Pick a square (1-9)"}, "options": options}]}]
    return {"text": text, "blocks": blocks}


@cmd("ttt")
def ttt(uid, args):
    if not args or not args[0].startswith("<"):
        return "❌ Usage: `/ttt @user [bet]`"
    return make_challenge(uid, args, "ttt")


@app.action("ttt_pick")
def ttt_action(ack, body, respond, action):
    ack()
    with lock:
        gid, idx = action["selected_option"]["value"].split(":")
        g = pvp.get(gid)
        user = body["user"]["id"]
        if not g:
            only_you(respond, "That game has ended.")
            return
        if user not in (g["a"], g["b"]):
            only_you(respond, "❌ This isn't your game!")
            return
        if user != g["turn"]:
            only_you(respond, "❌ It's not your turn!")
            return
        i = int(idx)
        if g["board"][i]:
            only_you(respond, "❌ That square is taken.")
            return
        mark = "X" if user == g["a"] else "O"
        g["board"][i] = mark
        g["url"] = body.get("response_url") or g["url"]
        won = any(all(g["board"][x] == mark for x in line) for line in LINES)
        full = all(g["board"])
        if won or full:
            if g["timer"]:
                g["timer"].cancel()
            pvp.pop(g["gid"], None)
            bet = g["bet"]
            if won:
                other = g["b"] if user == g["a"] else g["a"]
                res = win_text(user, other, bet, pvp_win(user, bet))
            else:
                change(g["a"], cash=bet)
                change(g["b"], cash=bet)
                res = "Draw, bets returned."
            glog(g["a"], f"played tic tac toe vs {tag(g['b'])}", res.replace("🏆 ", ""))
            replace(respond, f"{ttt_head(g)}\n{board_text(g['board'])}\n{res}")
            return
        g["turn"] = g["b"] if user == g["a"] else g["a"]
        arm(g)
        respond(replace_original=True, **render(ttt_view(g), False))


# ---------- items ----------
@cmd("catalog")
def catalog(uid, args):
    with lock:
        if args:
            r = db.execute("SELECT name, price, description FROM items WHERE name=?", (args[0],)).fetchone()
            return f"*{r[0]}* | {fmt(r[1])}\n{r[2]}" if r else "❌ Item not found."
        rows = db.execute("SELECT name, price FROM items ORDER BY price").fetchall()
    if not rows:
        return "❌ No items yet. An admin can use `/create-item`."
    return "*🛒 Catalog*\n" + "\n".join(f"`{n}` • {fmt(p)}" for n, p in rows) + "\nDetails: `/catalog <item>`"


@cmd("buy")
def buy(uid, args):
    if not args:
        return "❌ Usage: `/buy <item> [amount]`"
    amount = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    with lock:
        item = db.execute("SELECT price FROM items WHERE name=?", (args[0],)).fetchone()
    if not item:
        return "❌ Item not found."
    total = item[0] * amount
    cash, _ = get(uid)
    if amount < 1 or total > cash:
        return f"❌ Not enough cash. Cost: {fmt(total)}, you have {fmt(cash)}."
    change(uid, cash=-total)
    inv_change(uid, args[0], amount)
    return f"🛍️ You bought {amount}x `{args[0]}` for {fmt(total)}."


@cmd("inventory")
def inventory(uid, args):
    with lock:
        rows = db.execute("SELECT name, amount FROM inv WHERE uid=? AND amount>0", (uid,)).fetchall()
    if not rows:
        return "❌ Your inventory is empty."
    return "*🎒 Inventory*\n" + "\n".join(f"`{n}` x{a}" for n, a in rows)


@cmd("use")
def use(uid, args):
    if not args:
        return "❌ Usage: `/use <item> [amount]`"
    amount = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    if amount < 1 or inv_amount(uid, args[0]) < amount:
        return "❌ You don't have enough of that item."
    inv_change(uid, args[0], -amount)
    return f"✅ You used {amount}x `{args[0]}`."


# ---------- admin (hidden from the help everyone sees; replies only visible to the admin, logged in #gamblelogs) ----------
@cmd("add-money")
def add_money(uid, args):
    if uid not in ADMINS:
        return "🔒 Admins only."
    if len(args) < 2 or not parse_user(args[0]) or not args[1].isdigit():
        return "❌ Usage: `/add-money @user <amount>`"
    change(parse_user(args[0]), cash=int(args[1]))
    return f"✅ Added {fmt(int(args[1]))} to {tag(parse_user(args[0]))}."


@cmd("create-item", keep_case=True)
def create_item(uid, args):
    if uid not in ADMINS:
        return "🔒 Admins only."
    if len(args) < 3 or not args[1].isdigit():
        return "❌ Usage: `/create-item <name> <price> <description>`"
    name = args[0].lower()
    with lock:
        db.execute("INSERT OR REPLACE INTO items VALUES (?,?,?)", (name, int(args[1]), " ".join(args[2:])))
        db.commit()
    return f"✅ Item `{name}` created for {fmt(int(args[1]))}."


@cmd("delete-item")
def delete_item(uid, args):
    if uid not in ADMINS:
        return "🔒 Admins only."
    if not args:
        return "❌ Usage: `/delete-item <name>`"
    return remove_item(args[0].lower())


def remove_item(name):
    with lock:
        n = db.execute("DELETE FROM items WHERE name=?", (name,)).rowcount
        held = db.execute("DELETE FROM inv WHERE name=?", (name,)).rowcount
        db.commit()
    return f"✅ Item `{name}` deleted (removed from {held} inventories)." if n else f"❌ No item called `{name}`."


ADMIN_HELP = (
    "*🛠 /admin commands*\n"
    "`nuke @user`: wipe their cash and bank\n"
    "`nuke all confirm`: wipe EVERYONE's cash and bank\n"
    "`cap @user <amount>` / `cap all <amount>`: cut total coins down to the amount (cash first)\n"
    "`set @user <cash> [bank]` · `take @user <amount|all>` · `giveall <amount>` (cash to everyone)\n"
    "`undo`: restore the last nuke/cap/set/take\n"
    "`resetcd @user|all`: clear cooldowns\n"
    "`ban @user` / `unban @user`: block from the economy · `info @user`: balance\n"
    "`item-remove <name>`: delete an item (also `/delete-item`)\n"
    "Money changes can be checked with `/audit @user`.")


def snapshot(uids):
    with lock:
        db.execute("DELETE FROM snap")
        for u in uids:
            c, b = get(u)
            db.execute("INSERT INTO snap VALUES (?,?,?)", (u, str(c), str(b)))
        db.commit()


def set_money(u, cash, bank):
    c, b = get(u)
    change(u, cash=int(cash) - c, bank=int(bank) - b)


def all_uids():
    with lock:
        return [r[0] for r in db.execute("SELECT uid FROM users").fetchall()]


def cap_total(u, amt):
    c, b = get(u)
    if c + b <= amt:
        return False
    nc = min(c, amt)
    set_money(u, nc, min(b, amt - nc))
    return True


@cmd("admin")
def admin_cmd(uid, args):
    if uid not in ADMINS:
        return "🔒 Admins only."
    if not args or args[0] == "help":
        return ADMIN_HELP
    sub, rest = args[0], args[1:]
    tgt = parse_user(rest[0]) if rest else None
    if sub == "nuke":
        if rest and rest[0] == "all":
            if rest[1:] != ["confirm"]:
                return "⚠️ This wipes EVERYONE's cash and bank. Run `/admin nuke all confirm` to do it (`/admin undo` can restore it)."
            ids = all_uids()
            snapshot(ids)
            for u in ids:
                set_money(u, 0, 0)
            return f"☢️ Nuked everyone ({len(ids)} players) to 0."
        if not tgt:
            return "❌ Usage: `/admin nuke @user` or `/admin nuke all confirm`"
        snapshot([tgt])
        set_money(tgt, 0, 0)
        return f"☢️ Nuked {tag(tgt)} to 0."
    if sub == "cap":
        amt = parse_amount(rest[1], 0) if len(rest) > 1 else None
        if amt is None or not (tgt or rest[0] == "all"):
            return "❌ Usage: `/admin cap @user <amount>` or `/admin cap all <amount>`"
        ids = all_uids() if rest[0] == "all" else [tgt]
        snapshot(ids)
        n = sum(cap_total(u, amt) for u in ids)
        return f"✅ Capped {n} player(s) to {fmt(amt)} total."
    if sub == "set":
        if not tgt or len(rest) < 2 or not rest[1].isdigit() or (len(rest) > 2 and not rest[2].isdigit()):
            return "❌ Usage: `/admin set @user <cash> [bank]`"
        snapshot([tgt])
        set_money(tgt, int(rest[1]), int(rest[2]) if len(rest) > 2 else get(tgt)[1])
        return f"✅ {tag(tgt)} now has {fmt(get(tgt)[0])} cash and {fmt(get(tgt)[1])} bank."
    if sub == "take":
        if not tgt or len(rest) < 2:
            return "❌ Usage: `/admin take @user <amount|all>`"
        c, b = get(tgt)
        amt = parse_amount(rest[1], c + b)
        if not amt or amt < 1:
            return "❌ Usage: `/admin take @user <amount|all>`"
        amt = min(amt, c + b)
        snapshot([tgt])
        from_cash = min(c, amt)
        change(tgt, cash=-from_cash, bank=-(amt - from_cash))
        return f"✅ Took {fmt(amt)} from {tag(tgt)}."
    if sub == "giveall":
        amt = parse_amount(rest[0], 0) if rest else None
        if not amt:
            return "❌ Usage: `/admin giveall <amount>`"
        ids = all_uids()
        for u in ids:
            change(u, cash=amt)
        return f"🎁 Gave {fmt(amt)} to {len(ids)} players."
    if sub == "undo":
        with lock:
            rows = db.execute("SELECT uid, cash, bank FROM snap").fetchall()
            db.execute("DELETE FROM snap")
            db.commit()
        if not rows:
            return "❌ Nothing to undo."
        for u, c, b in rows:
            set_money(u, int(c), int(b))
        return f"↩️ Restored {len(rows)} player(s) to how they were before the last change."
    if sub == "resetcd":
        with lock:
            if tgt:
                db.execute("DELETE FROM cooldowns WHERE uid=?", (tgt,))
                game_hist.pop(tgt, None)
            elif rest and rest[0] == "all":
                db.execute("DELETE FROM cooldowns")
                game_hist.clear()
            else:
                return "❌ Usage: `/admin resetcd @user` or `/admin resetcd all`"
            db.commit()
        return "✅ Cooldowns cleared."
    if sub in ("ban", "unban"):
        if not tgt:
            return f"❌ Usage: `/admin {sub} @user`"
        set_setting(f"ban:{tgt}", 1 if sub == "ban" else 0)
        return f"✅ {tag(tgt)} is {'blocked from' if sub == 'ban' else 'allowed back into'} the economy."
    if sub == "info":
        if not tgt:
            return "❌ Usage: `/admin info @user`"
        c, b = get(tgt)
        return f"ℹ️ {tag(tgt)}: cash {fmt(c)} · bank {fmt(b)}{' · 🔒 blocked' if setting(f'ban:{tgt}', 0) else ''}"
    if sub in ("item-remove", "remove-item", "delete-item"):
        return remove_item(rest[0]) if rest else "❌ Usage: `/admin item-remove <name>`"
    return ADMIN_HELP


@cmd("set-blackjack-decks")
def set_blackjack_decks(uid, args):
    if uid not in ADMINS:
        return "🔒 Admins only."
    if not args or not args[0].isdigit() or not 1 <= int(args[0]) <= 8:
        return "❌ Usage: `/set-blackjack-decks <1-8>`"
    set_setting("bj_decks", int(args[0]))
    return f"✅ Blackjack now uses {args[0]} deck(s)."


@cmd("set-game-cooldown")
def set_game_cooldown(uid, args):
    if uid not in ADMINS:
        return "🔒 Admins only."
    secs = parse_duration(args[1]) if len(args) > 1 else None
    if len(args) < 2 or not args[0].isdigit() or int(args[0]) < 1 or not secs:
        return "❌ Usage: `/set-game-cooldown <usages> <duration>`\nExample: `/set-game-cooldown 4 5m` (4 games per 5 minutes)"
    set_setting("cd_uses", int(args[0]))
    set_setting("cd_secs", secs)
    return f"✅ Players can now play {args[0]} games per {secs}s."


@cmd("audit")
def audit(uid, args):
    if uid not in ADMINS:
        return "🔒 Admins only."
    if not args or not parse_user(args[0]):
        return "❌ Usage: `/audit @user`"
    target = parse_user(args[0])
    with lock:
        rows = db.execute("SELECT ts, cash, bank, why FROM log WHERE uid=? ORDER BY ts DESC LIMIT 20",
                          (target,)).fetchall()
    if not rows:
        return "No logged money moves for that user yet."
    lines = [f"`{time.strftime('%d %H:%M:%S', time.gmtime(t))}` cash {int(c):+,} bank {int(b):+,} ({w})"
             for t, c, b, w in rows]
    return f"*Last moves (UTC)* for {tag(target)}\n" + "\n".join(lines)


# ---------- help (also posted when someone @mentions the bot) ----------
INTRO = "Yo, this is my gambling bot, check out commands:"


def help_blocks(admin=False):
    parts = [
        f"*💰 Money*\n"
        "`/bank [@user]` – see your cash, bank and net worth (also `/balance`). Cash can be robbed, bank can't.\n"
        "`/deposit <amount|half|all>` – move cash into your bank so nobody can rob it.\n"
        "`/withdraw <amount|half|all>` – take money out of the bank.\n"
        "`/give @user <amount>` – send cash to someone. They get pinged.\n"
        "`/lb` – leaderboard: the richest players, 10 per page, and your own rank (also `/leaderboard`).\n"
        "`/stats` – how many coins exist in total.",

        "*⛏️ Earn coins*\n"
        "`/work` – safe job, pays 100-350. Every 60s.\n"
        "`/crime` – 60% win 200-900 coins, but if you get caught you pay a fine (10-20% of your cash, min 25, max 2,000). Every 60s.\n"
        "`/rob @user` – 50% chance to steal 40-90% of their *cash*. If you fail you pay them 15-30% of yours. "
        "You need 100+ cash, they need 50+ and must be active. Both of you get pinged. Every 2 min.\n"
        "`/collect` – daily reward of 1,000 (goes to your bank).\n"
        "💬 Just chatting gives 10-30 coins every 30s.",

        "*🎰 Games vs the bot* (only you see them)\n"
        "`/bj <bet>` – blackjack (also `/blackjack`). Beat the dealer without going over 21. "
        "Win pays 2x, a 21 pays 3x, natural blackjack 5x. Buttons: Hit, Stand, Double Down. "
        "Or `/bj @user [bet]` to play a person: highest hand up to 21 wins.\n"
        "`/slots <bet>` – 3 reels. Pair 1x, three of a kind 4x, up to 30x for triple 7️⃣.\n"
        "`/higher-lower <bet>` – guess if the next card is higher or lower. Riskier guesses pay more.\n"
        "`/russian-roulette <bet>` – 1 bullet in 6. Survive and win 20%, else lose the bet.\n"
        "`/rps <bet>` – rock paper scissors against the bot, win 2x.",

        "*🎡 Roulette* (shared table, everybody can join!)\n"
        "`/roulette <bet> <space>` – the first bet opens a table, anyone can join with their own bet. "
        "The wheel spins 15s after the last bet (max 60s) and everybody gets paid at the same time.\n"
        "Spaces: `red` `black` `odd` `even` `1-18` `19-36` pay 2x · `1st` `2nd` `3rd` (or `1-12` `13-24` `25-36`) pay 4x · "
        "a single number `0-36` pays 70x.",

        "*🤝 Play against people*\n"
        "`/rps @user [bet]` – rock paper scissors vs a person.\n"
        "`/ttt @user [bet]` – tic tac toe vs a person (pick squares from the dropdown).\n"
        "They get pinged and must accept. The winner nets 2x the bet.\n"
        "`/roll [max]` – roll a dice, or `/roll pizza, more pizza, pineapple` to pick for you.",

        "*🛒 Shop*\n"
        "`/catalog [item]` – items for sale. `/buy <item> [amount]` · `/inventory` · `/use <item> [amount]`",

        f"*ℹ️ Good to know*\n"
        f"Bets accept numbers, `5k`, `half` or `all`. There are no bet limits. "
        "Payouts shrink a little when you're very rich (over 100k) so nobody snowballs forever. "
        "Money and solo-game commands are only visible to you; rob, give, roulette and challenges are public.\n"
        "🏆 Top the `/lb` and you can win prizes too!",
    ]
    if admin:
        parts.append("*🛠 Admin (only you can see this)*\n"
                     "`/admin help` lists the admin tools (nuke, cap, set, take, undo, ban, item-remove…)\n"
                     "`/add-money @user <amount>` · `/create-item <name> <price> <description>` · `/delete-item <name>` · `/audit @user`\n"
                     "`/set-blackjack-decks <1-8>` · "
                     "`/set-game-cooldown <usages> <duration>` (e.g. `4 5m`)")
    return [section(INTRO)] + [section(p) for p in parts]


@cmd("ecohelp")
def ecohelp(uid, args):
    return {"text": INTRO, "blocks": help_blocks(uid in ADMINS), "response_type": "ephemeral", "nolog": True}


@app.event("app_mention")
def on_mention(event, say):
    # public reply (in a thread) so everyone sees the commands. Never includes admin commands.
    print("Received app_mention:", event.get("user"), event.get("channel"), event.get("text"))
    kwargs = {"text": INTRO, "blocks": help_blocks(False)}
    thread = event.get("thread_ts") or event.get("ts")
    if thread:
        kwargs["thread_ts"] = thread
    try:
        say(**kwargs)
    except Exception as e:
        print("app_mention reply failed:", repr(e))
        try:
            say(text=INTRO, blocks=help_blocks(False))
        except Exception as e2:
            print("app_mention fallback failed:", repr(e2))


def keep_alive_server():
    """Tiny web page so hosts like Render (which want an open port) accept the bot.
    Point UptimeRobot at the service URL so the free instance never sleeps."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(("Hakari is running\n" + "\n".join(f"{k}: {v}" for k, v in status.items())).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", int(os.environ["PORT"])), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


if __name__ == "__main__":
    if os.environ.get("PORT"):
        keep_alive_server()
    if GH_TOKEN and GH_REPO:
        threading.Thread(target=backup_loop, daemon=True).start()

        def on_stop(*_):  # host is restarting us: save right now
            backup_db()
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_stop)
    print("Starting bot...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
