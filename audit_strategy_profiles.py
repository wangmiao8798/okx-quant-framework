import json
from pathlib import Path

import yaml

from api.okx_client import OKXClient
from strategies.futures_hedge import FuturesHedgeStrategy


PROJ = Path(__file__).resolve().parent


def planned_side_margin(futures: dict) -> float:
    base = float(futures.get("base_position_usdt", 0) or 0)
    layers = int(futures.get("max_add_layers", 0) or 0)
    multiplier = float(futures.get("add_multiplier", 1) or 1)
    additions = sum(1 + (multiplier - 1) * index for index in range(layers))
    return base * (1 + additions)


def main():
    config_dir = PROJ / "config"
    paths = [config_dir / "config.yaml"]
    paths.extend(sorted(config_dir.glob("config_*.yaml")))
    paths.extend(sorted((config_dir / "standby").glob("config_*.yaml")))
    client = OKXClient({"api_key": "", "api_secret": "", "passphrase": ""}, flag="0")
    markets = {}
    results = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        futures = config.get("futures", {})
        symbol = str(futures.get("symbol", ""))
        if symbol not in markets:
            info = client.get_futures_info(symbol)
            ticker = client.get_ticker(symbol)
            markets[symbol] = (info, ticker)
        info, ticker = markets[symbol]
        warnings = []
        try:
            FuturesHedgeStrategy(config, None)
        except Exception as exc:
            warnings.append(f"strategy_init:{type(exc).__name__}:{exc}")
        leverage = float(futures.get("leverage", 1) or 1)
        minimum_margin = (
            float(info.get("minSz", 0))
            * float(info.get("ctVal", 0))
            * float(ticker.get("last", 0))
            / leverage
        )
        base = float(futures.get("base_position_usdt", 0) or 0)
        planned_margin = planned_side_margin(futures)
        planned_notional = planned_margin * leverage
        side_cap = float(futures.get("max_side_notional_usdt", 0) or 0)
        if base < minimum_margin * 1.2:
            warnings.append("base_below_1.2x_minimum_margin")
        if not 0 < float(futures.get("grid_bias_ratio", 0) or 0) <= 0.95:
            warnings.append("invalid_grid_bias_ratio")
        if not 0 < float(futures.get("rebalance_trigger_ratio", 0) or 0) <= 0.95:
            warnings.append("invalid_rebalance_trigger_ratio")
        if side_cap <= 0:
            warnings.append("missing_side_notional_cap")
        elif planned_notional > side_cap:
            warnings.append("planned_notional_exceeds_side_cap")
        if float(futures.get("max_net_notional_usdt", 0) or 0) <= 0:
            warnings.append("missing_net_notional_cap")
        if not bool(futures.get("hard_stop_enabled", False)):
            warnings.append("hard_stop_disabled")
        results.append({
            "file": str(path.relative_to(PROJ)),
            "symbol": symbol,
            "market_ok": bool(info.get("ok")),
            "last": ticker.get("last"),
            "minimum_margin": round(minimum_margin, 4),
            "base_position_usdt": base,
            "planned_side_margin": round(planned_margin, 4),
            "planned_side_notional": round(planned_notional, 4),
            "warnings": warnings,
        })
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
