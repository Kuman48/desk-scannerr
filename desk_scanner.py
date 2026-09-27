def run_once(seen):
    coins = fetch_pumpfun_new_coins()
    new_candidates = []
    for coin in coins:
        mint = coin.get("mint")
        if not mint or mint in seen:
            continue
        result = evaluate_candidate(coin)
        if result is None:
            continue
        new_candidates.append(result)
        seen.add(mint)
        mark_seen(mint)

    for c in new_candidates:
        msg = format_message(c)
        print(msg)
        print("-" * 50)
        send_telegram(msg)

    if not new_candidates:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Yeni aday yok. (Toplam çekilen coin: {len(coins)})")


def main():
    print("DESK Scanner v2 başladı. Ctrl+C ile durdur.")
    seen = load_seen()
    while True:
        run_once(seen)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
