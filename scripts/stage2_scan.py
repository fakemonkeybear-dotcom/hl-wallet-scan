import requests, time, pandas as pd, numpy as np, os
from datetime import datetime, timezone

API_URL = "https://api.hyperliquid.xyz/info"
WALLETS_FILE = "stage2_wallets.txt"
RESULTS_FILE = "state/stage2_results.csv"
OVERLAP_DATA_FILE = "state/stage2_overlap_data.csv"
TIME_BUDGET_SECONDS = 5 * 60 * 60

def hl_post(body, retries=3):
    """Standard calls (fills, state) - these matter, worth retrying properly."""
    for i in range(retries):
        try:
            r = requests.post(API_URL, json=body, timeout=12)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep(1.5 * (i + 1))
            else:
                time.sleep(0.5 * (i + 1))
        except Exception:
            time.sleep(1 * (i + 1))
    return None

def hl_post_fast(body):
    """For candle lookups only - fail fast, don't burn minutes retrying
    something non-critical. One quick attempt, no backoff."""
    try:
        r = requests.post(API_URL, json=body, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def get_fills(addr):
    return hl_post({"type": "userFills", "user": addr, "aggregateByTime": False}) or []

def get_state(addr):
    return hl_post({"type": "clearinghouseState", "user": addr})

def get_candles(coin, entry_ms, lookahead_hours=3):
    start = entry_ms - 1000*60*30
    end = entry_ms + 1000*60*60*lookahead_hours
    # only try ONE interval, fail fast - a missed candle just means
    # this trade's timing can't be checked, which is fine at this scale
    candles = hl_post_fast({"type": "candleSnapshot", "req": {"coin": coin, "interval": "15m", "startTime": start, "endTime": end}})
    if candles and len(candles) >= 2:
        return candles
    return None

def dedupe_events(df):
    df = df.sort_values("time").copy()
    df["time_bucket"] = df["time"].dt.floor("5s")
    return df.groupby(["coin", "dir", "time_bucket"], as_index=False).agg(
        closedPnl=("closedPnl", "sum"), time=("time", "first"), time_ms=("time_ms", "first"))

def analyze_wallet(addr):
    row = {"wallet": addr}
    overlap_rows = []

    fills = get_fills(addr)
    if not fills:
        return row, overlap_rows

    df = pd.DataFrame(fills)
    df["closedPnl"] = pd.to_numeric(df.get("closedPnl", 0), errors="coerce").fillna(0)
    df["px"] = pd.to_numeric(df.get("px", 0), errors="coerce")
    df["sz"] = pd.to_numeric(df.get("sz", 0), errors="coerce")
    df["notional"] = df["px"] * df["sz"]
    df["time"] = pd.to_datetime(df["time"], unit="ms", errors="coerce")
    df["time_ms"] = df["time"].astype("int64") // 10**6
    df = df.sort_values("time").reset_index(drop=True)

    # liquidation behavior
    if "liquidation" in df.columns:
        liq_rows = df[df["liquidation"].notna()]
        row["num_liquidations"] = len(liq_rows)
        if len(liq_rows):
            first_liq, last_liq = liq_rows["time"].min(), liq_rows["time"].max()
            row["days_since_last_liq"] = round((df["time"].max() - last_liq).total_seconds() / 86400, 1)
            liq_coins = liq_rows["coin"].tolist() if "coin" in liq_rows else []
            row["liq_same_coin_repeat"] = len(liq_coins) - len(set(liq_coins))
            before = df[df["time"] < first_liq]
            after = df[df["time"] > last_liq]
            b_n = before["notional"].mean() if len(before) else None
            a_n = after["notional"].mean() if len(after) else None
            if b_n and a_n:
                row["sizing_change_after_liq_%"] = round(100 * (a_n - b_n) / b_n, 1)
    else:
        row["num_liquidations"] = 0

    # current leverage
    state = get_state(addr)
    if state and state.get("assetPositions"):
        levs = [float(p.get("position", {}).get("leverage", {}).get("value"))
                for p in state["assetPositions"] if p.get("position", {}).get("leverage", {}).get("value")]
        row["current_open_positions"] = len(state["assetPositions"])
        row["current_avg_leverage"] = round(np.mean(levs), 1) if levs else None
        row["current_max_leverage"] = round(max(levs), 1) if levs else None
    else:
        row["current_open_positions"] = 0

    # timing edge - top 3 wins only, to keep this feasible across 1689 wallets
    closed = df[df["closedPnl"] != 0]
    events = dedupe_events(closed)
    top_wins = events.sort_values("closedPnl", ascending=False).head(2)
    checked, favorable, moves = 0, 0, []
    for _, ev in top_wins.iterrows():
        candles = get_candles(ev["coin"], ev["time_ms"])
        if not candles:
            continue
        cdf = pd.DataFrame(candles)
        cdf["c"] = pd.to_numeric(cdf["c"], errors="coerce")
        cdf["t"] = pd.to_numeric(cdf["t"], errors="coerce")
        pre = cdf[cdf["t"] <= ev["time_ms"]]
        post = cdf[cdf["t"] > ev["time_ms"]]
        if pre.empty or post.empty:
            continue
        p0, p1 = pre["c"].iloc[-1], post["c"].iloc[-1]
        if not p0:
            continue
        raw = round(100 * (p1 - p0) / p0, 2)
        fav = -raw if "Short" in str(ev["dir"]) else raw
        checked += 1
        if fav > 0:
            favorable += 1
        moves.append(fav)
    row["win_timing_checked"] = checked
    row["win_timing_pct_favorable"] = round(100 * favorable / checked, 1) if checked else None
    row["win_timing_avg_move_%"] = round(np.mean(moves), 2) if moves else None

    # export top 50 trades (by abs pnl) for the scalable overlap check done separately
    top_for_overlap = closed.reindex(closed["closedPnl"].abs().sort_values(ascending=False).index).head(50)
    for _, t in top_for_overlap.iterrows():
        overlap_rows.append({
            "wallet": addr, "coin": t.get("coin", ""), "dir": t.get("dir", ""),
            "time": t["time"], "time_bucket_1s": t["time"].floor("1s") if pd.notna(t["time"]) else None
        })

    return row, overlap_rows

def load_wallets():
    with open(WALLETS_FILE) as f:
        return [line.strip() for line in f if line.strip()]

def load_already_done():
    if os.path.exists(RESULTS_FILE):
        try:
            existing = pd.read_csv(RESULTS_FILE, usecols=["wallet"])
            return set(existing["wallet"].astype(str))
        except Exception:
            return set()
    return set()

def append_result(row, overlap_rows):
    os.makedirs("state", exist_ok=True)
    file_exists = os.path.exists(RESULTS_FILE)
    pd.DataFrame([row]).to_csv(RESULTS_FILE, mode="a", header=not file_exists, index=False)
    if overlap_rows:
        overlap_exists = os.path.exists(OVERLAP_DATA_FILE)
        pd.DataFrame(overlap_rows).to_csv(OVERLAP_DATA_FILE, mode="a", header=not overlap_exists, index=False)

def main():
    wallets = load_wallets()
    total = len(wallets)
    already_done = load_already_done()
    print(f"Total candidates: {total}. Already done: {len(already_done)}.")
    remaining = [w for w in wallets if w not in already_done]
    print(f"Remaining: {len(remaining)}.")

    start_time = time.time()
    processed = 0
    for addr in remaining:
        if time.time() - start_time > TIME_BUDGET_SECONDS:
            print(f"Time budget reached. Processed {processed} this run.")
            break
        row, overlap_rows = analyze_wallet(addr)
        append_result(row, overlap_rows)
        processed += 1
        if processed % 25 == 0:
            print(f"[{len(already_done)+processed}/{total}] {addr} done. ({processed} this run)")
        time.sleep(0.2)

    final_done = len(already_done) + processed
    print(f"Run complete. Processed {processed} this run. Total done: {final_done}/{total}.")
    if final_done >= total:
        print("*** STAGE 2 COMPLETE ***")

if __name__ == "__main__":
    main()
        
