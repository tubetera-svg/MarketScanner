import sys
sys.path.insert(0, ".")
sys.path.insert(0, "./src")
from datetime import date
from backtest import BacktestConfig, run_backtest

wl = [l.strip() for l in open("config/watchlist.txt").read().splitlines() if l.strip()]
print("watchlist symbols", len(wl))

for strat in ["ema5_sweep", "inside_bar_pattern_daily_sweep", "daily_fvg_sweep"]:
    cfg = BacktestConfig(
        symbols=wl, strategies=[strat],
        start_date=date(2025, 2, 6), end_date=date(2026, 8, 27),
        hold_days=5, initial_capital=100000.0,
    )
    rep = run_backtest(cfg)[strat]
    m = rep.metrics
    wr = round((m["win_rate"] or 0) * 100, 1) if m["win_rate"] is not None else None
    sh = round(m["sharpe"], 2) if m["sharpe"] is not None else None
    pf = round(m["profit_factor"], 2) if m["profit_factor"] is not None else None
    ex = round(m["expectancy"], 1) if m["expectancy"] is not None else None
    cg = round((m["cagr"] or 0) * 100, 1) if m["cagr"] is not None else None
    print(f"{strat:32s} trades={m['num_trades']:4d} win%={str(wr):>5} sharpe={str(sh):>6} pf={str(pf):>6} exp={str(ex):>7} cagr={str(cg):>6} maxDD={round(m['max_drawdown_pct']*100,1)}%")
