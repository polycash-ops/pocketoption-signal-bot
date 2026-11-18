from __future__ import annotations

"""
EMA20 Heiken Ashi MT5 Python bot — M5 trend scalper.

USER SPEC (2026-11-18):
  - Heiken Ashi chart, 5-minute bars (M5)
  - BUY  when price (HA close) closes ABOVE EMA 20 AND the HA bar is green
  - SELL when price (HA close) closes BELOW EMA 20 AND the HA bar is red
  - FIRST CLOSE ONLY (user update 2026-11-18 ~15:49 EST): the trade fires
    only on the FIRST close that satisfies the condition — if the previous
    closed bar already satisfied the same side, no new signal. All other
    parameters unchanged (TP 5, SL 10, 20,000 units, M5, HA, demo).
  - TP 5 pips, SL 10 pips
  - Size: 20,000 units fixed = 0.20 lots
  - EUR/USD, DEMO account, MT5, automatic trading ON

Signal is evaluated on the last CLOSED bar only (the forming bar is
never traded). It fires ONLY on the FIRST close that satisfies the
condition: BUY when this closed bar qualifies and the previous closed
bar did not qualify for BUY; SELL likewise. While the condition
persists across bars, no new signal fires — so after a TP/SL exit it
does NOT re-enter until a fresh first close appears. One position
at a time.

WORKS WITH ANY MT5 BROKER: the bot talks to the MT5 terminal running
on the Windows machine and trades whichever account is logged in there.
To switch brokers, log into the other broker's account in MT5 and run
the bot again — no code change. Two things that CAN differ per broker:
  - Symbol name: some brokers call it EURUSD.pro / EURUSDm / EURUSD+
    instead of EURUSD. Set `symbol` in BotConfig to the broker's name.
  - 4-digit vs 5-digit pricing: handled automatically — SL/TP/spread
    are stored in PIPS and converted from the broker's own digits.
  - If two brokers' terminals are installed, set `terminal_path` to
    the .exe of the terminal you want.

Size math (EURUSD, 0.20 lots = $2.00/pip):
  SL 10 pips -> $20.00 max loss per trade
  TP  5 pips -> $10.00 max win per trade
  That is 0.5R: breakeven win rate is 66.7% BEFORE spread.
  With a ~1.5-pip spread the net win is ~3.5 pips ($7) and the net
  loss ~11.5 pips ($23), so breakeven is ~76.7%. That is a high bar.
  The spec is built exactly as asked; the math is stated, not hidden.

Defaults: EURUSD, M5, 20,000 units, SL 10 pips, TP 5 pips,
max spread 2.0 pips, one position at a time, daily +/-2% stops.
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
UNITS_PER_LOT = 100_000


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
    timeframe = mt5.TIMEFRAME_M5 if mt5 else 5
    candles_needed: int = 300
    terminal_path: str = ""      # leave empty to use the running terminal;
    # set to the broker terminal's .exe path if several are installed
    units: float = 20_000.0      # fixed size: 20,000 units = 0.20 lots
    ema_period: int = 20         # EMA 20 on the Heiken Ashi close
    sl_pips: float = 10.0        # 10 pips (converted to points per broker)
    tp_pips: float = 5.0         # 5 pips (0.5R vs the 10-pip SL)
    max_spread_pips: float = 2.0  # 2.0 pips — spread eats a 5-pip TP fast
    max_daily_loss_pct: float = 2.0
    max_daily_profit_pct: float = 2.0
    poll_seconds: int = 5
    execute_live: bool = True    # True = place real orders when a signal fires
    live_confirmed: bool = False  # Must be True to trade on a REAL account.
    # Safety: live orders are refused on a real account unless
    # live_confirmed is True. Demo accounts always trade when
    # execute_live is True.


class EMA20MT5Bot:
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
        """Fixed size from units: 20,000 units = 0.20 lots.

        Returns (lots, expected_risk_dollars). Clamped to the broker's
        min/max lot and rounded to its volume step; if the clamp changes
        the size, the reported risk is the ACTUAL risk, not the target.
        """
        symbol = mt5.symbol_info(self.cfg.symbol)
        if symbol is None:
            return round(self.cfg.units / UNITS_PER_LOT, 2), 0.0
        lots = self.cfg.units / UNITS_PER_LOT
        step = float(symbol.volume_step) or 0.01
        lots = max(float(symbol.volume_min) or 0.01, round(lots / step) * step)
        lots = min(lots, float(symbol.volume_max) or lots)
        lots = round(lots, 2)
        risk = lots * self.cfg.sl_pips * self._pip_value_per_lot()
        return lots, round(risk, 2)

    def generate_signal(self, rates: List[dict]) -> tuple:
        """EMA20 Heiken Ashi signal on the last CLOSED bar — FIRST close only.

        BUY  when HA close > EMA20(HA close) AND HA bar green (close > open)
        SELL when HA close < EMA20(HA close) AND HA bar red   (close < open)
        A side fires only when this closed bar qualifies AND the previous
        closed bar did not qualify for the same side.
        Returns (signal, ema20, ha_close, ha_open) for logging.
        """
        if len(rates) < 50:
            return "NO_TRADE", None, None, None
        ha_o, ha_c = heiken_ashi(rates)
        e20 = ema(ha_c, self.cfg.ema_period)
        # last closed bar is index -2 (index -1 is the forming bar)
        idx = len(ha_c) - 2
        if e20[idx] is None or e20[idx - 1] is None:
            return "NO_TRADE", None, None, None

        def buy_at(j: int) -> bool:
            return (e20[j] is not None and ha_c[j] > e20[j]
                    and ha_c[j] > ha_o[j])

        def sell_at(j: int) -> bool:
            return (e20[j] is not None and ha_c[j] < e20[j]
                    and ha_c[j] < ha_o[j])

        if buy_at(idx) and not buy_at(idx - 1):
            return "BUY", e20[idx], ha_c[idx], ha_o[idx]
        if sell_at(idx) and not sell_at(idx - 1):
            return "SELL", e20[idx], ha_c[idx], ha_o[idx]
        return "NO_TRADE", e20[idx], ha_c[idx], ha_o[idx]

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
        lots, risk = self._calc_lot()
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
            "comment": "EMA20_M5_BOT",
        }
        result = mt5.order_send(request)
        print(f"[ORDER] {side} {lots} lots ({int(self.cfg.units):,} units) "
              f"-> retcode={getattr(result, 'retcode', None)} (risk ${risk:.2f})")

    def run(self) -> None:
        print(f"[RUN] EMA20-HA {self.cfg.symbol} M5 | {int(self.cfg.units):,} units "
              f"| SL {self.cfg.sl_pips:.0f} / TP {self.cfg.tp_pips:.0f} pips "
              f"| live={self.cfg.execute_live}")
        while True:
            try:
                if self._daily_stop_hit():
                    time.sleep(self.cfg.poll_seconds)
                    continue
                if not self._spread_ok():
                    time.sleep(self.cfg.poll_seconds)
                    continue
                rates = self._get_rates()
                if len(rates) < 50:
                    time.sleep(self.cfg.poll_seconds)
                    continue
                closed_time = rates[-2]["time"]
                if closed_time == self.last_closed_time:
                    time.sleep(self.cfg.poll_seconds)
                    continue
                self.last_closed_time = closed_time
                signal, e20, hc, ho = self.generate_signal(rates)
                now = datetime.now().isoformat(timespec="seconds")
                if signal in ("BUY", "SELL"):
                    print(f"[SIGNAL] {now} -> {signal} | HA close {hc:.5f} "
                          f"vs EMA20 {e20:.5f} | {'green' if hc > ho else 'red'} bar")
                    if self._has_position():
                        print("[SKIP] Position already open.")
                    elif self.cfg.execute_live:
                        self._send_order(signal)
                    else:
                        print(f"[PAPER] Would place {signal} {self.cfg.symbol}")
                else:
                    print(f"[SIGNAL] {now} -> NO_TRADE | HA close {hc:.5f} "
                          f"vs EMA20 {e20:.5f}")
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
    bot = EMA20MT5Bot(config)
    bot.connect()
    try:
        bot.run()
    finally:
        bot.shutdown()
