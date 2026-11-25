import os, re, sys, time, random, sqlite3, threading, uuid
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.webhook import WebhookClient

sys.stdout.reconfigure(line_buffering=True)  # show print() lines in the Render logs right away

if "SLACK_BOT_TOKEN" not in os.environ or "SLACK_APP_TOKEN" not in os.environ:
    sys.exit("Missing tokens. Run the two `export` lines first (see instructions).")

app = App(token=os.environ["SLACK_BOT_TOKEN"])

ADMINS = {"U0C7S6T72DD"}  # Slack member IDs allowed to use admin commands

# Replies in this list are only visible to the person who used the command.
# Solo games (blackjack, roulette, rps vs the bot) are private too.
PRIVATE = {"balance", "deposit", "withdraw", "inventory", "catalog", "buy", "use",
           "ecohelp", "stats", "collect", "work", "crime", "leaderboard",
           "blackjack", "roulette", "audit"}
# Commands that involve another person: public, and both people get a REAL @ping.
# (Errors like "wait 30s" still stay private to the person who ran the command.)
MENTION = {"rob", "give"}

# ---------- database ----------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eco.db")

# ---------- free cloud backup of the coins (private GitHub repo) ----------
# Set GITHUB_TOKEN (fine-grained token, "Contents: read & write" on ONE private repo)
# and GITHUB_REPO ("yourname/hakari-data"). The database is restored on start and
# saved every 60 seconds when something changed, so coins survive restarts on hosts with a wiped disk.
import base64, json, signal, urllib.request, urllib.error

GH_TOKEN, GH_REPO = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
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
    if os.path.exists(DB_PATH):
        synced = True
        status["restore"] = "kept the local database file"
        return
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
            status["restore"] = "no backup file yet (first run), starting fresh"
            print("No backup file yet (first run), starting fresh.")
        else:
            sys.exit(f"!! Could not read the coin backup (HTTP {e.code}). Refusing to start so the saved "
                     f"coins are not overwritten. Check GITHUB_TOKEN (Contents: read+write) and GITHUB_REPO.")
    except Exception as e:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
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
                    gh_sha = None  # refetch latest sha on next attempt
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
try:
    db.execute("ALTER TABLE users ADD COLUMN name TEXT")
except sqlite3.OperationalError:
    pass
db.commit()

COIN = "🪙"
COOLDOWNS = {"work": 60, "crime": 60, "rob": 120, "collect": 86400}  # seconds (admins have none)
GAME_TIMEOUT = 300  # seconds before an unfinished game is cancelled and refunded

# chatting earns coins: every CHAT_COOLDOWN seconds a message pays CHAT_MIN..CHAT_MAX
CHAT_COOLDOWN = 30
CHAT_MIN, CHAT_MAX = 10, 30
chat_last = {}


def fmt(n):
    return f"{COIN} {int(n):,}"


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
    """Placeholder for a person. Turned into a real mention (private replies)
    or a bold name (public replies) right before sending."""
    return f"⟦{uid}⟧"


def render(obj, private):
    if isinstance(obj, str):
        return MARK.sub(lambda m: f"<@{m.group(1)}>" if private else f"*{name_of(m.group(1))}*", obj)
    if isinstance(obj, dict):
        return {k: render(v, private) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, private) for v in obj]
    return obj


def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def cooldown_left(uid, action):
    """Returns seconds left. If 0, the cooldown is (re)started now."""
    if uid in ADMINS:
        return 0
    now = time.time()
    with lock:
        row = db.execute("SELECT ts FROM cooldowns WHERE uid=? AND action=?", (uid, action)).fetchone()
        if row and now - row[0] < COOLDOWNS[action]:
            return int(COOLDOWNS[action] - (now - row[0])) + 1
        db.execute("INSERT OR REPLACE INTO cooldowns VALUES (?,?,?)", (uid, action, now))
        db.commit()
    return 0


def wait_text(left):
    if left >= 3600:
        return f"⏱ Wait {left // 3600}h {(left % 3600) // 60}m."
    if left >= 60:
        return f"⏱ Wait {left // 60}m {left % 60}s."
    return f"⏱ Wait {left}s."


def parse_amount(text, maximum):
    if text == "all":
        return maximum
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def parse_user(text):
    m = re.match(r"<@(\w+)", text)
    return m.group(1) if m else None


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
def cmd(name, keep_case=False):
    def deco(fn):
        @app.command("/" + name)
        def handler(ack, respond, command):
            ack()
            text = command.get("text", "") or ""
            tokens = text.split()
            set_name(command["user_id"], command.get("user_name"))
            for t in tokens:
                m = re.match(r"<@(\w+)\|([^>]+)>", t)
                if m:
                    set_name(m.group(1), m.group(2))
            # lowercase everything except @mentions (user IDs are case-sensitive)
            args = tokens if keep_case else [t if t.startswith("<") else t.lower() for t in tokens]
            try:
                with lock:  # one command at a time: no double-spending with fast clicks
                    out = fn(command["user_id"], args)
            except Exception as e:
                print("Error:", repr(e))
                respond("Something broke 😅 check the terminal.", response_type="ephemeral")
                return
            if isinstance(out, dict):
                private = out.get("response_type") == "ephemeral"
                respond(**render(out, False))
            else:
                private = name in PRIVATE or out.startswith(("❌", "⏱", "🔒"))
                ping = name in MENTION and not private
                respond(render(out, ping), response_type="ephemeral" if private else "in_channel")
        return fn
    return deco


# ---------- balance & money ----------
@cmd("balance")
def balance(uid, args):
    target = parse_user(args[0]) if args else uid
    if not target:
        return "❌ Usage: `/balance [@user]`"
    cash, bank = get(target)
    return f"{tag(target)}: Cash {fmt(cash)} | Bank {fmt(bank)} | Net worth {fmt(cash + bank)}"


@cmd("deposit")
def deposit(uid, args):
    cash, _ = get(uid)
    amt = parse_amount(args[0], cash) if args else None
    if not amt or amt <= 0 or amt > cash:
        return f"❌ Usage: `/deposit <amount|all>` (you have {fmt(cash)} cash)"
    change(uid, cash=-amt, bank=amt)
    return f"🏦 You deposited {fmt(amt)}."


@cmd("withdraw")
def withdraw(uid, args):
    _, bank = get(uid)
    amt = parse_amount(args[0], bank) if args else None
    if not amt or amt <= 0 or amt > bank:
        return f"❌ Usage: `/withdraw <amount|all>` (you have {fmt(bank)} in bank)"
    change(uid, cash=amt, bank=-amt)
    return f"💵 You withdrew {fmt(amt)}."


@cmd("give")
def give(uid, args):
    if len(args) < 2 or not parse_user(args[0]):
        return "❌ Usage: `/give @user <amount|all>`"
    target = parse_user(args[0])
    cash, _ = get(uid)
    amt = parse_amount(args[1], cash)
    if target == uid or not amt or amt <= 0 or amt > cash:
        return f"❌ Invalid. You have {fmt(cash)} cash."
    change(uid, cash=-amt)
    change(target, cash=amt)
    return f"✅ {tag(uid)} gave {fmt(amt)} to {tag(target)}."


@cmd("leaderboard")
def leaderboard(uid, args):
    with lock:
        rows = db.execute("SELECT uid, cash, bank FROM users").fetchall()
    if not rows:
        return "❌ Nobody has played yet!"
    ranked = sorted(((u, int(c) + int(b)) for u, c, b in rows), key=lambda x: x[1], reverse=True)[:10]
    return "*🏆 Leaderboard*\n" + "\n".join(f"{i}. {tag(u)} • {fmt(t)}" for i, (u, t) in enumerate(ranked, 1))


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
        return wait_text(left)
    pay = random.randint(100, 350)
    job = random.choice(["delivered pizzas 🍕", "fixed some bugs 🐛", "walked dogs 🐕",
                         "mowed lawns 🌱", "freelanced 👨‍💻"])
    change(uid, cash=pay)
    return f"You {job} and earned {fmt(pay)}."


@cmd("crime")
def crime(uid, args):
    """60% to win 200-900. If you fail you only lose 5-10% of your cash (never more than you hold)."""
    left = cooldown_left(uid, "crime")
    if left:
        return wait_text(left)
    if random.random() < 0.60:
        win = random.randint(200, 900)
        change(uid, cash=win)
        return f"🕵️ You pulled off a heist and got {fmt(win)}."
    cash, _ = get(uid)
    loss = min(max(cash, 0), int(max(cash, 0) * random.randint(5, 10) / 100))
    change(uid, cash=-loss)
    return f"🚨 You got busted and were fined {fmt(loss)}."


@cmd("rob")
def rob(uid, args):
    """50% to steal 40-90% of the target's cash. If you fail you pay the target 15-30% of your cash
    (minimum 50, capped at what you hold). Public message, both people get pinged."""
    if not args or not parse_user(args[0]):
        return "❌ Usage: `/rob @user`"
    target = parse_user(args[0])
    if target == uid:
        return "❌ You can't rob yourself."
    t_cash, _ = get(target)
    if t_cash < 50:
        return f"❌ {tag(target)} only has {fmt(t_cash)} cash on hand, not worth it (needs 50+)."
    left = cooldown_left(uid, "rob")
    if left:
        return wait_text(left)
    my_cash, _ = get(uid)
    if random.random() < 0.50:
        pct = random.randint(40, 90)
        win = max(1, int(t_cash * pct / 100))
        change(target, cash=-win)
        change(uid, cash=win)
        return f"🕶️ {tag(uid)} robbed {tag(target)} and took {fmt(win)} ({pct}% of their cash)!"
    loss = min(max(my_cash, 0), max(50, int(max(my_cash, 0) * random.randint(15, 30) / 100)))
    change(uid, cash=-loss)
    change(target, cash=loss)  # the victim gets the fine
    return f"🚔 {tag(uid)} got caught robbing {tag(target)} and had to pay them {fmt(loss)}!"


@cmd("collect")
def collect(uid, args):
    left = cooldown_left(uid, "collect")
    if left:
        return wait_text(left)
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
    change(uid, cash=random.randint(CHAT_MIN, CHAT_MAX))


# ---------- solo games (private: only the player sees them) ----------
def check_bet(uid, args):
    cash, _ = get(uid)
    bet = parse_amount(args[0], cash) if args else None
    if not bet or bet <= 0 or bet > cash:
        return None, f"❌ Invalid bet. You have {fmt(cash)} cash."
    return bet, None


def solo_expire(store, gid):
    """Refund a solo game nobody finished."""
    with lock:
        g = store.pop(gid, None)
        if g:
            change(g["uid"], cash=g["bet"])


def solo_timer(store, gid):
    t = threading.Timer(GAME_TIMEOUT, solo_expire, args=(store, gid))
    t.daemon = True
    t.start()


games = {}


SUITS = "♠♥♦♣"


def card(c):
    rank, suit = c
    return f"[{ {1: 'A', 11: 'J', 12: 'Q', 13: 'K'}.get(rank, str(rank)) }{suit}]"


def show(h):
    return " ".join(card(c) for c in h)


def hand_val(h):
    t = sum(min(c[0], 10) for c in h)
    if any(c[0] == 1 for c in h) and t + 10 <= 21:
        t += 10
    return t


def bj_view(g, result=None, drew=None):
    dealer = f"{show(g['d'])} (= {hand_val(g['d'])})" if result else f"{card(g['d'][0])} [?]"
    text = (f"🃏 *Blackjack* | {tag(g['uid'])} bet {fmt(g['bet'])}\n"
            f"Your cards: {show(g['p'])}  =  *{hand_val(g['p'])}*\nDealer: {dealer}")
    if drew:
        text += f"\nYou drew {card(drew)}"
    if result:
        text += f"\n*{result}*"
    blocks = [section(text)]
    if not result:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Hit"},
             "action_id": "bj_hit", "value": g["gid"]},
            {"type": "button", "text": {"type": "plain_text", "text": "Stand"},
             "action_id": "bj_stand", "value": g["gid"]},
        ]})
    return {"blocks": blocks, "text": text, "response_type": "ephemeral"}


def bj_finish(g, natural=False):
    p = hand_val(g["p"])
    while hand_val(g["d"]) < 17 and p <= 21:
        g["d"].append(g["deck"].pop())
    d, bet = hand_val(g["d"]), g["bet"]
    # profit multipliers (doubled): win 2x, win with 21 3x, natural blackjack 5x
    if natural and d != 21:
        res, pay = "Blackjack! 5x", bet * 6
    elif p > 21:
        res, pay = "Bust, you lose", 0
    elif (d > 21 or p > d) and p == 21:
        res, pay = "21! You win 3x", bet * 4
    elif d > 21 or p > d:
        res, pay = "You win 2x!", bet * 3
    elif p == d:
        res, pay = "Push, bet returned", bet
    else:
        res, pay = "Dealer wins", 0
    change(g["uid"], cash=pay)
    games.pop(g["gid"], None)
    net = pay - bet
    return f"{res} ({'+' if net >= 0 else '-'}{fmt(abs(net))})"


def bj_timeout(gid):
    """Walked away from a hand? It auto-stands (no free refund for abandoning a bad hand)."""
    with lock:
        g = games.get(gid)
        if g:
            bj_finish(g)


@cmd("blackjack")
def blackjack(uid, args):
    bet, err = check_bet(uid, args)
    if err:
        return err + "\nUsage: `/blackjack <bet|all>`"
    change(uid, cash=-bet)  # bet is held until the game ends
    deck = [(r, s) for r in range(1, 14) for s in SUITS]
    random.shuffle(deck)
    g = {"gid": uuid.uuid4().hex[:8], "uid": uid, "bet": bet, "deck": deck,
         "p": [deck.pop(), deck.pop()], "d": [deck.pop(), deck.pop()]}
    games[g["gid"]] = g
    t = threading.Timer(GAME_TIMEOUT, bj_timeout, args=(g["gid"],))
    t.daemon = True
    t.start()
    if hand_val(g["p"]) == 21:
        return bj_view(g, bj_finish(g, natural=True))
    return bj_view(g)


@app.action(re.compile(r"^bj_(hit|stand)$"))
def bj_action(ack, body, respond, action):
    ack()
    with lock:
        g = games.get(action["value"])
        if not g:
            respond(text="That game has ended.", replace_original=False, response_type="ephemeral")
            return
        if body["user"]["id"] != g["uid"]:
            respond(text="❌ This isn't your game!", replace_original=False, response_type="ephemeral")
            return
        drew = None
        if action["action_id"] == "bj_hit":
            drew = g["deck"].pop()
            g["p"].append(drew)
            if hand_val(g["p"]) < 21:
                view = render(bj_view(g, drew=drew), False)
                respond(replace_original=True, blocks=view["blocks"], text=view["text"])
                return
        result = bj_finish(g)
        view = render(bj_view(g, result, drew), False)
        respond(replace_original=True, blocks=view["blocks"], text=view["text"])


RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


@cmd("roulette")
def roulette(uid, args):
    usage = "\nUsage: `/roulette <bet|all> <red|black|odd|even|0-36>`"
    bet, err = check_bet(uid, args)
    if err:
        return err + usage
    if len(args) < 2:
        return "❌ Pick a space." + usage
    space = args[1]
    if space not in ("red", "black", "odd", "even"):
        if not space.isdigit() or not 0 <= int(space) <= 36:
            return "❌ Invalid space." + usage
    n = random.randint(0, 36)
    color = "green" if n == 0 else ("red" if n in RED else "black")
    if space == "red":
        win = color == "red"
    elif space == "black":
        win = color == "black"
    elif space == "odd":
        win = n != 0 and n % 2 == 1
    elif space == "even":
        win = n != 0 and n % 2 == 0
    else:
        win = int(space) == n
    mult = 70 if space.isdigit() else 2  # doubled payouts
    head = f"🎡 The ball landed on *{color} {n}*!"
    if win:
        change(uid, cash=bet * mult)
        return f"{head}\n🎉 {tag(uid)} bet on `{space}` and won {fmt(bet * mult)}!"
    change(uid, cash=-bet)
    return f"{head}\n{tag(uid)} bet on `{space}` and lost {fmt(bet)}."


# ---------- rock paper scissors vs the bot (private) ----------
rps_games = {}
RPS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


@cmd("rps")
def rps(uid, args):
    if args and args[0].startswith("<"):  # /rps @user [bet] -> play another person
        return make_challenge(uid, args, "rps")
    bet, err = check_bet(uid, args)
    if err:
        return err + "\nUsage: `/rps <bet|all>` (vs the bot) or `/rps @user [bet]` (vs a person)"
    change(uid, cash=-bet)  # held until the game ends
    gid = uuid.uuid4().hex[:8]
    rps_games[gid] = {"gid": gid, "uid": uid, "bet": bet}
    solo_timer(rps_games, gid)
    text = f"✊ *Rock Paper Scissors* | {tag(uid)} bet {fmt(bet)}\nPick your move!"
    buttons = [{"type": "button", "text": {"type": "plain_text", "text": f"{e} {m.title()}"},
                "action_id": f"rps_{m}", "value": gid} for m, e in RPS.items()]
    return {"blocks": [section(text), {"type": "actions", "elements": buttons}],
            "text": text, "response_type": "ephemeral"}


# NOTE: patterns are anchored (^...$). Without that, "prps_rock" (the 2-player buttons)
# also matched "rps_rock" and was swallowed by this handler -> "That game has ended."
@app.action(re.compile(r"^rps_(rock|paper|scissors)$"))
def rps_action(ack, body, respond, action):
    ack()
    with lock:
        g = rps_games.get(action["value"])
        if not g:
            respond(text="That game has ended.", replace_original=False, response_type="ephemeral")
            return
        if body["user"]["id"] != g["uid"]:
            respond(text="❌ This isn't your game!", replace_original=False, response_type="ephemeral")
            return
        me = action["action_id"][4:]
        bot_move = random.choice(list(RPS))
        bet = g["bet"]
        if me == bot_move:
            pay, res = bet, "Tie, bet returned"
        elif BEATS[me] == bot_move:
            pay, res = bet * 3, "You win double!"
        else:
            pay, res = 0, "You lose"
        change(g["uid"], cash=pay)
        rps_games.pop(action["value"], None)
        net = pay - bet
        text = (f"✊ *Rock Paper Scissors* | {tag(g['uid'])} bet {fmt(bet)}\n"
                f"You: {RPS[me]} {me}  vs  Bot: {RPS[bot_move]} {bot_move}\n"
                f"*{res}* ({'+' if net >= 0 else '-'}{fmt(abs(net))})")
        v = render({"text": text, "blocks": [section(text)]}, True)
        respond(replace_original=True, **v)


# ---------- 2-player games: challenges, rock paper scissors, tic tac toe (public) ----------
challenges = {}  # waiting for the other person to accept
pvp = {}         # running 2-player games
GAME_NAMES = {"rps": "Rock Paper Scissors", "ttt": "Tic Tac Toe"}


def stake_text(bet):
    return f"{fmt(bet)} each" if bet else "no bet"


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
    gid = uuid.uuid4().hex[:8]
    challenges[gid] = {"kind": kind, "a": uid, "b": target, "bet": bet}
    t = threading.Timer(GAME_TIMEOUT, forget_challenge, args=(gid,))
    t.daemon = True
    t.start()
    # the challenged person is the only one who gets a real @ping
    text = f"<@{target}> {tag(uid)} challenges you to *{GAME_NAMES[kind]}* ({stake_text(bet)})!"
    return {"text": text, "response_type": "in_channel", "blocks": [
        section(text),
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Accept"},
             "action_id": "ch_accept", "value": gid},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Decline"},
             "action_id": "ch_decline", "value": gid},
        ]}]}


def only_you(respond, text):
    respond(text=text, replace_original=False, response_type="ephemeral")


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
        else:
            g["board"] = [None] * 9
            g["turn"] = g["a"]
        pvp[gid] = g
        arm(g)
        view = rps_view(g) if g["kind"] == "rps" else ttt_view(g)
        respond(replace_original=True, **render(view, False))


# --- 2-player rock paper scissors ---
def rps_view(g):
    picks = g["picks"]
    status = " | ".join(f"{tag(u)} {'✅ picked' if u in picks else '⏳ thinking'}" for u in (g["a"], g["b"]))
    text = (f"✊ *Rock Paper Scissors* | {tag(g['a'])} vs {tag(g['b'])} ({stake_text(g['bet'])})\n{status}")
    buttons = [{"type": "button", "text": {"type": "plain_text", "text": f"{e} {m.title()}"},
                "action_id": f"prps_{m}", "value": g["gid"]} for m, e in RPS.items()]
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
        head = (f"✊ *Rock Paper Scissors* | {tag(a)} {RPS[pa]} {pa}  vs  {tag(b)} {RPS[pb]} {pb}\n")
        if pa == pb:
            change(a, cash=bet)
            change(b, cash=bet)
            res = "Tie, bets returned."
        else:
            winner = a if BEATS[pa] == pb else b
            change(winner, cash=bet * 3)  # own bet back + opponent's bet + a bonus bet from the house
            res = f"🏆 {tag(winner)} wins" + (f" {fmt(bet * 2)} ({tag(b if winner == a else a)} loses {fmt(bet)})!" if bet else "!")
        replace(respond, head + res)


# --- tic tac toe ---
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
CELL = {None: "⬜", "X": "❌", "O": "⭕"}


def ttt_head(g):
    return f"*Tic Tac Toe* | ❌ {tag(g['a'])} vs ⭕ {tag(g['b'])} ({stake_text(g['bet'])})"


NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def board_text(board, numbers=False):
    def c(i):
        return NUMS[i] if numbers and not board[i] else CELL[board[i]]
    return "\n".join("".join(c(r * 3 + k) for k in range(3)) for r in range(3))


def ttt_view(g):
    # Buttons stack vertically on Slack mobile, so the board is drawn as emoji text
    # (looks the same everywhere) and you pick a square from a dropdown.
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
                change(user, cash=bet * 3)  # same doubled payout as 2-player RPS
                other = g["b"] if user == g["a"] else g["a"]
                res = f"🏆 {tag(user)} wins" + (f" {fmt(bet * 2)} ({tag(other)} loses {fmt(bet)})!" if bet else "!")
            else:
                change(g["a"], cash=bet)
                change(g["b"], cash=bet)
                res = "Draw, bets returned."
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


# ---------- admin ----------
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


# ---------- help (also shown when someone types @Hakari) ----------
HELP_TEXT = ("*Hakari commands*\n"
             "`/balance [@user]` `/deposit <amt|all>` `/withdraw <amt|all>` `/give @user <amt|all>`\n"
             "`/leaderboard` `/stats` `/work` `/crime` `/collect`\n"
             "`/rob @user` 50% to steal 40-90% of their cash, fail and you pay them 15-30% of yours "
             "(keep money in the bank to be safe!)\n"
             "`/blackjack <bet|all>` win 2x, 21 = 3x, blackjack = 5x\n"
             "`/roulette <bet|all> <red|black|odd|even|0-36>` color/parity 2x, number 70x\n"
             "`/rps <bet|all>` (vs bot) win 2x, or `/rps @user [bet]` (vs a person) winner nets 2x the bet\n"
             "`/ttt @user [bet]` tic tac toe vs a person\n"
             "`/catalog [item]` `/buy <item> [amt]` `/inventory` `/use <item> [amt]`\n"
             f"💬 Chatting earns {COIN} {CHAT_MIN}-{CHAT_MAX} every {CHAT_COOLDOWN}s.\n"
             "Solo games and money commands are private, only you see them. "
             "Rob/give/challenges are public and ping the other person.\n"
             "Admins: `/add-money` `/create-item` `/audit @user`")

MENTION_TEXT = "🎰 *Check out my gambling bot!* You can earn prizes too 🏆 Be a robber, do crime.\n"


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


@cmd("ecohelp")
def ecohelp(uid, args):
    return HELP_TEXT


@app.event("app_mention")
def on_mention(event, say):
    print("Received app_mention:", event.get("user"), event.get("channel"), event.get("text"))
    msg = MENTION_TEXT + HELP_TEXT
    kwargs = {"text": msg}
    thread = event.get("thread_ts") or event.get("ts")
    if thread:
        kwargs["thread_ts"] = thread
    try:
        say(**kwargs)
    except Exception as e:
        print("app_mention reply failed:", repr(e))
        try:
            say(text=msg)
        except Exception as e2:
            print("app_mention fallback reply failed:", repr(e2))


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
