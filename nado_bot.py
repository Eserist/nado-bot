"""
Nado.xyz Trading Bot
=====================
- State wird gespeichert (überlebt Neustarts)
- Trailing Stop + TP/SL
- Kein sofortiger Umkehr-Trade nach Close
- Limit Orders (0.1% Slippage)
- RSI + EMA + MACD auf 5-Min Kerzen

Einrichten:
    1. WALLET_ADDR = deine Wallet Adresse
    2. SIGNER_KEY  = 1-Click Trading Key (app.nado.xyz → Settings)
"""

import time, random, requests, sys, json, os, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    G=Fore.GREEN; R=Fore.RED; Y=Fore.YELLOW; C=Fore.CYAN; M=Fore.MAGENTA
    X=Style.RESET_ALL; B=Style.BRIGHT
except:
    G=R=Y=C=M=X=B=""

# ═══════════════════════════════════════════════════════════
#  EINSTELLUNGEN
# ═══════════════════════════════════════════════════════════
WALLET_ADDR = "0xc15263578ce7fd6290f56Ab78a23D3b6C653B28C"
SIGNER_KEY  = "0x8097b0ec439aa91bd4f3c3ea79735be6688ce00589bbcd0e3dea2ab596580a4d"

PRODUCT_ID  = 2
CHAIN_ID    = 57073
GATEWAY     = "https://gateway.prod.nado.xyz/v1"
ARCHIVE     = "https://archive.prod.nado.xyz/v1"
HEADERS     = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE  = 0.0017
TAKE_PROFIT = 1.5
STOP_LOSS   = 0.8
TRAIL_PCT   = 0.8
COOLDOWN    = 3

RSI_LOW     = 35
RSI_HIGH    = 65
MIN_SIG     = 4
MIN_CANDLES = 50
INTERVAL    = 30
DRY_RUN     = False
STATE_FILE  = "state.json"
# ═══════════════════════════════════════════════════════════

pos    = None
cool   = 0
trades = wins = loss = 0


def ts():    return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try: return f"${float(x):,.2f}"
    except: return "?"


# ─── STATE SPEICHERN / LADEN ──────────────────────────────

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"pos": pos, "trades": trades, "wins": wins, "loss": loss, "cool": cool}, f)
    except: pass

def load_state():
    global pos, trades, wins, loss, cool
    try:
        if os.path.exists(STATE_FILE):
            d = json.load(open(STATE_FILE))
            pos    = d.get("pos")
            trades = d.get("trades", 0)
            wins   = d.get("wins", 0)
            loss   = d.get("loss", 0)
            cool   = d.get("cool", 0)
            if pos:
                log(f"State geladen: {pos['dir']} @ {fmt(pos['entry'])}", Y)
            else:
                log("State geladen: keine offene Position", C)
    except Exception as e:
        log(f"State laden Fehler: {e}", Y)


# ─── API ──────────────────────────────────────────────────

def get_candles():
    try:
        r = requests.post(
            ARCHIVE,
            json={"candlesticks": {"product_id": PRODUCT_ID, "granularity": 300, "limit": 100}},
            headers=HEADERS, timeout=15, verify=False
        )
        if r.status_code != 200: return None
        cs = r.json().get("candlesticks", [])
        return [{"o": float(c.get("open_x18",0))/1e18, "h": float(c.get("high_x18",0))/1e18,
                 "l": float(c.get("low_x18",0))/1e18,  "c": float(c.get("close_x18",0))/1e18}
                for c in cs] if cs else None
    except Exception as e:
        log(f"Kerzen Fehler: {e}", Y); return None

def get_preis():
    try:
        r = requests.get(f"{GATEWAY}/query?type=all_products",
                        headers={"Accept-Encoding": "gzip"}, timeout=15, verify=False)
        if r.status_code != 200: return None
        body = r.json()
        data = body.get("data", body)
        for p in data.get("perp_products", []):
            if int(p.get("product_id", -1)) == PRODUCT_ID:
                px = float(p.get("oracle_price_x18") or p.get("mark_price_x18") or 0)
                if px > 0: return px / 1e18
    except Exception as e:
        log(f"Preis Fehler: {e}", Y)
    return None


# ─── INDIKATOREN ──────────────────────────────────────────

def calc_rsi(c, n=14):
    if len(c) < n+1: return None
    g, v = [], []
    for i in range(1, len(c)):
        d = c[i]-c[i-1]; g.append(max(d,0)); v.append(max(-d,0))
    ag=sum(g[:n])/n; av=sum(v[:n])/n
    for i in range(n, len(g)): ag=(ag*(n-1)+g[i])/n; av=(av*(n-1)+v[i])/n
    return 100 if av==0 else 100-(100/(1+ag/av))

def calc_ema(c, n):
    if len(c) < n: return None
    k=2/(n+1); e=sum(c[:n])/n
    for x in c[n:]: e=x*k+e*(1-k)
    return e

def calc_macd(c):
    if len(c) < 26: return None, None
    vs = []
    for i in range(26, len(c)+1):
        e12=calc_ema(c[:i],12); e26=calc_ema(c[:i],26)
        if e12 and e26: vs.append(e12-e26)
    if not vs: return None, None
    return vs[-1], calc_ema(vs,9) if len(vs)>=9 else None

def calc_atr(cs, n=14):
    if len(cs) < n: return None
    tl = []
    for i in range(len(cs)):
        t = cs[i]["h"]-cs[i]["l"] if i==0 else max(cs[i]["h"]-cs[i]["l"],
            abs(cs[i]["h"]-cs[i-1]["c"]), abs(cs[i]["l"]-cs[i-1]["c"]))
        tl.append(t)
    return sum(tl[-n:])/n

def signal(cs):
    if len(cs) < MIN_CANDLES: return None, {}
    cl = [c["c"] for c in cs]
    r  = calc_rsi(cl); e9 = calc_ema(cl,9); e21 = calc_ema(cl,21)
    e50 = calc_ema(cl, min(50,len(cl))); mc, ms = calc_macd(cl); a = calc_atr(cs)
    if not r or not e9 or not e21 or not a or a < 0.001: return None, {}
    cur = cl[-1]; prv = cl[-2] if len(cl)>1 else cur

    ls = (2 if r<RSI_LOW else 0)+(2 if e9>e21 else 0)+(1 if cur>e21 else 0)+(1 if mc and ms and mc>ms else 0)+(1 if cur>prv else 0)
    ss = (2 if r>RSI_HIGH else 0)+(2 if e9<e21 else 0)+(1 if cur<e21 else 0)+(1 if mc and ms and mc<ms else 0)+(1 if cur<prv else 0)

    info = {"rsi": round(r,1), "ls": ls, "ss": ss}
    if ls >= MIN_SIG and ls > ss: return "LONG", info
    if ss >= MIN_SIG and ss > ls: return "SHORT", info
    return None, info


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x",""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()

def place_order(is_buy, price, reduce_only=False):
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {ORDER_SIZE} BTC @ {fmt(price)}", Y)
        return True
    try:
        from eth_account import Account
        px    = round(price * (1.001 if is_buy else 0.999)) * int(1e18)
        amt   = int(ORDER_SIZE*1e18) if is_buy else -int(ORDER_SIZE*1e18)
        exp   = int(time.time()) + 60
        nonce = ((int(time.time()*1000)+5000) << 20) + random.randint(0,999)
        apx   = 1 | (1<<11 if reduce_only else 0)
        sndr  = sender_hex()

        dom = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,"verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ = {"Order":[{"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
                        {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
                        {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}]}
        msg = {"sender":sndr,"priceX18":px,"amount":amt,"expiration":exp,"nonce":nonce,"appendix":apx}

        acc = Account.from_key(SIGNER_KEY)
        sig = acc.sign_typed_data(domain_data=dom, message_types=typ, message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x"+sig

        pld = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":str(apx)
        },"signature":sig}}

        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G); return True
        err  = d.get("error","")
        code = d.get("error_code","")
        # Code 2064: Position existiert nicht mehr auf Nado — State zurücksetzen
        if code == 2064:
            log(f"Position nicht auf Nado — State wird zurückgesetzt", Y)
            return "RESET"
        log(f"❌ {err} (Code:{code})", R); return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False


# ─── POSITION ─────────────────────────────────────────────

def open_pos(richtung, preis):
    global pos, trades, cool
    is_buy = richtung == "LONG"
    ok = place_order(is_buy, preis)
    if not ok and not DRY_RUN: return
    tp = preis*(1+TAKE_PROFIT/100) if is_buy else preis*(1-TAKE_PROFIT/100)
    sl = preis*(1-STOP_LOSS/100)   if is_buy else preis*(1+STOP_LOSS/100)
    pos = {"dir":richtung,"entry":preis,"tp":tp,"sl":sl,
           "best":preis if is_buy else 0,
           "worst":preis if not is_buy else float('inf'),
           "id":trades}
    trades += 1; cool = 0
    save_state()
    print(f"\n{B}{'═'*55}")
    print(f"  {'🟢' if is_buy else '🔴'} {G if is_buy else R}POSITION #{pos['id']} GEÖFFNET — {richtung}{X}")
    print(f"  Entry:{fmt(preis)}  TP:{fmt(tp)}  SL:{fmt(sl)}  Trail:{TRAIL_PCT}%")
    print(f"{'═'*55}{X}\n")

def close_pos(grund, preis):
    global pos, wins, loss, cool
    if not pos: return
    is_buy = pos["dir"] != "LONG"
    ok = place_order(is_buy, preis, reduce_only=False)

    # Position auf Nado existiert nicht mehr — State zurücksetzen
    if ok == "RESET" or (not ok and not DRY_RUN):
        log("State wird zurückgesetzt!", Y)
        pos = None; cool = COOLDOWN; save_state(); return

    pnl = (preis-pos["entry"])/pos["entry"]*100 if pos["dir"]=="LONG" else (pos["entry"]-preis)/pos["entry"]*100
    if pnl > 0: wins += 1
    else: loss += 1
    wr = wins/(wins+loss)*100 if (wins+loss)>0 else 0
    fc = G if pnl>0 else R; emoji = "✅" if pnl>0 else "❌"
    print(f"\n{B}{'═'*55}")
    print(f"  {emoji} POSITION #{pos['id']} GESCHLOSSEN — {grund}")
    print(f"  Entry:{fmt(pos['entry'])}  Exit:{fmt(preis)}  P&L:{fc}{pnl:+.2f}%{X}")
    print(f"  {trades} Trades | {wins}W {loss}L | {wr:.0f}% Win Rate")
    print(f"{'═'*55}{X}\n")
    pos = None; cool = COOLDOWN
    save_state()


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global cool
    tick = 0
    log(f"Bot | BTC | TP:{TAKE_PROFIT}% SL:{STOP_LOSS}% Trail:{TRAIL_PCT}% | {'DRY RUN' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1
            cs = get_candles()
            if not cs: log("Keine Kerzen", Y); time.sleep(INTERVAL); continue

            preis = get_preis()
            if not preis: log("Kein Preis", Y); time.sleep(INTERVAL); continue

            if pos:
                is_long = pos["dir"] == "LONG"
                pnl = (preis-pos["entry"])/pos["entry"]*100 if is_long else (pos["entry"]-preis)/pos["entry"]*100
                fc  = G if pnl>0 else R

                if is_long:
                    pos["best"]  = max(pos["best"], preis)
                    trail        = pos["best"] * (1 - TRAIL_PCT/100)
                else:
                    pos["worst"] = min(pos["worst"], preis)
                    trail        = pos["worst"] * (1 + TRAIL_PCT/100)

                log(f"#{pos['id']} {pos['dir']} | {fmt(pos['entry'])} → {fmt(preis)} | P&L:{fc}{pnl:+.2f}%{X} | Trail:{fmt(trail)}")
                save_state()

                if (is_long and preis >= pos["tp"]) or (not is_long and preis <= pos["tp"]):
                    close_pos("TAKE PROFIT ✅", preis)
                elif (is_long and preis <= pos["sl"]) or (not is_long and preis >= pos["sl"]):
                    close_pos("STOP LOSS ❌", preis)
                elif (is_long and preis <= trail) or (not is_long and preis >= trail):
                    close_pos("TRAILING STOP 📉", preis)

            else:
                if cool > 0:
                    cool -= 1
                    log(f"Cooldown: {cool} | BTC {fmt(preis)}", Y)
                    time.sleep(INTERVAL); continue

                sig, info = signal(cs)
                ls = info.get("ls",0); ss = info.get("ss",0)

                if sig:
                    log(f"🎯 SIGNAL: {sig} (L:{ls}/7 S:{ss}/7 RSI:{info.get('rsi','?')})", M)
                    open_pos(sig, preis)
                else:
                    if tick % 2 == 0:
                        log(f"BTC {fmt(preis)}  RSI:{info.get('rsi','?')}  L:{ls}/7 S:{ss}/7  → warten")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if pos: log(f"⚠️ OFFENE POSITION: {pos['dir']} @ {fmt(pos['entry'])} — MANUELL SCHLIESSEN!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║      Nado.xyz — Smart Trading Bot        ║")
    print(f"  ║   Trailing Stop + TP/SL + State Save     ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet: {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  TP:{TAKE_PROFIT}%  SL:{STOP_LOSS}%  Trail:{TRAIL_PCT}%")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus: {modus}\n")
    load_state()
    loop()


if __name__ == "__main__":
    main()
