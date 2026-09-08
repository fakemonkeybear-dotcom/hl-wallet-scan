import requests, time, pandas as pd, numpy as np, json, os, sys
from datetime import datetime, timezone

API_URL = "https://api.hyperliquid.xyz/info"
WALLETS_FILE = "wallets.txt"
RESULTS_FILE = "state/results.csv"
CHECKPOINT_FILE = "state/checkpoint.json"
TIME_BUDGET_SECONDS = 5 * 60 * 60  # stop after 5 hours, leave buffer under GH's 6hr hard cap

def hl_post(body, retries=4):
    for i in range(retries):
        try:
            r = requests.post(API_URL, json=body, timeout=20)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep(3 * (i + 1))
            else:
                time.sleep(1 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None

def get_fills(addr):
    return hl_post({"type": "userFills", "user": addr, "aggregateByTime": False}) or []

def get_state(addr):
    return hl_post({"type": "clearinghouseState", "user": addr})

def get_funding(addr):
    return hl_post({"type": "userFunding", "user": addr, "startTime": 0}) or []

def analyze_wallet(addr):
    row = {"wallet": addr}
    try:
        state = get_state(addr)
        margin = state.get("marginSummary", {}) if state else {}
        row["account_value"] = float(margin.get("accountValue", 0))

        fills = get_fills(addr)
        row["num_fills"] = len(fills)
        if not fills:
            row["total_realized_pnl"] = None
            return row

        df = pd.DataFrame(fills)
        df["closedPnl"] = pd.to_numeric(df.get("closedPnl", 0), errors="coerce").fillna(0)
        df["time"] = pd.to_datetime(df["time"], unit="ms", errors="coerce")

        closed = df[df["closedPnl"] != 0]
        row["total_realized_pnl"] = round(closed["closedPnl"].sum(), 2)
        row["num_closing_trades"] = len(closed)
        row["win_rate_%"] = round(100 * (closed["closedPnl"] > 0).mean(), 1) if len(closed) else None

        if "liquidation" in df.columns:
            row["num_liquidations"] = int(df["liquidation"].notna().sum())
        else:
            row["num_liquidations"] = 0

        row["first_trade"] = df["time"].min()
        row["last_trade"] = df["time"].max()
        span_days = (df["time"].max() - df["time"].min()).total_seconds() / 86400
        row["active_days_span"] = round(span_days, 1)
        row["coins_traded"] = df["coin"].nunique() if "coin" in df else None

        if "crossed" in df.columns:
            row["taker_%"] = round(100 * df["crossed"].mean(), 1)

        funding = get_funding(addr)
        try:
            row["total_funding"] = round(sum(float(f.get("delta", {}).get("usdc", 0)) for f in funding), 2)
        except Exception:
            row["total_funding"] = None
        row["net_pnl_after_funding"] = round(row["total_realized_pnl"] + (row["total_funding"] or 0), 2)

    except Exception as e:
        row["error"] = str(e)
    return row

def load_wallets():
    with open(WALLETS_FILE) as f:
        return [line.strip() for line in f if line.strip()]

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"last_index": -1}

def save_checkpoint(idx):
    os.makedirs("state", exist_ok=True)
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"last_index": idx, "updated": datetime.now(timezone.utc).isoformat()}, f)

def append_result(row):
    os.makedirs("state", exist_ok=True)
    file_exists = os.path.exists(RESULTS_FILE)
    df_row = pd.DataFrame([row])
    df_row.to_csv(RESULTS_FILE, mode="a", header=not file_exists, index=False)

def main():
    wallets = load_wallets()
    checkpoint = load_checkpoint()
    start_idx = checkpoint["last_index"] + 1
    total = len(wallets)

    print(f"Total wallets: {total}. Resuming from index {start_idx}.")

    start_time = time.time()
    processed_this_run = 0

    for idx in range(start_idx, total):
        if time.time() - start_time > TIME_BUDGET_SECONDS:
            print(f"Time budget reached. Processed {processed_this_run} this run. Stopping at index {idx}.")
            break

        addr = wallets[idx]
        row = analyze_wallet(addr)
        append_result(row)
        save_checkpoint(idx)
        processed_this_run += 1

        if idx % 25 == 0:
            print(f"[{idx+1}/{total}] {addr} done. ({processed_this_run} this run)")

        time.sleep(0.15)  # ~6-7 req/sec across the 3 calls per wallet, conservative

    print(f"Run complete. Processed {processed_this_run} wallets this run.")
    if start_idx + processed_this_run >= total:
        print("*** ALL WALLETS COMPLETE ***")

if __name__ == "__main__":
    main()
