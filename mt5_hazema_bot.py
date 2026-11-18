from __future__ import annotations

"""
HAZEMA MT5 Python bot — Heiken Ashi Zero-Lag EMA on MetaTrader 5.

Port of HAZEMA v1.2 by JustUncleL (PineScript).

USER SPEC (2026-11-18):
  - EUR/USD, DEMO account, MT5, automatic trading ON
  - $1 per trade (max loss $1.00: 0.01 lots x 10-pip SL x $0.10/pip)
  - 1-minute signals (M1)
  - Only trade signals scoring 70 or above (0-100 strength score)

WORKS WITH ANY MT5 BROKER: the bot talks to the MT5 terminal running
on the Windows machine and trades whichever account is logged in there.
To switch brokers, log into the other broker's account in MT5 and run
the bot again — no code change. Two things that CAN differ per broker:
  - Symbol name: some brokers call it EURUSD.pro / EURUSDm / EURUSD+
    instead of EURUSD. Set `symbol` in BotConfig to the broker's name.
  - 4-digit vs 5-digit pricing: handled automatically — SL/TP/spread
    are stored in PIPS and converted from the broker's own digits.
  - If two brokers' terminals are installed, set `terminal_path` to
    the .exe of the terminal you want (e.g. the XM or Exness folder).

Signal (evaluated on the last CLOSED bar, first bar of a streak only):
  BUY  when ZEMA 20 rising + green Heiken Ashi candle
       + SMMA 11 > EMA 89 + MACD 13/26/9 histogram green
  SELL when ZEMA 20 falling + red Heiken Ashi candle
       + SMMA 11 < EMA 89 + MACD 13/26/9 histogram red

Strength score (0-100, BUY shown, SELL mirrored):
  HA streak 3+ bars ......... 20 | ZEMA rising 3 bars .... 20
  Trend gap vs ATR .......... 20 | MACD > 0 and rising .... 20
  No lower wick + big body .. 20 | (sums to 100)

Defaults: EURUSD, M1, $1.00 risk, SL 10 pips, TP 15 pips (1.5R),
max spread 2.5 pips, one position at a time, daily +/-2% stops.
execute_live is TRUE: it places orders. A demo-only guard refuses
live orders on a REAL account unless live_confirmed=True.

Requires: Windows + MT5 terminal running, logged into a DEMO account,
pip install MetaTrader5. Cannot run on Linux or on a phone.
UNVALIDATED: no backtest published for this logic. Demo first.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional
import time

try:
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - runtime dependency check
    mt5 = None

Signal = Literal["BUY", "SELL", "NO_TRADE"]


def ema(values: List[float], period: int) -> List[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return [None] * len(values)
    out: List[Optional[float]] = [None] * len(values)
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        val = values[i] * k + prev * (1 - k)
        out[i] = val
        prev = val
    return out


def smma(values: List[float], period: int) -> List[Optional[float]]:
    """Wilder smoothed MA: SMMA[i] = (SMMA[i-1]*(n-1) + src[i]) / n."""
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return [None] * len(values)
    out: List[Optional[float]] = [None] * len(values)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        val = (prev * (period - 1) + values[i]) / period
        out[i] = val
        prev = val
    return out


def zema(values: List[float], period: int = 20) -> List[Optional[float]]:
    """Zero Lag EMA: data[i] = src[i] + (src[i] - src[i-lag]), lag=(n-1)//2."""
    lag = (period - 1) // 2
    data: List[float] = []
    for i, v in enumerate(values):
        if i >= lag:
            data.append(v + (v - values[i - lag]))
        else:
            data.append(v)
    return ema(data, period)


def heiken_ashi(rates: List[dict]) -> tuple:
    """Return (ha_open, ha_close) lists from chronological OHLC bars."""
    ha_o: List[float] = []
    ha_c: List[float] = []
    for i, r in enumerate(rates):
        c = (r["open"] + r["high"] + r["low"] + r["close"]) / 4.0
        if i == 0:
            o = (r["open"] + r["close"]) / 2.0
        else:
            o = (ha_o[-1] + ha_c[-1]) / 2.0
        ha_o.append(o)
        ha_c.append(c)
    return ha_o, ha_c


@dataclass
class BotConfig:
    symbol: str = "EURUSD"
    timeframe = mt5.TIMEFRAME_M1 if mt5 else 1
    candles_needed: int = 300
    terminal_path: str = ""      # leave empty to use the running terminal;
    # set to the broker terminal's .exe path if several are installed
    risk_dollars: float = 1.0    # max loss per trade, $1.00
    sl_pips: float = 10.0        # 10 pips (converted to points per broker)
    tp_pips: float = 15.0        # 15 pips (1.5R)
    min_strength: int = 70       # only trade signals scoring >= 70/100
    max_spread_pips: float = 2.5  # 2.5 pips
    max_daily_loss_pct: float = 2.0
    max_daily_profit_pct: float = 2.0
    poll_seconds: int = 5
    use_trend_filter: bool = True  # SMMA 11 vs EMA 89
    use_macd_filter: bool = True   # MACD 13/26/9 histogram colour
    execute_live: bool = True      # True = place real orders when a signal fires
    live_confirmed: bool = False   # Must be True to trade on a REAL account.
    # Safety: live orders are refused on a real account unless
    # live_confirmed is True. Demo accounts always trade when
    # execute_live is True.


class HAZEMAMT5Bot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.day_anchor = datetime.now().date()
        self.day_start_balance: Optional[float] = None
        self.last_closed_time = 0

    def connect(self) -> None:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package is not installed.")
        if self.cfg.terminal_path:
            ok = mt5.initialize(path=self.cfg.terminal_path)
        else:
            ok = mt5.initialize()
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("Cannot read MT5 account info.")
        self.day_start_balance = float(info.balance)
        print(f"[INIT] Connected. Balance={info.balance:.2f}")

    def shutdown(self) -> None:
        if mt5:
            mt5.shutdown()

    def _reset_day_if_needed(self) -> None:
        today = datetime.now().date()
        if today != self.day_anchor:
            self.day_anchor = today
            info = mt5.account_info()
            self.day_start_balance = float(info.balance) if info else self.day_start_balance
            print(f"[DAY] New day. Start balance={self.day_start_balance:.2f}")

    def _daily_stop_hit(self) -> bool:
        self._reset_day_if_needed()
        if self.day_start_balance is None:
            return False
        info = mt5.account_info()
        if info is None:
            return False
        pnl_pct = ((float(info.balance) - self.day_start_balance) / self.day_start_balance) * 100
        if pnl_pct <= -self.cfg.max_daily_loss_pct:
            print(f"[STOP] Daily loss reached: {pnl_pct:.2f}%")
            return True
        if pnl_pct >= self.cfg.max_daily_profit_pct:
            print(f"[STOP] Daily profit reached: {pnl_pct:.2f}%")
            return True
        return False

    def _spread_ok(self) -> bool:
        tick = mt5.symbol_info_tick(self.cfg.symbol)
        symbol = mt5.symbol_info(self.cfg.symbol)
        if tick is None or symbol is None or symbol.point <= 0:
            return False
        spread_pips = (tick.ask - tick.bid) / symbol.point / self._points_per_pip()
        if spread_pips > self.cfg.max_spread_pips:
            print(f"[SKIP] Spread too high: {spread_pips:.1f} pips")
            return False
        return True

    def _has_position(self) -> bool:
        positions = mt5.positions_get(symbol=self.cfg.symbol)
        return bool(positions)

    def _get_rates(self) -> List[dict]:
        rates = mt5.copy_rates_from_pos(self.cfg.symbol, self.cfg.timeframe, 0, self.cfg.candles_needed)
        if rates is None or len(rates) == 0:
            return []
        return [
            {"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"])}
            for r in rates
        ]

    def _points_per_pip(self) -> float:
        """Points in one pip for this broker's symbol: 10 on 5/3-digit, 1 on 4/2-digit."""
        symbol = mt5.symbol_info(self.cfg.symbol)
        if symbol is None:
            return 10.0
        digits = int(getattr(symbol, "digits", 5) or 5)
        return 10.0 if digits in (3, 5) else 1.0

    def _pip_value_per_lot(self) -> float:
        """Dollars per pip for 1 lot. EURUSD 5-digit: tick $1.00 x 10 ticks/pip = $10."""
        symbol = mt5.symbol_info(self.cfg.symbol)
        if symbol is None:
            return 10.0
        tick_size = float(symbol.trade_tick_size) or 0.00001
        tick_value = float(symbol.trade_tick_value) or 1.0
        return tick_value * (0.0001 / tick_size)

    def _calc_lot(self) -> tuple:
        """Lots sizing the trade to risk_dollars at the SL distance.

        Returns (lots, actual_risk_dollars). Floored at the broker's
        minimum lot; if the floor makes the risk exceed risk_dollars,
        the actual risk is reported, not hidden.
        """
        symbol = mt5.symbol_info(self.cfg.symbol)
        if symbol is None:
            return 0.01, self.cfg.risk_dollars
        pip_value = self._pip_value_per_lot()
        lots = self.cfg.risk_dollars / max(self.cfg.sl_pips * pip_value, 1e-9)
        step = float(symbol.volume_step) or 0.01
        lots = max(float(symbol.volume_min) or 0.01, round(lots / step) * step)
        lots = min(lots, float(symbol.volume_max) or lots)
        actual_risk = lots * self.cfg.sl_pips * pip_value
        return round(lots, 2), round(actual_risk, 2)

    def _atr(self, ha_high: List[float], ha_low: List[float], lookback: int = 20) -> float:
        if len(ha_high) < lookback:
            return 0.0
        ranges = [ha_high[-i] - ha_low[-i] for i in range(1, lookback + 1)]
        return sum(ranges) / len(ranges)

    def _score(self, side: Signal, idx: int, ha_o, ha_c, ha_h, ha_l,
               z, fast, slow, macd, atr: float) -> tuple:
        """0-100 strength score for a fired signal (side mirrored). Returns (score, reasons)."""
        pts = 0
        reasons: List[str] = []
        up = side == "BUY"

        # 1. HA streak (max 20): consecutive same-colour HA candles ending at idx
        streak = 0
        j = idx
        while j >= 0:
            green = ha_o[j] < ha_c[j]
            if green == up:
                streak += 1
                j -= 1
            else:
                break
        streak_pts = 20 if streak >= 3 else (15 if streak == 2 else 10)
        pts += streak_pts
        reasons.append(f"HA streak {streak} (+{streak_pts})")

        # 2. ZEMA momentum (max 20): rising/falling 3 bars in a row
        if up:
            z3 = z[idx] > z[idx - 1] > z[idx - 2]
        else:
            z3 = z[idx] < z[idx - 1] < z[idx - 2]
        z_pts = 20 if z3 else 10
        pts += z_pts
        reasons.append(f"ZEMA 3-bar {'rise' if z3 else 'turn'} (+{z_pts})")

        # 3. Trend gap vs ATR (max 20)
        gap = abs(fast[idx] - slow[idx])
        if atr > 0 and gap > 0.5 * atr:
            g_pts = 20
        elif atr > 0 and gap > 0.2 * atr:
            g_pts = 15
        else:
            g_pts = 10
        pts += g_pts
        reasons.append(f"trend gap {gap:.5f} vs ATR {atr:.5f} (+{g_pts})")

        # 4. MACD (max 20): on the right side of zero + rising/falling 3 bars
        if up:
            side_ok = macd[idx] > 0
            rising = macd[idx] > macd[idx - 1] > macd[idx - 2]
        else:
            side_ok = macd[idx] < 0
            rising = macd[idx] < macd[idx - 1] < macd[idx - 2]
        m_pts = (10 if side_ok else 0) + (10 if rising else 0)
        pts += m_pts
        reasons.append(f"MACD {'right' if side_ok else 'wrong'} side, {'moving' if rising else 'flat'} (+{m_pts})")

        # 5. Wick + body (max 20): no wick against the trade + a real body
        if up:
            no_wick = ha_l[idx] >= ha_o[idx] - 1e-9
        else:
            no_wick = ha_h[idx] <= ha_o[idx] + 1e-9
        body = abs(ha_c[idx] - ha_o[idx])
        w_pts = (10 if no_wick else 0) + (10 if atr > 0 and body > 0.3 * atr else 0)
        pts += w_pts
        reasons.append(f"{'clean' if no_wick else 'wicked'} candle, body {body:.5f} (+{w_pts})")

        return pts, reasons

    def generate_signal(self, rates: List[dict]) -> tuple:
        """HAZEMA signal on the last CLOSED bar; fires only on the first bar of a streak.

        Returns (signal, score, reasons). Score is 0 when no signal fires.
        """
        if len(rates) < 100:
            return "NO_TRADE", 0, []
        ha_o, ha_c = heiken_ashi(rates)
        ha_h = [max(r["high"], o, c) for r, o, c in zip(rates, ha_o, ha_c)]
        ha_l = [min(r["low"], o, c) for r, o, c in zip(rates, ha_o, ha_c)]
        n = len(ha_c)
        z = zema(ha_c, 20)
        fast = smma(ha_c, 11)
        slow = ema(ha_c, 89)
        e13 = ema(ha_c, 13)
        e26 = ema(ha_c, 26)
        macd: List[Optional[float]] = [None] * n
        for i in range(n):
            if e13[i] is not None and e26[i] is not None:
                macd[i] = e13[i] - e26[i]
        atr = self._atr(ha_h, ha_l, 20)

        # last closed bar is index -2 (index -1 is the forming bar)
        idx = n - 2

        def call_at(j: int) -> bool:
            if z[j] is None or z[j - 1] is None or fast[j] is None or slow[j] is None:
                return False
            if macd[j] is None or macd[j - 1] is None or macd[j - 2] is None or macd[j - 3] is None:
                return False
            zema_up = z[j] > z[j - 1]
            ha_green = ha_o[j] < ha_c[j]
            trend_up = (not self.cfg.use_trend_filter) or fast[j] > slow[j]
            macd_green = True
            if self.cfg.use_macd_filter:
                rising = macd[j] > macd[j - 1] > macd[j - 2] > macd[j - 3]
                falling = macd[j] < macd[j - 1] < macd[j - 2] < macd[j - 3]
                # gist: macd>0 ? (rising ? green : red) : (falling ? red : green)
                macd_green = rising if macd[j] > 0 else (not falling)
            return zema_up and ha_green and trend_up and macd_green

        def put_at(j: int) -> bool:
            if z[j] is None or z[j - 1] is None or fast[j] is None or slow[j] is None:
                return False
            if macd[j] is None or macd[j - 1] is None or macd[j - 2] is None or macd[j - 3] is None:
                return False
            zema_dn = z[j] < z[j - 1]
            ha_red = ha_o[j] > ha_c[j]
            trend_dn = (not self.cfg.use_trend_filter) or fast[j] < slow[j]
            macd_red = True
            if self.cfg.use_macd_filter:
                rising = macd[j] > macd[j - 1] > macd[j - 2] > macd[j - 3]
                falling = macd[j] < macd[j - 1] < macd[j - 2] < macd[j - 3]
                macd_red = (not rising) if macd[j] > 0 else falling
            return zema_dn and ha_red and trend_dn and macd_red

        if call_at(idx) and not call_at(idx - 1):
            score, reasons = self._score("BUY", idx, ha_o, ha_c, ha_h, ha_l, z, fast, slow, macd, atr)
            return "BUY", score, reasons
        if put_at(idx) and not put_at(idx - 1):
            score, reasons = self._score("SELL", idx, ha_o, ha_c, ha_h, ha_l, z, fast, slow, macd, atr)
            return "SELL", score, reasons
        return "NO_TRADE", 0, []

    def _live_allowed(self) -> bool:
        """True only on a demo account, or on a real account the user confirmed."""
        info = mt5.account_info()
        if info is None:
            print("[SAFETY] Cannot read account info; refusing live order.")
            return False
        demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        if int(info.trade_mode) == int(demo_mode):
            return True
        if self.cfg.live_confirmed:
            return True
        print("[SAFETY] Account is NOT a demo account and live_confirmed is False; "
              "refusing live order. Set live_confirmed=True only when you mean live.")
        return False

    def _send_order(self, side: Signal) -> None:
        if side not in ("BUY", "SELL"):
            return
        if not self._live_allowed():
            return
        tick = mt5.symbol_info_tick(self.cfg.symbol)
        symbol = mt5.symbol_info(self.cfg.symbol)
        if tick is None or symbol is None:
            return
        lots, actual_risk = self._calc_lot()
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side == "BUY" else tick.bid
        point = symbol.point
        ppp = self._points_per_pip()
        if side == "BUY":
            sl = price - self.cfg.sl_pips * ppp * point
            tp = price + self.cfg.tp_pips * ppp * point
        else:
            sl = price + self.cfg.sl_pips * ppp * point
            tp = price - self.cfg.tp_pips * ppp * point
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.cfg.symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
            "comment": "HAZEMA_MT5_BOT",
        }
        result = mt5.order_send(request)
        print(f"[ORDER] {side} {lots} lots -> retcode={getattr(result, 'retcode', None)} "
              f"(risk ${actual_risk:.2f})")

    def run(self) -> None:
        print(f"[RUN] HAZEMA {self.cfg.symbol} M1 | risk ${self.cfg.risk_dollars:.2f}/trade | "
              f"min strength {self.cfg.min_strength} | live={self.cfg.execute_live}")
        while True:
            try:
                if self._daily_stop_hit():
                    time.sleep(self.cfg.poll_seconds)
                    continue
                if not self._spread_ok():
                    time.sleep(self.cfg.poll_seconds)
                    continue
                rates = self._get_rates()
                if len(rates) < 100:
                    time.sleep(self.cfg.poll_seconds)
                    continue
                closed_time = rates[-2]["time"]
                if closed_time == self.last_closed_time:
                    time.sleep(self.cfg.poll_seconds)
                    continue
                self.last_closed_time = closed_time
                signal, score, reasons = self.generate_signal(rates)
                now = datetime.now().isoformat(timespec="seconds")
                if signal in ("BUY", "SELL"):
                    print(f"[SIGNAL] {now} -> {signal} strength {score}/100 :: {'; '.join(reasons)}")
                    if score < self.cfg.min_strength:
                        print(f"[SKIP] Strength {score} < {self.cfg.min_strength}; no trade.")
                        time.sleep(self.cfg.poll_seconds)
                        continue
                    if self._has_position():
                        print("[SKIP] Position already open.")
                    elif self.cfg.execute_live:
                        self._send_order(signal)
                    else:
                        print(f"[PAPER] Would place {signal} {self.cfg.symbol}")
                else:
                    print(f"[SIGNAL] {now} -> NO_TRADE")
                time.sleep(self.cfg.poll_seconds)
            except KeyboardInterrupt:
                print("[EXIT] Stopped by user.")
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                time.sleep(self.cfg.poll_seconds)


if __name__ == "__main__":
    config = BotConfig(
        symbol="EURUSD",
        execute_live=True,   # Automatic trading ON: places orders on signals.
        # live_confirmed stays False: the bot trades DEMO accounts only
        # and refuses a real account. Set live_confirmed=True yourself
        # if you ever mean live money.
    )
    bot = HAZEMAMT5Bot(config)
    bot.connect()
    try:
        bot.run()
    finally:
        bot.shutdown()
