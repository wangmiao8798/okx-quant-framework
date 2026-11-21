from pathlib import Path
import importlib.util

MODULE = Path('/opt/okx_quant/scripts/dram_inventory_shadow.py')
spec = importlib.util.spec_from_file_location('dram_inventory_shadow', MODULE)
shadow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shadow)


def state(long_qty=46, short_qty=8, margin=0.6144, max_net_notional=20.0):
    total = long_qty + short_qty
    return {
        'long_qty': long_qty,
        'short_qty': short_qty,
        'margin_load_ratio': margin,
        'imbalance_ratio': abs(long_qty-short_qty)/total if total else 0,
        'mark_price': 1.0,
        'contract_value': 1.0,
        'max_net_notional': max_net_notional,
    }


def test_candidate_proposes_only_weak_side_repair_when_inventory_is_high():
    result = shadow.decide_candidate(state())
    assert result['candidate_action'] == 'repair_short'
    assert result['repair_active'] is True
    assert result['repair_side'] == 'short'
    assert result['suppress_open_long'] is False
    assert result['suppress_open_short'] is False
    assert result['advisory_only'] is True


def test_candidate_repairs_when_imbalance_gate_triggers_without_net_cap_breach():
    # Inventory repair is intentionally triggered by either independent
    # exposure gate: a large side imbalance is actionable even below the
    # absolute net-notional cap.
    assert shadow.decide_candidate(state(max_net_notional=100.0))['candidate_action'] == 'repair_short'
    assert shadow.decide_candidate(state(long_qty=30, short_qty=20, max_net_notional=100.0))['candidate_action'] == 'as_is'


def test_short_dominance_proposes_weak_long_repair_without_suppression():
    result = shadow.decide_candidate(state(long_qty=8, short_qty=46))
    assert result['candidate_action'] == 'repair_long'
    assert result['repair_side'] == 'long'
    assert result['suppress_open_long'] is False


def test_source_is_read_only_and_never_calls_order_mutations():
    source = MODULE.read_text(encoding='utf-8')
    forbidden = ('place_order', 'cancel_order', 'amend_order', 'close_positions', 'set_leverage')
    assert not any(name in source for name in forbidden)
