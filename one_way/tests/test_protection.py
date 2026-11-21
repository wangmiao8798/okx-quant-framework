from one_way.protection import *

def plan(direction='long'):
    return EntryPlan('t1','BTC-USDT-SWAP',direction,100,0.4,98,104,0.8,40)

def test_long_plan_and_protection_requires_both_orders():
    p=plan(); assert validate_plan(p,max_loss_usdt=1,max_notional_usdt=50)==[]
    s=after_entry_fill(p,.4); assert s.state==ProtectionState.ENTRY_FILLED_UNPROTECTED
    s=apply_tp(s,'tp1',.4); assert s.state==ProtectionState.TP_SUBMITTED
    s=apply_sl(s,'sl1',.4); assert s.state==ProtectionState.POSITION_PROTECTED
    assert can_open_new(s)

def test_short_price_direction_and_fail_closed():
    p=plan('short'); assert 'price_direction_invalid' in validate_plan(p,max_loss_usdt=1,max_notional_usdt=50)
    s=after_entry_fill(plan(),.4); s=apply_tp(s,'',.4)
    assert s.state==ProtectionState.FAIL_CLOSED
    assert not can_open_new(s)

def test_partial_protection_does_not_unlock():
    p=plan(); s=apply_tp(after_entry_fill(p,.4),'tp1',.2)
    assert s.state==ProtectionState.FAIL_CLOSED
    assert not can_open_new(s)

def test_limits_and_no_averaging_contract():
    p=plan(); assert 'max_loss_exceeded' in validate_plan(p,max_loss_usdt=.5,max_notional_usdt=50)
    assert 'max_notional_exceeded' in validate_plan(p,max_loss_usdt=1,max_notional_usdt=30)
