//+------------------------------------------------------------------+
//|                                             HAZEMA_EURUSD_EA.mq5 |
//|  Heiken Ashi Zero-Lag EMA (HAZEMA v1.2 by JustUncleL) ported to   |
//|  MQL5 for automatic trading on MT5.                               |
//|                                                                    |
//|  DEMO ACCOUNT ONLY unless you deliberately attach it to a live     |
//|  account. It trades whatever account is logged in.                 |
//|                                                                    |
//|  Logic (faithful port of the PineScript v1.2 gist):                |
//|   - All calculations on Heiken Ashi candles.                       |
//|   - ZEMA 20: lag=(20-1)/2=9, data = close + (close - close[lag]),  |
//|     then EMA 20 of that series.                                    |
//|   - Trend filter: Smoothed MA 11 vs EMA 89 on HA close.            |
//|   - MACD 13/26/9 on HA close; histogram green/red by the gist's    |
//|     rising/falling-over-3-bars rule.                               |
//|   - CALL on the FIRST bar where: ZEMA rising + green HA candle +   |
//|     SMMA11 > EMA89 + MACD green.                                   |
//|   - PUT on the FIRST bar where: ZEMA falling + red HA candle +     |
//|     SMMA11 < EMA89 + MACD red.                                     |
//|   - Tuned for 1 / 5 / 15 min; expiry 3-6 candles in binary terms. |
//|     On MT5 this EA holds a spot position with SL/TP instead.       |
//|  UNVALIDATED: no backtest published for this logic. Demo first.    |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"
#property description "HAZEMA: Heiken Ashi + Zero Lag EMA 20 + SMMA 11 / EMA 89 + MACD 13/26/9. EURUSD, MT5 demo."

#include <Trade/Trade.mqh>

input string InpSymbol = "EURUSD";
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_M5;
input double InpLotSize = 0.01;

input int InpSLPoints = 300;           // 30 pips on 5-digit symbols
input int InpTPPoints = 450;           // 45 pips on 5-digit symbols
input int InpMaxSpreadPoints = 25;     // 2.5 pips on 5-digit symbols
input bool InpOnePositionAtATime = true;
input ulong InpMagic = 20260402;

input bool InpUseTrendFilter = true;   // SMMA 11 vs EMA 89
input bool InpUseMACDFilter = true;    // MACD 13/26/9 histogram colour

input bool InpUseDailyProfitStop = true;
input bool InpUseDailyLossStop = true;
input double InpDailyProfitPercent = 2.0; // stop trading for day once reached
input double InpDailyLossPercent = 2.0;   // stop trading for day once reached

CTrade trade;

datetime lastBarTime = 0;
datetime dayAnchor = 0;
double dayStartBalance = 0.0;

#define BARS 300

//--- standard EMA, seeded with the SMA of the first <period> values
void CalcEMA(const double &src[], int period, double &out[])
{
   ArrayResize(out, BARS);
   double k = 2.0 / (period + 1);
   double sum = 0.0;
   for(int i = 0; i < period; i++)
      sum += src[i];
   out[period - 1] = sum / period;
   for(int i = 0; i < period - 1; i++)
      out[i] = EMPTY_VALUE;
   for(int i = period; i < BARS; i++)
      out[i] = src[i] * k + out[i - 1] * (1.0 - k);
}

//--- Wilder smoothed MA: SMMA[i] = (SMMA[i-1]*(n-1) + src[i]) / n
void CalcSMMA(const double &src[], int period, double &out[])
{
   ArrayResize(out, BARS);
   double sum = 0.0;
   for(int i = 0; i < period; i++)
      sum += src[i];
   out[period - 1] = sum / period;
   for(int i = 0; i < period - 1; i++)
      out[i] = EMPTY_VALUE;
   for(int i = period; i < BARS; i++)
      out[i] = (out[i - 1] * (period - 1) + src[i]) / period;
}

//--- Zero Lag EMA: data[i] = src[i] + (src[i] - src[i-lag]), then EMA
void CalcZEMA(const double &src[], int period, double &out[])
{
   int lag = (period - 1) / 2;
   double data[];
   ArrayResize(data, BARS);
   for(int i = 0; i < BARS; i++)
      data[i] = (i >= lag) ? (src[i] + (src[i] - src[i - lag])) : src[i];
   CalcEMA(data, period, out);
}

bool IsNewBar()
{
   datetime t = iTime(InpSymbol, InpTimeframe, 0);
   if(t != lastBarTime)
   {
      lastBarTime = t;
      return true;
   }
   return false;
}

void ResetDayAnchorIfNeeded()
{
   MqlDateTime nowStruct;
   TimeToStruct(TimeCurrent(), nowStruct);
   nowStruct.hour = 0;
   nowStruct.min = 0;
   nowStruct.sec = 0;
   datetime todayMidnight = StructToTime(nowStruct);
   if(dayAnchor != todayMidnight)
   {
      dayAnchor = todayMidnight;
      dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      Print("New day anchor set. Start balance: ", DoubleToString(dayStartBalance, 2));
   }
}

bool DailyStopReached()
{
   ResetDayAnchorIfNeeded();
   if(dayStartBalance <= 0.0)
      return false;
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double pnlPct = ((balance - dayStartBalance) / dayStartBalance) * 100.0;
   if(InpUseDailyProfitStop && pnlPct >= InpDailyProfitPercent)
   {
      Print("Daily profit stop reached: ", DoubleToString(pnlPct, 2), "%");
      return true;
   }
   if(InpUseDailyLossStop && pnlPct <= -InpDailyLossPercent)
   {
      Print("Daily loss stop reached: ", DoubleToString(pnlPct, 2), "%");
      return true;
   }
   return false;
}

bool HasOpenPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == InpSymbol &&
         PositionGetInteger(POSITION_MAGIC) == (long)InpMagic)
         return true;
   }
   return false;
}

//--- evaluate the HAZEMA condition at bar index <idx> (chronological arrays)
bool EvalCall(int idx, const double &haO[], const double &haC[],
              const double &zema[], const double &smma11[], const double &ema89[],
              const double &macdLine[])
{
   bool zemaUp = zema[idx] > zema[idx - 1];
   bool haGreen = haO[idx] < haC[idx];
   bool trendUp = !InpUseTrendFilter || smma11[idx] > ema89[idx];
   bool macdGreen = true;
   if(InpUseMACDFilter)
   {
      bool rising = macdLine[idx] > macdLine[idx - 1] &&
                    macdLine[idx - 1] > macdLine[idx - 2] &&
                    macdLine[idx - 2] > macdLine[idx - 3];
      bool falling = macdLine[idx] < macdLine[idx - 1] &&
                     macdLine[idx - 1] < macdLine[idx - 2] &&
                     macdLine[idx - 2] < macdLine[idx - 3];
      // gist: macd>0 ? (rising ? green : red) : (falling ? red : green)
      macdGreen = (macdLine[idx] > 0) ? rising : !falling;
   }
   return zemaUp && haGreen && trendUp && macdGreen;
}

bool EvalPut(int idx, const double &haO[], const double &haC[],
             const double &zema[], const double &smma11[], const double &ema89[],
             const double &macdLine[])
{
   bool zemaDn = zema[idx] < zema[idx - 1];
   bool haRed = haO[idx] > haC[idx];
   bool trendDn = !InpUseTrendFilter || smma11[idx] < ema89[idx];
   bool macdRed = true;
   if(InpUseMACDFilter)
   {
      bool rising = macdLine[idx] > macdLine[idx - 1] &&
                    macdLine[idx - 1] > macdLine[idx - 2] &&
                    macdLine[idx - 2] > macdLine[idx - 3];
      bool falling = macdLine[idx] < macdLine[idx - 1] &&
                     macdLine[idx - 1] < macdLine[idx - 2] &&
                     macdLine[idx - 2] < macdLine[idx - 3];
      macdRed = (macdLine[idx] > 0) ? !rising : falling;
   }
   return zemaDn && haRed && trendDn && macdRed;
}

void OnTick()
{
   if(!IsNewBar())
      return;
   if(DailyStopReached())
      return;
   if(InpOnePositionAtATime && HasOpenPosition())
      return;

   long spread = SymbolInfoInteger(InpSymbol, SYMBOL_SPREAD);
   if(spread > InpMaxSpreadPoints)
   {
      Print("Spread too high: ", spread, " points, max ", InpMaxSpreadPoints);
      return;
   }

   MqlRates rates[];
   if(CopyRates(InpSymbol, InpTimeframe, 0, BARS, rates) != BARS)
   {
      Print("CopyRates failed: ", GetLastError());
      return;
   }
   // CopyRates returns chronological order: rates[0] oldest, rates[BARS-1] forming bar.
   // Last closed bar is BARS-2.

   double haO[], haH[], haL[], haC[];
   ArrayResize(haO, BARS);
   ArrayResize(haH, BARS);
   ArrayResize(haL, BARS);
   ArrayResize(haC, BARS);
   for(int i = 0; i < BARS; i++)
   {
      haC[i] = (rates[i].open + rates[i].high + rates[i].low + rates[i].close) / 4.0;
      haO[i] = (i == 0) ? (rates[i].open + rates[i].close) / 2.0
                        : (haO[i - 1] + haC[i - 1]) / 2.0;
      haH[i] = MathMax(rates[i].high, MathMax(haO[i], haC[i]));
      haL[i] = MathMin(rates[i].low, MathMin(haO[i], haC[i]));
   }

   double ema13[], ema26[], ema89[], smma11[], zema20[];
   CalcEMA(haC, 13, ema13);
   CalcEMA(haC, 26, ema26);
   CalcEMA(haC, 89, ema89);
   CalcSMMA(haC, 11, smma11);
   CalcZEMA(haC, 20, zema20);

   double macdLine[];
   ArrayResize(macdLine, BARS);
   for(int i = 0; i < BARS; i++)
      macdLine[i] = ema13[i] - ema26[i];

   int idx = BARS - 2; // last closed bar
   bool callNow = EvalCall(idx, haO, haC, zema20, smma11, ema89, macdLine);
   bool callPrev = EvalCall(idx - 1, haO, haC, zema20, smma11, ema89, macdLine);
   bool putNow = EvalPut(idx, haO, haC, zema20, smma11, ema89, macdLine);
   bool putPrev = EvalPut(idx - 1, haO, haC, zema20, smma11, ema89, macdLine);

   double point = SymbolInfoDouble(InpSymbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(InpSymbol, SYMBOL_DIGITS);
   double ask = SymbolInfoDouble(InpSymbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(InpSymbol, SYMBOL_BID);

   trade.SetExpertMagicNumber(InpMagic);

   if(callNow && !callPrev)
   {
      double sl = NormalizeDouble(ask - InpSLPoints * point, digits);
      double tp = NormalizeDouble(ask + InpTPPoints * point, digits);
      Print("HAZEMA CALL signal, ZEMA ", DoubleToString(zema20[idx], digits),
            " HA ", DoubleToString(haO[idx], digits), " -> ", DoubleToString(haC[idx], digits));
      if(!trade.Buy(InpLotSize, InpSymbol, ask, sl, tp, "HAZEMA CALL"))
         Print("Buy failed: ", trade.ResultRetcodeDescription());
   }
   else if(putNow && !putPrev)
   {
      double sl = NormalizeDouble(bid + InpSLPoints * point, digits);
      double tp = NormalizeDouble(bid - InpTPPoints * point, digits);
      Print("HAZEMA PUT signal, ZEMA ", DoubleToString(zema20[idx], digits),
            " HA ", DoubleToString(haO[idx], digits), " -> ", DoubleToString(haC[idx], digits));
      if(!trade.Sell(InpLotSize, InpSymbol, bid, sl, tp, "HAZEMA PUT"))
         Print("Sell failed: ", trade.ResultRetcodeDescription());
   }
}
//+------------------------------------------------------------------+
