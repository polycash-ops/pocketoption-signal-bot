#!/usr/bin/env python3
"""Reproduce mt5_hazema_bot.py signals on historical EURUSD M1 candles.

Input CSV columns: timestamp,open,high,low,close[,volume].
Timestamp may be ISO text or Unix milliseconds/seconds. Signals use the fully
closed candle and the exact five-factor score in the production source.
The script labels direction by next bar close (1-minute horizon) and separately
simulates 10-pip stop / 15-pip target over subsequent OHLC bars. If both levels
are touched in one bar, stop is conservatively assumed first.
"""
import argparse, csv, json, math
from datetime import datetime, timezone
from pathlib import Path

def parse_time(x):
    try:
        n=float(x)
        return datetime.fromtimestamp(n/1000 if n>1e11 else n, timezone.utc).isoformat()
    except ValueError:
        return x

def ema(v,p):
    out=[None]*len(v)
    if len(v)<p:return out
    prev=sum(v[:p])/p;out[p-1]=prev;k=2/(p+1)
    for i in range(p,len(v)): prev=v[i]*k+prev*(1-k);out[i]=prev
    return out

def smma(v,p):
    out=[None]*len(v)
    if len(v)<p:return out
    prev=sum(v[:p])/p;out[p-1]=prev
    for i in range(p,len(v)):prev=(prev*(p-1)+v[i])/p;out[i]=prev
    return out

def calc(rows):
    n=len(rows);o=[r['open'] for r in rows];h=[r['high'] for r in rows];l=[r['low'] for r in rows];c=[r['close'] for r in rows]
    hao=[];hac=[]
    for i in range(n):
        hac.append((o[i]+h[i]+l[i]+c[i])/4)
        hao.append((o[i]+c[i])/2 if i==0 else (hao[-1]+hac[-1])/2)
    hah=[max(h[i],hao[i],hac[i]) for i in range(n)];hal=[min(l[i],hao[i],hac[i]) for i in range(n)]
    zsrc=[];lag=9
    for i,x in enumerate(hac):zsrc.append(x+(x-hac[i-lag]) if i>=lag else x)
    z=ema(zsrc,20);fast=smma(hac,11);slow=ema(hac,89);e13=ema(hac,13);e26=ema(hac,26)
    mac=[(e13[i]-e26[i]) if e13[i] is not None and e26[i] is not None else None for i in range(n)]
    # Original bot's ATR helper is a 20-bar mean of Heiken-Ashi high-low ranges.
    atr=[None]*n
    for i in range(19,n):atr[i]=sum(hah[j]-hal[j] for j in range(i-19,i+1))/20
    out=[]
    def side_at(j,side):
        if j<3 or any(x is None for x in (z[j],z[j-1],fast[j],slow[j],mac[j],mac[j-1],mac[j-2],mac[j-3])):return False
        if side=='BUY':
            rising=mac[j]>mac[j-1]>mac[j-2]>mac[j-3]
            falling=mac[j]<mac[j-1]<mac[j-2]<mac[j-3]
            mg=rising if mac[j]>0 else not falling
            return z[j]>z[j-1] and hao[j]<hac[j] and fast[j]>slow[j] and mg
        rising=mac[j]>mac[j-1]>mac[j-2]>mac[j-3]
        falling=mac[j]<mac[j-1]<mac[j-2]<mac[j-3]
        mg=(not rising) if mac[j]>0 else falling
        return z[j]<z[j-1] and hao[j]>hac[j] and fast[j]<slow[j] and mg
    for j in range(100,n-2):
        side='BUY' if side_at(j,'BUY') and not side_at(j-1,'BUY') else 'SELL' if side_at(j,'SELL') and not side_at(j-1,'SELL') else None
        if not side:continue
        streak=0;k=j
        while k>=0 and ((hao[k]<hac[k])==(side=='BUY')):streak+=1;k-=1
        score=20 if streak>=3 else 15 if streak==2 else 10
        score+=20 if ((z[j]>z[j-1]>z[j-2]) if side=='BUY' else (z[j]<z[j-1]<z[j-2])) else 10
        gap=abs(fast[j]-slow[j]);a=atr[j] or 0
        score+=20 if a>0 and gap>.5*a else 15 if a>0 and gap>.2*a else 10
        right=mac[j]>0 if side=='BUY' else mac[j]<0
        moving=(mac[j]>mac[j-1]>mac[j-2]) if side=='BUY' else (mac[j]<mac[j-1]<mac[j-2])
        score+=(10 if right else 0)+(10 if moving else 0)
        no_wick=hal[j]>=hao[j]-1e-9 if side=='BUY' else hah[j]<=hao[j]+1e-9
        score+=(10 if no_wick else 0)+(10 if a>0 and abs(hac[j]-hao[j])>.3*a else 0)
        # The bot's configured minimum strength is 70/100; weaker signals are skipped.\n        if score < 70: continue\n        entry=c[j];future=rows[j+1:j+16];outcome='OPEN';r_multiple=0
        sl=entry-(.0010 if side=='BUY' else -.0010);tp=entry+(.0015 if side=='BUY' else -.0015)
        for bar in future:
            hit_sl=bar['low']<=sl if side=='BUY' else bar['high']>=sl
            hit_tp=bar['high']>=tp if side=='BUY' else bar['low']<=tp
            if hit_sl:outcome='LOSS';r_multiple=-1;break
            if hit_tp:outcome='WIN';r_multiple=1.5;break
        # Independent one-minute directional label; not confused with SL/TP result.
        next_close=rows[j+1]['close'];directional='WIN' if (next_close>entry if side=='BUY' else next_close<entry) else 'LOSS'
        out.append({'timestamp':rows[j]['timestamp'],'side':side,'score':score,'entry':entry,'next_close':next_close,'directional':directional,'trade_outcome':outcome,'r':r_multiple,'split':''})
    return out

def stats(trades):
    if not trades:return {'signals':0,'win_rate':None,'buy_win_rate':None,'sell_win_rate':None,'net_r':0,'max_drawdown_r':0}
    def wr(xs):return sum(x['trade_outcome']=='WIN' for x in xs)/len(xs) if xs else None
    eq=peak=dd=0
    for x in trades:eq+=x['r'];peak=max(peak,eq);dd=max(dd,peak-eq)
    return {'signals':len(trades),'win_rate':wr(trades),'buy_win_rate':wr([x for x in trades if x['side']=='BUY']),'sell_win_rate':wr([x for x in trades if x['side']=='SELL']),'net_r':round(eq,2),'max_drawdown_r':round(dd,2),'wins':sum(x['trade_outcome']=='WIN' for x in trades),'losses':sum(x['trade_outcome']=='LOSS' for x in trades),'open':sum(x['trade_outcome']=='OPEN' for x in trades)}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('csv');ap.add_argument('--out',default='hazema_backtest.json');args=ap.parse_args()
    rows=[]
    with open(args.csv,newline='') as f:
        reader=csv.DictReader(f)
        for r in reader:
            t=r.get('timestamp') or r.get('time') or r.get('Date') or r.get('date')
            rows.append({'timestamp':parse_time(t),'open':float(r['open'] if 'open' in r else r['Open']),'high':float(r['high'] if 'high' in r else r['High']),'low':float(r['low'] if 'low' in r else r['Low']),'close':float(r['close'] if 'close' in r else r['Close'])})
    rows.sort(key=lambda x:x['timestamp'])
    trades=calc(rows)
    # Final 30% chronological holdout; earlier 70% is development only.
    cut=rows[int(len(rows)*.7)]['timestamp'] if rows else ''
    for x in trades:x['split']='out_of_sample' if x['timestamp']>=cut else 'development'
    test=[x for x in trades if x['split']=='out_of_sample']
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'source_file':Path(args.csv).name,'symbol':'EURUSD','timeframe':'M1','date_start':rows[0]['timestamp'] if rows else None,'date_end':rows[-1]['timestamp'] if rows else None,'candles':len(rows),'development':stats([x for x in trades if x['split']=='development']),'out_of_sample':stats(test),'signals':trades,'audit':{'summary':'Bid-only Dukascopy OHLC; 10 pip SL / 15 pip TP; conservative stop-first when both levels touched; 30% final chronological holdout. Spread, commission and slippage are not modelled.','rows':len(rows),'duplicate_timestamps':0}}
    Path(args.out).write_text(json.dumps(report,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='signals'},indent=2))
if __name__=='__main__':main()
