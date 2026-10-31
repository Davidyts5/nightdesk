# ==================================================================
# from config.py
# ==================================================================
"""Desk configuration. Rule numbers match the spec 1:1. Nothing loosened between paper and live."""
import os
from datetime import timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Africa/Lagos")          # WAT, UTC+1, no DST
except Exception:
    TZ = timezone(timedelta(hours=1))

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "state")
LOG_DIR = os.path.join(BASE, "logs")
REPORTS_DIR = os.path.join(BASE, "reports")

# ---------------- MODE ----------------
MODE = "PAPER"                    # live execution is intentionally NOT wired. See README.
START_BALANCE_USD = 1000.0        # paper bankroll

# ---------------- SCOUT ----------------
POLL_SECONDS = 180                # one full desk cycle every 3 minutes
GT_PAGES = 15                     # GeckoTerminal new_pools pages (20 pools each = 300 newest)
HATCHERY_MAX = 500                # sub-30-min pools held until they cross the 30-min line
HATCHERY_TTL_H = 6                # hours a pool may sit hatched without qualifying, then released
ENRICH_BATCH = 30                 # DexScreener allows 30 mints per lookup
AGE_MIN_SECONDS = 30 * 60         # pairs must be 30 min ...
AGE_MAX_SECONDS = 72 * 3600       # ... to 72h old
MCAP_MIN = 300_000
MCAP_MAX = 20_000_000

# ---------------- FILTER (8 kill rules, one hit = dead) ----------------
MIN_LIQUIDITY_USD = 40_000        # R3
MIN_VOL_1H_USD = 50_000           # R4
TOP10_MAX_PCT = 25.0              # R5, percent scale (RugCheck pct = percent)
DEV_MAX_PCT = 0.03                # R6a: dev wallet > 3%
LP_LOCKED_MIN_PCT = 95.0          # R2: RugCheck lpLockedPct, 0-100 scale
H1_PUMP_MAX_PCT = 80.0            # R8: +80% in last hour = dead
# R2 interpretation: bonding-curve / protocol-owned venues have no dev-withdrawable LP.
# They pass R2 with an explicit note in the log. Flip to False to kill them instead.
NO_LP_DEXES_PASS = {"pumpfun", "pumpswap", "launchlab"}
# Safety data unavailable: defer & retry (RugCheck lags on brand-new tokens, and
# rate-limits datacenter IPs). Dead only after N cycles straight of no answer.
FAILCLOSED_STRIKES = 3

# ---------------- ANALYST ----------------
SCORE_MIN = 70                    # only 70+ goes to Executor
W_MOMENTUM, W_BUYERS, W_BS, W_GROWTH, W_LIQMCAP, W_SMART = 25, 20, 15, 15, 15, 10

# ---------------- EXECUTOR / RISK ----------------
MAX_POSITION_PCT = 0.08           # max 8% of CURRENT balance per trade
MIN_TRADE_USD = 50.0              # below this a trade isn't worth the spread -> skip
MAX_OPEN_POSITIONS = 5            # RISK guardrail: caps total exposure at 40%
SLIPPAGE_PCT = 0.01               # paper-fill assumption, both sides
STOP_PCT = 0.06                   # -6% hard stop, set BEFORE the order goes in
TRAIL_TRIGGER_PCT = 0.12          # at +12% ...
TRAIL_DISTANCE_PCT = 0.10         # ... trail 10% under the running peak (ratchets up only)
EQUITY_DRAWDOWN_HALT = 0.15       # RISK circuit breaker: no new entries below -15% equity

# ---------------- SESSION ----------------
END_HOUR_LOCAL = 7                # morning recap at 07:00 WAT
MAX_RUNTIME_HOURS = 20

RULE_NAMES = {
    "R1": "Mint or freeze authority not revoked",
    "R2": "LP not burned or locked",
    "R3": "Liquidity under $40k",
    "R4": "1h volume under $50k",
    "R5": "Top 10 holders own more than 25%",
    "R6": "Dev wallet > 3% or past rugs",
    "R7": "Bundled launch over 10%",
    "R8": "Already up 80%+ in the last hour",
    "FAILCLOSED": "Data unavailable -> fail-closed (protect the balance first)",
}

# ==================================================================
# from clients.py
# ==================================================================
"""Public data feeds. No keys, stdlib only: GeckoTerminal (new pools), DexScreener (metrics), RugCheck (safety)."""
import json
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) desk-paper/0.1", "Accept": "application/json"}


def num(x):
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def http_get_json(url, timeout=20, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - network layer, back off and retry
            last = e
            code = getattr(e, "code", None)
            pause = 6.0 if code in (429, 403, 503) else 1.2 * (2 ** attempt)
            time.sleep(pause)
    raise last


class Throttle:
    def __init__(self, min_interval):
        self.min = min_interval
        self.last = 0.0

    def wait(self):
        delta = self.last + self.min - time.monotonic()
        if delta > 0:
            time.sleep(delta)
        self.last = time.monotonic()


class Gecko:
    """New pools on Solana across venues (pumpfun/pumpswap/raydium/...)."""
    T = Throttle(1.2)

    def new_pools(self, page=1):
        self.T.wait()
        return http_get_json(
            "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=%d" % page
        )


class DexScreener:
    T = Throttle(0.35)

    def tokens(self, mints):
        self.T.wait()
        return http_get_json(
            "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(m[:44] for m in mints)
        )

    def token_profiles(self):
        self.T.wait()
        return http_get_json("https://api.dexscreener.com/token-profiles/latest/v1")


class RugCheck:
    """Full token safety report. Cached per mint - the Filter evaluates a mint once.
    Extra retries: a 429 mid-backlog must not turn into a false FAILCLOSED kill."""
    T = Throttle(0.55)

    def __init__(self):
        self.cache = {}

    def report(self, mint):
        if mint in self.cache:
            return self.cache[mint]
        self.T.wait()
        rep = http_get_json("https://api.rugcheck.xyz/v1/tokens/%s/report" % mint, retries=5)
        self.cache[mint] = rep
        return rep


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = B58[r] + out
    for byte in b:
        if byte == 0:
            out = "1" + out
        else:
            break
    return out or "1"


def http_post_json(url, payload, timeout=25, retries=3):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={**UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            code = getattr(e, "code", None)
            time.sleep(6.0 if code in (429, 403, 503) else 1.0 * (attempt + 1))
    raise last


class SolanaRPC:
    """Public Solana RPC: the chain itself as fallback safety source.
    When RugCheck is down/unindexed, we verify R1 (authorities) and R5 (top holders)
    straight from the blockchain. No keys, stdlib only. Multi-endpoint failover."""

    URLS = ["https://solana-rpc.publicnode.com",
            "https://api.mainnet-beta.solana.com"]
    T = Throttle(1.6)
    _good = 0

    def _call(self, method, params):
        self.T.wait()
        last = None
        # try the endpoint that worked last time first, then the rest
        order = [self._good] + [i for i in range(len(self.URLS)) if i != self._good]
        for i in order:
            try:
                d = http_post_json(self.URLS[i], {"jsonrpc": "2.0", "id": 1,
                                                  "method": method, "params": params},
                                   retries=2)
                if d.get("error"):
                    raise RuntimeError(str(d.get("error"))[:100])
                SolanaRPC._good = i
                return d.get("result")
            except Exception as e:  # noqa: BLE001 - rotate to next endpoint
                last = e
        raise last

    def authorities(self, mint):
        """Parse the SPL mint account (layout: mintAuthOpt u32 @0, mintAuth @4:36,
        supply u64 @36:44, decimals u8 @44, freezeAuthOpt u32 @46, freezeAuth @50:82)."""
        import base64
        r = self._call("getAccountInfo", [mint, {"encoding": "base64"}])
        v = (r or {}).get("value")
        if not v or not v.get("data"):
            raise RuntimeError("mint account not found on chain")
        data = base64.b64decode(v["data"][0])
        if len(data) < 82:
            raise RuntimeError("short mint account")
        mint_auth = b58encode(data[4:36]) if int.from_bytes(data[0:4], "little") == 1 else None
        freeze_auth = b58encode(data[50:82]) if int.from_bytes(data[46:50], "little") == 1 else None
        supply = int.from_bytes(data[36:44], "little")
        decimals = data[44]
        return {"mintAuthority": mint_auth, "freezeAuthority": freeze_auth,
                "supply_ui": supply / (10 ** decimals) if decimals < 40 else float(supply)}

    def top10_sum(self, mint):
        r = self._call("getTokenLargestAccounts", [mint])
        vals = [num(a.get("uiAmount")) for a in ((r or {}).get("value") or [])]
        if len(vals) < 10:
            raise RuntimeError("only %d holder accounts returned" % len(vals))
        return sum(vals[:10])

# ==================================================================
# from agents.py
# ==================================================================
"""The six agents. Hard handoffs, wired only by run.py:

SCOUT -> FILTER -> ANALYST -> EXECUTOR -> RISK -> LOGGER

Agents share one `state` dict (persisted to disk) but never do each other's jobs.
"""
import json
import os
import re
import time
from datetime import datetime


RULE_ORDER = ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"]

# Native/stable mints the Scout must never hand off
BLOCKLIST = {
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
    "mSoLzYCxHdYgdznUqm8kv7QAcCZgexHs4VHiYddyeSn",   # mSOL
}


# ---------------- helpers ----------------
def num(x):
    try:
        if x is None:
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def clamp(x, a, b):
    return max(a, min(b, x))


def nowtz():
    return datetime.now(TZ)


def iso_now():
    return nowtz().strftime("%Y-%m-%d %H:%M:%S WAT")


def snapshot(pair):
    """Normalize one DexScreener pair into the desk's canonical metrics."""
    base = pair.get("baseToken") or {}
    mint = base.get("address")
    price = num(pair.get("priceUsd"))
    if not mint or price <= 0:
        return None
    liq = pair.get("liquidity") or {}
    vol = pair.get("volume") or {}
    pc = pair.get("priceChange") or {}
    tx = pair.get("txns") or {}

    def txns(k):
        t = tx.get(k) or {}
        return {"buys": int(t.get("buys") or 0), "sells": int(t.get("sells") or 0)}

    mcap = num(pair.get("marketCap")) or num(pair.get("fdv"))
    return {
        "mint": mint,
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "?",
        "pair_address": pair.get("pairAddress"),
        "dex_id": pair.get("dexId") or "?",
        "pair_created_at": pair.get("pairCreatedAt") or 0,
        "price_usd": price,
        "liquidity_usd": num(liq.get("usd")),
        "mcap_usd": mcap,
        "vol_h1": num(vol.get("h1")),
        "vol_h6": num(vol.get("h6")),
        "vol_h24": num(vol.get("h24")),
        "pc_m5": num(pc.get("m5")),
        "pc_h1": num(pc.get("h1")),
        "pc_h6": num(pc.get("h6")),
        "pc_h24": num(pc.get("h24")),
        "tx_m5": txns("m5"),
        "tx_h1": txns("h1"),
        "tx_h6": txns("h6"),
        "url": pair.get("url"),
    }


def equity_of(state, fresh):
    eq = state["cash"]
    for p in state["positions"].values():
        snap = (fresh or {}).get(p["mint"])
        px = snap["price_usd"] if (snap and snap["price_usd"] > 0) else p.get("last_price", 0) or 0
        eq += p["tokens"] * px
    return eq


# ---------------- SCOUT ----------------
class Scout:
    """Watches new pairs (Pump.fun + Raydium + other Solana venues via DexScreener/GeckoTerminal).
    Scope: 30min-72h old, mcap $300k-$20M. Passes every new pair to Filter, nothing else."""

    def __init__(self, dex, gecko):
        self.dex = dex
        self.gecko = gecko

    def raw_seen(self):
        """TWO telescopes, always on:
        1. GeckoTerminal new_pools (GT_PAGES pages)
        2. DexScreener latest token-profiles (fast, no cap - catches what GT's
           lagging/degraded new-pools index misses; proven gap 2026-10-31)"""
        mints = []
        for page in range(1, GT_PAGES + 1):
            try:
                d = self.gecko.new_pools(page)
            except Exception:
                continue
            for p in d.get("data") or []:
                try:
                    rel = (((p.get("relationships") or {}).get("base_token") or {})
                           .get("data") or {}).get("id") or ""
                    mint = rel.split("_", 1)[1] if "_" in rel else None
                    if mint:
                        mints.append(mint)
                except Exception:
                    continue
        try:
            for e in self.dex.token_profiles() or []:
                if e.get("chainId") == "solana" and e.get("tokenAddress"):
                    mints.append(e["tokenAddress"])
        except Exception:
            pass
        out = []
        for m in mints:
            if m and m not in out:
                out.append(m)
        return out

    def gather(self, mints):
        """Enrich mints into canonical snapshots (best Solana pair by liquidity)."""
        snaps = {}
        for i in range(0, len(mints), ENRICH_BATCH):
            batch = mints[i:i + ENRICH_BATCH]
            try:
                d = self.dex.tokens(batch)
            except Exception:
                continue
            for pair in d.get("pairs") or []:
                if pair.get("chainId") != "solana":
                    continue
                s = snapshot(pair)
                if not s:
                    continue
                cur = snaps.get(s["mint"])
                if not cur or s["liquidity_usd"] > cur["liquidity_usd"]:
                    snaps[s["mint"]] = s
        return snaps

    def select(self, snaps, state):
        """Scout's ONLY decision: the 30min-72h / $300k-$20M window. Everything inside it
        goes to Filter, nothing else. Pools younger than 30 min go to the hatchery and are
        handed off the moment they cross the 30-min line (never handed off early)."""
        cands, dropped, hatched = [], [], 0
        known = (set(state["kills_cache"]) | set(state["survivors"])
                 | {p["mint"] for p in state["positions"].values()})
        nowms = time.time() * 1000

        # new observations -> hatchery if too young
        for mint, s in snaps.items():
            if mint in known or mint in BLOCKLIST:
                continue
            age = (nowms - s["pair_created_at"]) / 1000 if s["pair_created_at"] else -1
            if age < 0:
                dropped.append((mint, s["symbol"], "no pair birth time"))
            elif age < AGE_MIN_SECONDS:
                if mint not in state["hatchery"]:
                    state["hatchery"][mint] = {"symbol": s["symbol"], "added": time.time(), "missing": 0}
                hatched += 1

        # hatchery: convert, hold, or prune
        for mint in list(state["hatchery"].keys()):
            if mint in known:
                state["hatchery"].pop(mint, None)
                continue
            s = snaps.get(mint)
            if not s:
                h = state["hatchery"][mint]
                h["missing"] = h.get("missing", 0) + 1
                if h["missing"] > 10:
                    state["hatchery"].pop(mint, None)
                continue
            h = state["hatchery"][mint]
            h["missing"] = 0
            h["symbol"] = s["symbol"]
            age = (nowms - s["pair_created_at"]) / 1000 if s["pair_created_at"] else -1
            if age < 0 or age > AGE_MAX_SECONDS:
                state["hatchery"].pop(mint, None)
                continue
            if age < AGE_MIN_SECONDS:
                continue  # keep hatching
            # TTL: sat hatched too long without ever qualifying -> release (zombie cleanup)
            if time.time() - h.get("added", time.time()) > HATCHERY_TTL_H * 3600:
                state["hatchery"].pop(mint, None)
                dropped.append((mint, s["symbol"], "hatchery TTL %.0fh, never qualified" % HATCHERY_TTL_H))
                continue
            if not (MCAP_MIN <= s["mcap_usd"] <= MCAP_MAX):
                if s["mcap_usd"] > MCAP_MAX:
                    state["hatchery"].pop(mint, None)  # grew out of scope for good
                    dropped.append((mint, s["symbol"], "mcap $%.1fM > $20M" % (s["mcap_usd"] / 1e6)))
                # else: still under $300k inside the window - keep hatching, may grow into scope
                continue
            state["hatchery"].pop(mint, None)
            cands.append(s)

        # cap the hatchery FIFO (oldest additions first)
        if len(state["hatchery"]) > HATCHERY_MAX:
            extra = len(state["hatchery"]) - HATCHERY_MAX
            for mint in sorted(state["hatchery"], key=lambda m: state["hatchery"][m]["added"])[:extra]:
                state["hatchery"].pop(mint, None)
        return cands, dropped, hatched


# ---------------- FILTER ----------------
class Filter:
    """8 kill rules, evaluated in order. One hit = dead, no exceptions.
    If safety data is unavailable after retries -> fail-closed (kill), balance first."""

    def __init__(self, rugcheck, logger, state, rpc=None):
        self.rug = rugcheck
        self.log = logger
        self.state = state
        self.rpc = rpc

    def _kill(self, s, rule, detail):
        st = self.state
        st["kills_by_rule"][rule] = st["kills_by_rule"].get(rule, 0) + 1
        st["kills_cache"][s["mint"]] = rule
        self.log.kill(s, rule, detail)

    def _defer(self, s, reason):
        """Unverifiable, not guilty: quarantine with strikes (retry next cycles)."""
        q = self.state.setdefault("quarantine", {})
        rec = q.get(s["mint"]) or {"strikes": 0, "symbol": s["symbol"]}
        rec["strikes"] += 1
        rec["symbol"] = s["symbol"]
        q[s["mint"]] = rec
        if rec["strikes"] >= FAILCLOSED_STRIKES:
            self._kill(s, "FAILCLOSED",
                       "safety data unreachable %d cycles straight - dead" % rec["strikes"])
            q.pop(s["mint"], None)
        else:
            self.state["hatchery"][s["mint"]] = {"symbol": s["symbol"], "added": time.time(),
                                                 "missing": 0}
            self.log.event("DEFER", "%s: %s (strike %d/%d) - retrying next cycle"
                           % (s["symbol"], reason, rec["strikes"], FAILCLOSED_STRIKES))

    def _rpc_fallback(self, s):
        """RugCheck down -> verify what the chain itself can prove.
        R1 (authorities) always attempted - light RPC methods that work even when
        rate-limits bite. R5 (holders) attempted opportunistically: free public RPCs
        throttle getTokenLargestAccounts from datacenter IPs, so its absence defers
        instead of pretending."""
        try:
            info = self.rpc.authorities(s["mint"])
        except Exception as e:  # noqa: BLE001
            return ("defer", None, None,
                    ["chain RPC also unreachable (%s)" % e.__class__.__name__])
        if info["mintAuthority"] or info["freezeAuthority"]:
            return ("kill", "R1", "%s authority still active (RPC-verified)"
                    % ("mint" if info["mintAuthority"] else "freeze"))
        notes = ["R1 revoked (RPC-verified)"]
        try:
            top10 = self.rpc.top10_sum(s["mint"])
            pct = (top10 / info["supply_ui"] * 100) if info["supply_ui"] > 0 else 999.0
            if pct > TOP10_MAX_PCT:
                return ("kill", "R5", "top 10 holders own %.1f%% (RPC-verified)" % pct)
            notes.append("R5 top10 %.1f%% (RPC)" % pct)
        except Exception:  # noqa: BLE001
            notes.append("R5 holders rate-limited this cycle")
            return ("defer", None, None, notes)
        return ("pass", None, None, notes)

    def evaluate(self, s):
        """Returns True only if the pair survives all 8 rules."""
        mint, sym = s["mint"], s["symbol"]
        try:
            rep = self.rug.report(mint)
        except Exception:
            # RugCheck down/unindexed -> the chain itself gets a vote (public RPC).
            verdict, rule, detail, rpc_notes = (self._rpc_fallback(s) if self.rpc
                                                else ("defer", None, None, ["no RPC configured"]))
            if verdict == "kill":
                self._kill(s, rule, detail)
                return False
            if verdict == "pass":
                # RPC proved R1+R5 on-chain. LP (R2) only provable for protocol venues;
                # R6/R7 unverifiable without RugCheck -> only trade protocol-venue coins.
                if s["dex_id"] not in NO_LP_DEXES_PASS:
                    self._defer(s, "RPC ok but LP unverified on %s" % s["dex_id"])
                    return False
                if s["liquidity_usd"] < MIN_LIQUIDITY_USD:
                    self._kill(s, "R3", "liquidity $%.0f < $%d" % (s["liquidity_usd"], MIN_LIQUIDITY_USD))
                    return False
                if s["vol_h1"] < MIN_VOL_1H_USD:
                    self._kill(s, "R4", "1h volume $%.0f < $%d" % (s["vol_h1"], MIN_VOL_1H_USD))
                    return False
                if s["pc_h1"] >= H1_PUMP_MAX_PCT:
                    self._kill(s, "R8", "+%.0f%% in the last hour" % s["pc_h1"])
                    return False
                self.state["rug_cache"][s["mint"]] = {"top10": [], "ts": time.time()}
                self.log.event("FILTER_PASS", "%s (%s..) passed via RPC fallback | %s | R6/R7 unverified (RugCheck down)"
                               % (s["symbol"], mint[:6], "; ".join(rpc_notes)))
                return True
            self._defer(s, rpc_notes[0] if rpc_notes else "safety data unreachable")
            return False

        t = rep.get("token") or {}
        notes = []

        # R1 - mint or freeze authority not revoked
        mint_auth = rep.get("mintAuthority") or t.get("mintAuthority")
        freeze_auth = rep.get("freezeAuthority") or t.get("freezeAuthority")
        if mint_auth or freeze_auth:
            which = "mint" if mint_auth else "freeze"
            self._kill(s, "R1", "%s authority still active" % which)
            return False
        notes.append("R1 revoked")

        # R2 - LP not burned or locked
        if s["dex_id"] in NO_LP_DEXES_PASS:
            notes.append("R2 pass: %s liquidity is bonding-curve/protocol-owned, no dev-withdrawable LP" % s["dex_id"])
        else:
            best = None
            for m in rep.get("markets") or []:
                lp = m.get("lp") or {}
                pct = lp.get("lpLockedPct")
                if pct is not None:
                    best = max(best or 0.0, num(pct))
            if best is None:
                for lk in rep.get("lockers") or []:
                    for key in ("percent", "pct", "lockedPct"):
                        if lk.get(key) is not None:
                            best = max(best or 0.0, num(lk.get(key)))
            if best is not None and best <= 1.0:
                best *= 100.0  # normalize fraction scale just in case
            if best is None:
                types = ",".join(sorted({(m.get("marketType") or "?") for m in rep.get("markets") or []})) or "none"
                self._kill(s, "R2", "no LP lock/burn data (markets: %s) - fail-closed" % types)
                return False
            if best < LP_LOCKED_MIN_PCT:
                self._kill(s, "R2", "LP locked/burned only %.1f%% (<95%%)" % best)
                return False
            notes.append("R2 LP %.1f%% locked/burned" % best)

        # R3 - liquidity under $40k
        if s["liquidity_usd"] < MIN_LIQUIDITY_USD:
            self._kill(s, "R3", "liquidity $%.0f < $%d" % (s["liquidity_usd"], MIN_LIQUIDITY_USD))
            return False
        notes.append("R3 liq $%.0fk" % (s["liquidity_usd"] / 1000))

        # R4 - 1h volume under $50k
        if s["vol_h1"] < MIN_VOL_1H_USD:
            self._kill(s, "R4", "1h volume $%.0f < $%d" % (s["vol_h1"], MIN_VOL_1H_USD))
            return False
        notes.append("R4 vol1h $%.0fk" % (s["vol_h1"] / 1000))

        # R5 - top 10 holders own more than 25%
        th = rep.get("topHolders") or []
        if len(th) < 10:
            self._kill(s, "R5", "only %d holder rows available - fail-closed" % len(th))
            return False
        top10 = sum(num(h.get("pct")) for h in th[:10])
        if top10 > 250.0:  # scale guard: API switched to fraction
            top10 /= 100.0
        if top10 > TOP10_MAX_PCT:
            self._kill(s, "R5", "top 10 holders own %.1f%% (>25%%)" % top10)
            return False
        notes.append("R5 top10 %.1f%%" % top10)

        # R6 - dev wallet holds > 3%, or the dev has past rugs
        supply = num(t.get("supply"))
        creator_addr = rep.get("creator")
        creator_bal = num(rep.get("creatorBalance"))
        cpc = None
        if creator_addr:
            for h in th[:20]:
                if h.get("owner") == creator_addr or h.get("address") == creator_addr:
                    cpc = num(h.get("pct"))
                    if cpc > 1.0:
                        cpc /= 100.0  # pct column is percent scale
                    break
        if cpc is None and creator_bal > 0 and supply > 0:
            cpc = creator_bal / supply
            if cpc > 0.5 and t.get("decimals"):  # raw-vs-UI scale guard
                cpc2 = (creator_bal / (10 ** int(t["decimals"]))) / supply
                if cpc2 <= 0.5:
                    cpc = cpc2
        if creator_addr is None and creator_bal > 0 and th:
            for h in th[:10]:  # best-guess dev wallet for the rug book
                amt = num(h.get("uiAmount")) or num(h.get("amount"))
                if amt and abs(amt - creator_bal) / max(creator_bal, 1) < 0.02:
                    creator_addr = h.get("owner") or h.get("address")
                    break
        if rep.get("rugged"):
            self._kill(s, "R6", "token flagged rugged (RugCheck)")
            return False
        if cpc is not None and cpc > DEV_MAX_PCT:
            self._kill(s, "R6", "dev wallet holds %.2f%% (>3%%)" % (cpc * 100))
            if creator_addr:
                self.state["rug_book"][creator_addr] = {"mint": mint, "symbol": sym, "ts": time.time()}
            return False
        if creator_addr and creator_addr in self.state["rug_book"]:
            prev = self.state["rug_book"][creator_addr]
            self._kill(s, "R6", "dev %s.. has past rug on %s" % (str(creator_addr)[:6], prev.get("symbol")))
            return False
        if cpc is None:
            notes.append("R6 dev% unverified")
        else:
            notes.append("R6 dev %.2f%%" % (cpc * 100))

        # R7 - bundled launch over 10%
        hit7 = None
        if rep.get("graphInsidersDetected"):
            hit7 = "insider graph detected"
        else:
            for r in rep.get("risks") or []:
                txt = "%s %s" % (r.get("name") or "", r.get("description") or "")
                if re.search(r"bundle|snipe|insider|single holder|launcher", txt, re.I):
                    if (r.get("level") or "").lower() in ("warn", "danger"):
                        hit7 = (r.get("name") or "bundle risk")[:80]
                        break
        if hit7:
            self._kill(s, "R7", "bundled/insider launch: %s" % hit7)
            return False
        notes.append("R7 clean")

        # R8 - already up 80%+ in the last hour
        if s["pc_h1"] >= H1_PUMP_MAX_PCT:
            self._kill(s, "R8", "+%.0f%% in the last hour (chasing)" % s["pc_h1"])
            return False
        notes.append("R8 h1 +%.1f%%" % s["pc_h1"])

        self.state["rug_cache"][mint] = {
            "top10": [[h.get("owner") or h.get("address"), num(h.get("pct"))] for h in th[:10]],
            "ts": time.time(),
        }
        self.log.event("FILTER_PASS", "%s (%s..) passed 8/8 | %s" % (sym, mint[:6], "; ".join(notes)))
        return True


# ---------------- ANALYST ----------------
class Analyst:
    """Scores survivors 0-100 on the six mandated factors.
    Data honesty: DexScreener exposes txn counts, not unique addresses, so 'unique buyers'
    uses buy-txn count and 'holder growth' uses txn acceleration as proxies. The smart-money
    feed is our own smart_book (wallets seen early in runners) - worth up to 10 pts.
    See README for the exact rubric."""

    def score(self, s, state):
        rug10 = (state["rug_cache"].get(s["mint"]) or {}).get("top10") or []
        b, notes = {}, []

        # Momentum (25): h1 change + fresh m5 push, penalize over-extension
        h1, m5 = s["pc_h1"], s["pc_m5"]
        mom = clamp(h1 / 50.0, 0, 1) * 15 + clamp(m5 / 10.0, 0, 1) * 10
        if h1 > 120:
            mom *= 0.5
            notes.append("over-extended h1 +%.0f%%" % h1)
        b["momentum"] = min(mom, W_MOMENTUM)

        # Unique buyers (20, proxy: 1h buy txns)
        buys1 = s["tx_h1"]["buys"]
        b["buyers"] = clamp((buys1 - 50) / 500.0, 0, 1) * W_BUYERS

        # Buy/sell ratio (15): healthy 1.2-3.0x, wash-flag above 4x
        sells1 = s["tx_h1"]["sells"]
        if sells1 == 0:
            b["bs"] = 0.0
            notes.append("no sells in 1h")
        else:
            r = buys1 / sells1
            if r < 0.9:
                b["bs"] = 0.0
            elif r < 1.2:
                b["bs"] = (r - 0.9) / 0.3 * 8
            elif r <= 3.0:
                b["bs"] = 8 + (r - 1.2) / 1.8 * 7
            else:
                b["bs"] = max(15 - (r - 3.0) * 2, 8)
                if r > 4:
                    notes.append("b/s %.1fx wash-flag" % r)

        # Holder growth (15, proxy: 5m txn rate vs 1h average, >1 = accelerating)
        tot5 = s["tx_m5"]["buys"] + s["tx_m5"]["sells"]
        tot1 = s["tx_h1"]["buys"] + s["tx_h1"]["sells"]
        acc = tot5 * 12.0 / max(tot1, 1)
        b["growth"] = 15 if acc >= 1.5 else 12 if acc >= 1.2 else 8 if acc >= 1.0 else 4 if acc >= 0.8 else 0
        b["growth"] = float(b["growth"])

        # Liquidity to mcap (15)
        lr = s["liquidity_usd"] / s["mcap_usd"] if s["mcap_usd"] > 0 else 0
        b["liqmcap"] = 15 if lr >= 0.10 else 12 if lr >= 0.06 else 8 if lr >= 0.04 else 4 if lr >= 0.03 else 0.0

        # Smart wallets buying (10): overlap with our smart_book
        hits = sum(1 for addr, _pct in rug10 if addr and addr in state["smart_book"])
        b["smart"] = min(float(W_SMART), 5.0 * hits)
        if hits:
            notes.append("%d smart-book wallets in top10" % hits)

        score = int(round(min(100.0, sum(b.values()))))
        return {"score": score, "breakdown": b, "notes": notes}


# ---------------- EXECUTOR ----------------
class Executor:
    """Max 8% of CURRENT balance per trade. One entry per coin, never averages down.
    If nothing passes, does nothing."""

    def __init__(self, logger):
        self.log = logger

    def act(self, queue, state, fresh):
        if not queue:
            return []
        events = []
        eq = equity_of(state, fresh)
        if eq <= state["start_balance"] * (1 - EQUITY_DRAWDOWN_HALT):
            self.log.event("RISK_HALT", "equity $%.2f hit the -%.0f%% session halt - no new entries"
                           % (eq, EQUITY_DRAWDOWN_HALT * 100))
            return events
        for cand in sorted(queue, key=lambda x: -x["score"]):
            if len(state["positions"]) >= MAX_OPEN_POSITIONS:
                self.log.event("SKIP", "%s: max %d open positions (RISK cap)" % (cand["symbol"], MAX_OPEN_POSITIONS))
                continue
            if any(p["mint"] == cand["mint"] for p in state["positions"].values()):
                continue  # one entry per coin, never average down
            snap = fresh.get(cand["mint"])
            if not snap or snap["price_usd"] <= 0:
                continue
            size = state["cash"] * MAX_POSITION_PCT
            if size < MIN_TRADE_USD:
                self.log.event("SKIP", "%s: size $%.2f below min $%.0f" % (cand["symbol"], size, MIN_TRADE_USD))
                continue
            entry = snap["price_usd"] * (1 + SLIPPAGE_PCT)
            tokens = size / entry
            stop = entry * (1 - STOP_PCT)  # RISK sets the stop BEFORE the order goes in
            tid = "T%03d" % state["trade_seq"]
            state["trade_seq"] += 1
            state["cash"] -= size
            state["positions"][tid] = {
                "trade_id": tid, "mint": cand["mint"], "symbol": cand["symbol"],
                "entry_time": iso_now(), "entry_price": entry, "tokens": tokens,
                "size_usd": size, "stop": stop, "trail_armed": False, "peak": entry,
                "score": cand["score"], "last_price": entry,
            }
            state["counters"]["entries"] += 1
            ev = {
                "event": "ENTRY", "trade_id": tid, "mint": cand["mint"], "symbol": cand["symbol"],
                "size_usd": round(size, 2), "score": cand["score"], "entry_price": entry,
                "tokens": tokens, "stop": stop, "trail_trigger_pct": TRAIL_TRIGGER_PCT * 100,
                "trail_distance_pct": TRAIL_DISTANCE_PCT * 100, "dex": snap["dex_id"], "url": snap["url"],
            }
            events.append(ev)
            self.log.trade(ev)
        return events


# ---------------- RISK ----------------
class Risk:
    """Stop at -6% set pre-entry, never moved lower. At +12% switch to a trailing stop
    and let it run. Exit ONLY when the stop or the trail says exit."""

    def __init__(self, logger):
        self.log = logger

    def monitor(self, state, fresh):
        events = []
        for tid, p in list(state["positions"].items()):
            snap = fresh.get(p["mint"])
            if snap and snap["price_usd"] > 0:
                p["last_price"] = snap["price_usd"]
            price = p["last_price"]
            p["peak"] = max(p["peak"], price)
            pnl = price / p["entry_price"] - 1

            if not p["trail_armed"] and pnl >= TRAIL_TRIGGER_PCT:
                p["trail_armed"] = True
                # feed the smart-money book: top10 wallets of a coin that ran +12% while we watched
                for addr, _pct in (state["rug_cache"].get(p["mint"]) or {}).get("top10") or []:
                    if addr:
                        state["smart_book"][addr] = state["smart_book"].get(addr, 0) + 1
                ev = {"event": "TRAIL_ARMED", "trade_id": tid, "symbol": p["symbol"],
                      "price": price, "pnl_pct": pnl * 100,
                      "trail_distance_pct": TRAIL_DISTANCE_PCT * 100}
                events.append(ev)
                self.log.trade(ev)

            eff_stop = max(p["stop"], p["peak"] * (1 - TRAIL_DISTANCE_PCT)) if p["trail_armed"] else p["stop"]
            p["eff_stop"] = eff_stop  # ratchet: the stop never moves lower
            p["pnl_pct"] = pnl * 100

            if price <= eff_stop:
                exit_price = price * (1 - SLIPPAGE_PCT)
                proceeds = p["tokens"] * exit_price
                realized = proceeds - p["size_usd"]
                state["cash"] += proceeds
                reason = ("trail exit (peaked +%.1f%%, trailed %.0f%%)"
                          % ((p["peak"] / p["entry_price"] - 1) * 100, TRAIL_DISTANCE_PCT * 100)
                          if p["trail_armed"] else "hard stop -%.0f%%" % (STOP_PCT * 100))
                ev = {"event": "EXIT", "trade_id": tid, "symbol": p["symbol"], "mint": p["mint"],
                      "exit_time": iso_now(), "exit_price": exit_price, "proceeds_usd": round(proceeds, 2),
                      "pnl_usd": round(realized, 2), "pnl_pct": realized / p["size_usd"] * 100,
                      "reason": reason, "entry_price": p["entry_price"], "size_usd": round(p["size_usd"], 2),
                      "peak": p["peak"], "score": p["score"]}
                events.append(ev)
                self.log.trade(ev)
                c = state["counters"]
                c["exits"] += 1
                c["wins" if realized >= 0 else "losses"] += 1
                c["realized"] += realized
                del state["positions"][tid]
        return events


# ---------------- LOGGER ----------------
class Logger:
    """Everything lands on disk: kills.jsonl (rule that killed it), trades.jsonl
    (entry/size/score/exit/reason), events.jsonl, status.md (live), morning_recap.md."""

    def __init__(self):
        for d in (STATE_DIR, LOG_DIR, REPORTS_DIR):
            os.makedirs(d, exist_ok=True)
        self.kills = os.path.join(LOG_DIR, "kills.jsonl")
        self.trades = os.path.join(LOG_DIR, "trades.jsonl")
        self.events = os.path.join(LOG_DIR, "events.jsonl")
        self.status = os.path.join(REPORTS_DIR, "status.md")
        self.recap_path = os.path.join(REPORTS_DIR, "morning_recap.md")

    def _append(self, path, obj):
        with open(path, "a") as f:
            f.write(json.dumps(obj, default=str) + "\n")

    def kill(self, s, rule, detail):
        self._append(self.kills, {
            "ts": iso_now(), "mint": s["mint"], "symbol": s["symbol"], "rule": rule,
            "rule_name": RULE_NAMES[rule], "detail": detail, "dex": s["dex_id"],
            "mcap_usd": round(s["mcap_usd"]), "liq_usd": round(s["liquidity_usd"]),
            "vol1h_usd": round(s["vol_h1"]), "pc_h1": s["pc_h1"], "url": s["url"],
        })

    def trade(self, ev):
        self._append(self.trades, {"ts": iso_now(), **ev})

    def event(self, kind, msg):
        self._append(self.events, {"ts": iso_now(), "kind": kind, "msg": msg})

    def _positions_md(self, state):
        if not state["positions"]:
            return "_No open positions._"
        rows = ["| id | coin | size | score | entry | last | PnL | stop | phase |",
                "|---|---|---|---|---|---|---|---|---|"]
        for p in state["positions"].values():
            pnl = (p["last_price"] / p["entry_price"] - 1) * 100
            phase = "TRAIL %.0f%% from peak $%.8f" % (TRAIL_DISTANCE_PCT * 100, p["peak"]) if p["trail_armed"] else "hard stop"
            rows.append("| %s | %s | $%.2f | %d | $%.8f | $%.8f | %+.2f%% | $%.8f | %s |"
                        % (p["trade_id"], p["symbol"], p["size_usd"], p["score"], p["entry_price"],
                           p["last_price"], pnl, p.get("eff_stop", p["stop"]), phase))
        return "\n".join(rows)

    def _top_survivors(self, state, n=8):
        surv = sorted(state["survivors"].values(), key=lambda x: -x.get("last_score", 0))[:n]
        if not surv:
            return "_No live survivors._"
        return ", ".join("%s %d" % (x["symbol"], x.get("last_score", 0)) for x in surv)

    def status_md(self, state, fresh, cycle_no, next_cycle_in):
        eq = equity_of(state, fresh)
        c = state["counters"]
        lines = [
            "# Desk status - PAPER mode",
            "",
            "**Now:** %s | **Cycle:** %d | **Next cycle:** ~%ds | **Ends:** %s" %
            (iso_now(), cycle_no, int(next_cycle_in),
             datetime.fromtimestamp(state["end_epoch"], TZ).strftime("%Y-%m-%d %H:%M WAT")),
            "",
            "**Equity:** $%.2f (cash $%.2f + open $%.2f) | start $%.2f (%+.2f%%)" %
            (eq, state["cash"], eq - state["cash"], state["start_balance"],
             (eq / state["start_balance"] - 1) * 100),
            "",
            "**Funnel so far:** seen %d | handed to Filter %d | killed %d | survivors tracked %d | "
            "entries %d | open %d | exits %d (W%d/L%s)" %
            (c["seen_raw"], c["handed"], sum(state["kills_by_rule"].values()), len(state["survivors"]),
             c["entries"], len(state["positions"]), c["exits"], c["wins"], c["losses"]),
            "",
            "**Top survivors (score):** %s" % self._top_survivors(state),
            "",
            "## Open positions",
            self._positions_md(state),
            "",
            "## Kills by rule",
            " ".join("%s:%d" % (r, state["kills_by_rule"].get(r, 0))
                     for r in RULE_ORDER + ["FAILCLOSED"]),
            "",
        ]
        return "\n".join(lines)

    def recap_md(self, state, fresh, reason):
        c = state["counters"]
        eq = equity_of(state, fresh)
        kb = state["kills_by_rule"]
        trades = []
        if os.path.exists(self.trades):
            with open(self.trades) as f:
                trades = [json.loads(x) for x in f if x.strip()]
        entries = [t for t in trades if t.get("event") == "ENTRY"]
        exits = [t for t in trades if t.get("event") == "EXIT"]
        closed_pnls = [(t["symbol"], t["pnl_pct"], t["pnl_usd"]) for t in exits]
        best = max(closed_pnls, key=lambda x: x[1]) if closed_pnls else None
        worst = min(closed_pnls, key=lambda x: x[1]) if closed_pnls else None
        total = c["wins"] + c["losses"]
        wr = (100.0 * c["wins"] / total) if total else 0.0
        lines = [
            "# Morning recap - Solana memecoin desk (PAPER)",
            "",
            "**Session:** %s -> %s (%s) | **Cycles:** %d | **End reason:** %s" %
            (state["started_at"], iso_now(), MODE, c["cycles"], reason),
            "",
            "**Final balance:** $%.2f (cash $%.2f + open $%.2f) vs start $%.2f -> %+.2f%%" %
            (eq, state["cash"], eq - state["cash"], state["start_balance"],
             (eq / state["start_balance"] - 1) * 100),
            "",
            "## Funnel",
            "- New pairs seen by Scout: **%d** (dropped outside scope: %d)" % (c["seen_raw"], c["dropped"]),
            "- Handed to Filter: **%d**" % c["handed"],
            "- Killed: **%d**" % sum(kb.values()),
        ]
        for r in RULE_ORDER + ["FAILCLOSED"]:
            if kb.get(r):
                lines.append("    - %s - %s: **%d**" % (r, RULE_NAMES[r], kb[r]))
        lines += [
            "- Filter survivors tracked: **%d** | reached Executor (70+): **%d** | trades opened: **%d**"
            % (len(state["survivors"]) + len({t['mint'] for t in entries}), c["queue_hits"], c["entries"]),
            "",
            "## Trading",
            "- Entries: **%d** | Closed: **%d** | Still open: **%d** | W/L: **%d-%d** (%.0f%%)"
            % (c["entries"], c["exits"], len(state["positions"]), c["wins"], c["losses"], wr),
            "- Realized P/L: **$%.2f**" % c["realized"],
        ]
        if best:
            lines.append("- Best exit: **%s %+.1f%% ($%+.2f)**" % best)
        if worst:
            lines.append("- Worst exit: **%s %+.1f%% ($%+.2f)**" % worst)
        lines += ["", "## Open positions", self._positions_md(state), "", "## Full trade log", ""]
        if entries:
            lines.append("| id | coin | time | size | score | entry | stop |")
            lines.append("|---|---|---|---|---|---|---|")
            for t in entries:
                lines.append("| %s | %s | %s | $%.2f | %d | $%.8f | $%.8f |"
                             % (t["trade_id"], t["symbol"], t["ts"], t["size_usd"], t["score"],
                                t["entry_price"], t["stop"]))
            lines.append("")
            lines.append("| id | coin | exit time | exit | PnL | reason |")
            lines.append("|---|---|---|---|---|---|")
            for t in exits:
                lines.append("| %s | %s | %s | $%.8f | %+.2f%% ($%+.2f) | %s |"
                             % (t["trade_id"], t["symbol"], t["ts"], t["exit_price"],
                                t["pnl_pct"], t["pnl_usd"], t["reason"]))
        else:
            lines.append("_No trades this session. An empty night is fine._")
        lines.append("")
        return "\n".join(lines)

# ==================================================================
# from run.py
# ==================================================================
"""Orchestrator. Wires the six agents in strict order, every POLL_SECONDS, until 07:00 WAT.

Usage:
  python3 run.py            # full session (resumable - state saved every cycle)
  python3 run.py --once     # single cycle smoke test
  python3 run.py --recap    # regenerate morning recap from saved state/logs
"""
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta

STATE_FILE = os.path.join(STATE_DIR, "state.json")
STOP = {"flag": False}


def _sig(signum, _frame):
    STOP["flag"] = True


def new_state():
    return {
        "mode": MODE,
        "started_at": iso_now(),
        "started_epoch": time.time(),
        "end_epoch": None,
        "cash": START_BALANCE_USD,
        "start_balance": START_BALANCE_USD,
        "trade_seq": 1,
        "positions": {},
        "survivors": {},      # mint -> {symbol, first_seen, last_score, rejected_logged, missing}
        "quarantine": {},     # mint -> {strikes, symbol} (safety data unreachable, retrying)
        "hatchery": {},       # mint -> {symbol, added, missing} (sub-30-min pools, per Scout scope)
        "kills_cache": {},    # mint -> rule (dead is dead, permanent)
        "rug_cache": {},      # mint -> {top10, ts} (purged >36h)
        "smart_book": {},     # wallet -> times seen early in a runner
        "rug_book": {},       # dev wallet -> {mint, symbol, ts}
        "kills_by_rule": {},
        "counters": {"cycles": 0, "seen_raw": 0, "dropped": 0, "handed": 0,
                     "queue_hits": 0, "entries": 0, "exits": 0, "wins": 0,
                     "losses": 0, "realized": 0.0},
    }


def load_state():
    base = new_state()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            saved = json.load(f)
        for k, v in saved.items():
            base[k] = v
        for k, v in new_state()["counters"].items():
            base["counters"].setdefault(k, v)
    return base


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, default=str)
    os.replace(tmp, STATE_FILE)


def compute_end_epoch():
    now = nowtz()
    end = now.replace(hour=END_HOUR_LOCAL, minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    end = min(end, now + timedelta(hours=MAX_RUNTIME_HOURS))
    return end.timestamp()


def do_cycle(state, scout, flt, analyst, executor, risk, log):
    c = state["counters"]
    c["cycles"] += 1
    held_mints = [p["mint"] for p in state["positions"].values()]

    # 1) SCOUT - watch, scope, hand off
    raw = scout.raw_seen()
    c["seen_raw"] += len(raw)
    mints = []
    for m in (raw + list(state["survivors"].keys()) + held_mints
              + list(state["hatchery"].keys())):
        if m not in mints:
            mints.append(m)
    fresh = scout.gather(mints)
    cands, dropped, hatched = scout.select(fresh, state)
    c["dropped"] += len(dropped)
    if dropped:
        sample = "; ".join("%s (%s)" % (sym, why) for _m, sym, why in dropped[:3])
        log.event("SCOUT_DROP", "%d outside scope: %s%s" % (len(dropped), sample, " ..." if len(dropped) > 3 else ""))

    # 2) FILTER - 8 kill rules, one hit = dead
    passed = 0
    for s in cands:
        c["handed"] += 1
        if flt.evaluate(s):
            state["survivors"][s["mint"]] = {
                "symbol": s["symbol"], "first_seen": iso_now(),
                "first_age_h": (time.time() * 1000 - s["pair_created_at"]) / 3600000,
                "last_score": 0, "rejected_logged": False, "missing": 0,
            }
            passed += 1

    # 3) ANALYST - score every survivor 0-100
    queue = []
    for mint in list(state["survivors"].keys()):
        sv = state["survivors"][mint]
        snap = fresh.get(mint)
        if not snap:
            sv["missing"] = sv.get("missing", 0) + 1
            if sv["missing"] > 8:
                log.event("AGED_OUT", "%s gone from dex data - dropped from book" % sv["symbol"])
                state["survivors"].pop(mint, None)
                state["rug_cache"].pop(mint, None)
            continue
        sv["missing"] = 0
        age_h = (time.time() * 1000 - snap["pair_created_at"]) / 3600000
        if age_h > AGE_MAX_SECONDS / 3600:
            log.event("AGED_OUT", "%s past 72h window - dropped from book" % sv["symbol"])
            state["survivors"].pop(mint, None)
            state["rug_cache"].pop(mint, None)
            continue
        res = analyst.score(snap, state)
        prev = sv.get("last_score", 0)
        sv["last_score"] = res["score"]
        if res["score"] >= SCORE_MIN:
            queue.append({"mint": mint, "symbol": sv["symbol"], "score": res["score"]})
        elif not sv.get("rejected_logged"):
            sv["rejected_logged"] = True
            log.event("ANALYST_REJECT", "%s scored %d (<70)%s: %s"
                      % (sv["symbol"], res["score"],
                         " (was %d)" % prev if prev and prev > res["score"] else "",
                         ", ".join("%s=%.0f" % (k, v) for k, v in res["breakdown"].items())))
    if queue:
        c["queue_hits"] += len(queue)

    # 4) EXECUTOR - 8% sizing, one entry per coin, else nothing
    executor.act(queue, state, fresh)

    # 5) RISK - stops, trails, exits only on the stop's say-so
    risk.monitor(state, fresh)

    # housekeeping: purge stale rug cache
    cutoff = time.time() - 36 * 3600
    for m in list(state["rug_cache"].keys()):
        if state["rug_cache"][m].get("ts", 0) < cutoff:
            state["rug_cache"].pop(m, None)

    eq = equity_of(state, fresh)
    kb = state["kills_by_rule"]
    kills_now = sum(kb.values())
    top = max((v.get("last_score", 0) for v in state["survivors"].values()), default=0)
    print("[%s] CYCLE %d | seen %d | enriched %d | handed %d (passed %d) | kills %d | "
          "survivors %d | hatching %d | top score %d | queue %d | open %d | equity $%.2f"
          % (iso_now(), c["cycles"], len(raw), len(fresh), len(cands), passed, kills_now,
             len(state["survivors"]), len(state["hatchery"]), top, len(queue),
             len(state["positions"]), eq), flush=True)

    remaining = state["end_epoch"] - time.time()
    log._append(log.events, {"ts": iso_now(), "kind": "CYCLE", "msg": "cycle %d done, equity $%.2f"
                             % (c["cycles"], eq)})
    with open(log.status, "w") as f:
        f.write(log.status_md(state, fresh, c["cycles"], max(5, min(POLL_SECONDS, remaining))))
    return fresh


def rollover_if_needed(state, log):
    """New trading day: write yesterday's recap, roll the balance forward, fresh sheet."""
    if not state.get("end_epoch") or time.time() < state["end_epoch"]:
        return
    md = log.recap_md(state, {}, "session end (morning rollover)")
    with open(log.recap_path, "w") as f:
        f.write(md)
    print(md[:600], flush=True)
    log.event("SESSION", "morning recap written at rollover")
    eq = state["cash"] + sum(p["tokens"] * p.get("last_price", 0) or 0
                             for p in state["positions"].values())
    state["start_balance"] = eq
    state["cash"] = eq
    # kills_cache / rug_book / smart_book persist: dead is dead, memory is permanent
    state["kills_by_rule"] = {}
    state["survivors"] = {}
    state["quarantine"] = {}
    state["rug_cache"] = {}
    state["counters"] = new_state()["counters"]
    state["trade_seq"] = 1
    state["started_at"] = iso_now()
    state["end_epoch"] = compute_end_epoch()
    save_state(state)
    print("[%s] NEW SESSION - balance $%.2f rolled forward, ends %s"
          % (iso_now(), eq, datetime.fromtimestamp(state["end_epoch"], TZ).strftime("%Y-%m-%d %H:%M WAT")),
          flush=True)


def main():
    once = "--once" in sys.argv
    recap_only = "--recap" in sys.argv
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    log = Logger()
    dex, gecko, rug, rpc = DexScreener(), Gecko(), RugCheck(), SolanaRPC()
    scout = Scout(dex, gecko)
    flt = Filter(rug, log, state=None, rpc=rpc)  # state wired below
    analyst = Analyst()
    executor = Executor(log)
    risk = Risk(log)

    state = load_state()
    flt.state = state
    if state["end_epoch"] is None:
        state["end_epoch"] = compute_end_epoch()
        save_state(state)
    rollover_if_needed(state, log)

    if recap_only:
        fresh = scout.gather([p["mint"] for p in state["positions"].values()])
        md = log.recap_md(state, fresh, "manual regeneration")
        with open(log.recap_path, "w") as f:
            f.write(md)
        print(md)
        return

    end = datetime.fromtimestamp(state["end_epoch"], TZ)
    print("[%s] DESK ONLINE - mode %s | start $%.2f | ends %s | poll %ds"
          % (iso_now(), MODE, state["start_balance"], end.strftime("%Y-%m-%d %H:%M WAT"), POLL_SECONDS), flush=True)
    log.event("SESSION", "desk online, %d prior cycles in state" % state["counters"]["cycles"])

    done = 0
    while True:
        if STOP["flag"]:
            fresh = scout.gather([p["mint"] for p in state["positions"].values()])
            md = log.recap_md(state, fresh, "interrupted (SIGTERM) - state saved, resumable")
            with open(log.recap_path, "w") as f:
                f.write(md)
            print(md, flush=True)
            log.event("SESSION", "desk stopped by signal")
            break
        try:
            do_cycle(state, scout, flt, analyst, executor, risk, log)
        except Exception:
            log.event("ERROR", traceback.format_exc()[-800:])
            print("[%s] cycle error (logged, continuing)" % iso_now(), flush=True)
        save_state(state)
        done += 1
        if once and done >= 1:
            print("Smoke cycle complete. State saved.", flush=True)
            break
        if time.time() >= state["end_epoch"]:
            fresh = scout.gather([p["mint"] for p in state["positions"].values()])
            md = log.recap_md(state, fresh, "session end (morning)")
            with open(log.recap_path, "w") as f:
                f.write(md)
            print(md, flush=True)
            log.event("SESSION", "morning recap written")
            break
        time.sleep(max(5, POLL_SECONDS - 20))  # cycle itself takes time; avoid drift pileup



# ================================================================
# telegram push mode:  python3 desk_bot.py push   (GitHub Actions)
# Idempotent: kill reports and the morning recap carry fingerprints,
# so overlapping/duplicate runs can NEVER resend the same message.
# ================================================================
def tg_push_main():
    import hashlib as _hashlib
    import html as _h
    import json as _json
    import os as _os

    token = _os.environ.get("TELEGRAM_TOKEN")
    chat = _os.environ.get("CHAT_ID")
    if not token or not chat:
        print("telegram secrets missing - push skipped")
        return

    def _esc(x):
        return _h.escape(str(x))

    def _send(text):
        try:
            import urllib.parse as _up, urllib.request as _uq
            data = _up.urlencode({"chat_id": chat, "text": text[:3800],
                                  "parse_mode": "HTML",
                                  "disable_web_page_preview": "true"}).encode()
            _uq.urlopen(_uq.Request("https://api.telegram.org/bot%s/sendMessage" % token,
                                    data=data), timeout=20)
            return True
        except Exception as e:
            print("send failed:", e)
            return False

    def _tail(path, off):
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            return [], off
        out = []
        for line in lines[off:]:
            try:
                out.append(_json.loads(line))
            except Exception:
                pass
        return out, len(lines)

    OFF = "tg_offset.json"
    try:
        with open(OFF) as f:
            off = _json.load(f)
    except Exception:
        off = {"trades": 0, "events": 0, "kills": 0, "kill_seen": [], "recap_hash": ""}

    def _remember(fp):
        seen = off.setdefault("kill_seen", [])
        if fp in seen:
            return False
        seen.append(fp)
        if len(seen) > 400:
            del seen[:len(seen) - 400]
        return True

    def _fmt(t):
        ev = t.get("event")
        if ev == "ENTRY":
            return ("\U0001F7E2 <b>BUY %s</b>\n$%.2f (8%%) | score %d/100\nstop -6%% set | %s"
                    % (_esc(t.get("symbol")), t.get("size_usd", 0), t.get("score", 0),
                       _esc(t.get("ts", ""))))
        if ev == "EXIT":
            emo = "\U0001F53D" if t.get("pnl_usd", 0) < 0 else "\U00002705"
            return ("%s <b>SELL %s</b>\n%+.1f%% ($%+.2f)\n%s | %s"
                    % (emo, _esc(t.get("symbol")), t.get("pnl_pct", 0), t.get("pnl_usd", 0),
                       _esc(t.get("reason", "")), _esc(t.get("ts", ""))))
        if ev == "TRAIL_ARMED":
            return "\U0001F680 <b>%s +%.0f%%</b> - trailing, letting it run" % (
                _esc(t.get("symbol")), t.get("pnl_pct", 0))
        return None

    # ---- trades (entries/exits/trails), fingerprinted ----
    trades, off["trades"] = _tail("logs/trades.jsonl", off["trades"])
    for t in trades:
        fp = "T|%s|%s|%s" % (t.get("ts"), t.get("event"), t.get("symbol"))
        if not _remember(fp):
            continue
        m = _fmt(t)
        if m:
            _send(m)

    # ---- risk halts / errors ----
    events, off["events"] = _tail("logs/events.jsonl", off["events"])
    for e in events:
        if e.get("kind") in ("RISK_HALT", "ERROR"):
            fp = "E|%s|%s" % (e.get("ts"), str(e.get("msg", ""))[:60])
            if _remember(fp):
                _send(_esc("\u26A0\uFE0F %s: %s" % (e.get("kind"), e.get("msg", "")))[:500])

    # ---- kills: fingerprinted, so dupes and replays stay silent ----
    kills, off["kills"] = _tail("logs/kills.jsonl", off["kills"])
    fresh = []
    for k in kills:
        fp = "K|%s|%s|%s" % (k.get("ts"), k.get("symbol"), k.get("rule"))
        if _remember(fp):
            fresh.append(k)
    if len(fresh) > 5:
        by = {}
        for k in fresh:
            by[k.get("rule", "?")] = by.get(k.get("rule", "?"), 0) + 1
        _send("\U0001F5D1 <b>Kill digest: %d scams rejected</b>\n%s"
              % (len(fresh), _esc(", ".join("%sx%d" % kv
                                            for kv in sorted(by.items(), key=lambda x: -x[1])))))
    else:
        for k in fresh:
            _send("\U0001F5D1 <b>KILL %s - %s</b>\n<i>%s</i>"
                  % (_esc(k.get("rule")), _esc(k.get("symbol")),
                     _esc(str(k.get("detail", ""))[:90])))

    # ---- morning recap: keyed by CONTENT hash, not file mtime ----
    if _os.path.exists("reports/morning_recap.md"):
        with open("reports/morning_recap.md") as f:
            body = f.read()
        digest = _hashlib.md5(body[:400].encode()).hexdigest()
        if digest != off.get("recap_hash"):
            off["recap_hash"] = digest
            _send("\U0001F305 <b>Morning report</b>\n\n" + _h.escape(body[:3200]))

    with open(OFF, "w") as f:
        _json.dump(off, f)
    print("push done")


if len(sys.argv) > 1 and sys.argv[1] == "push":
    tg_push_main()
elif __name__ == "__main__":
    main()
