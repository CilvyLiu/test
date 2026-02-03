import os
import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timedelta, timezone

# ===================== 0. 环境底座 =====================
TZ_CHINA = timezone(timedelta(hours=8))

def get_now_china():
    return datetime.now(timezone.utc).astimezone(TZ_CHINA)

def is_trading_time():
    now = get_now_china()
    if now.weekday() >= 5: return False
    hm = now.hour * 100 + now.minute
    return (915 <= hm <= 1135) or (1255 <= hm <= 1505)

def init_vault():
    state_keys = {
        "price_history": [],      
        "volume_history": [],     
        "sup_history": [],        # [补丁①] 支撑历史，用于时间一致性
        "prev_vol_cumulative": 0.0, 
        "risk_lock_active": False,
        "lock_timestamp": 0.0,     
        "last_valid_vol": 0.0005,  
        "avg_vol_ema": 0.0,        
        "last_sell_time": 0.0,
        "last_buy_time": 0.0,      # [补丁③] 买入动作钝化记忆
        "break_count": 0           
    }
    for key, val in state_keys.items():
        if key not in st.session_state:
            st.session_state[key] = val

init_vault()

def safe_float(x, default=0.0):
    try:
        if x in ['-', '--', None, '', 'None']: return default
        return float(x)
    except: return default

# ===================== 1. 核心加固工具 =====================

def safe_weighted_avg(df, price_col, vol_col, fallback):
    try:
        p = df[price_col].apply(safe_float).values
        v = df[vol_col].apply(safe_float).values
        v_sum = v.sum()
        return np.average(p, weights=v) if v_sum > 0 else fallback
    except: return fallback

def get_filtered_volatility(prices):
    if len(prices) < 5: return st.session_state.last_valid_vol
    returns = np.diff(np.log(np.array(prices)))
    valid_returns = returns[np.abs(returns) > 1e-6]
    if len(valid_returns) < 3: return st.session_state.last_valid_vol
    curr_vol = np.std(valid_returns)
    st.session_state.last_valid_vol = curr_vol
    return curr_vol

# ===================== 2. 审计内核 v8.4 =====================
def gringotts_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    curr_cum_vol = safe_float(quote['成交量'])
    now_ts = time.time()
    
    # --- A. 数据清洗与量能归一化 ---
    if curr_cum_vol < st.session_state.prev_vol_cumulative:
        st.session_state.prev_vol_cumulative = curr_cum_vol
        tick_vol = 0
    else:
        tick_vol = max(0, curr_cum_vol - st.session_state.prev_vol_cumulative)
    st.session_state.prev_vol_cumulative = curr_cum_vol
    
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-30:]
    
    volatility = get_filtered_volatility(st.session_state.price_history)
    
    # EMA 量能归一化
    alpha = 0.2
    st.session_state.avg_vol_ema = alpha * tick_vol + (1 - alpha) * st.session_state.avg_vol_ema if st.session_state.avg_vol_ema > 0 else tick_vol
    vol_ratio = min(tick_vol / (st.session_state.avg_vol_ema + 1e-9), 10.0)

    # --- B. 支撑一致性确认 [补丁①] ---
    weighted_bid_p = safe_weighted_avg(df_bids, '价格', '数量', fallback=curr_p)
    st.session_state.sup_history.append(weighted_bid_p)
    st.session_state.sup_history = st.session_state.sup_history[-5:] # 5个tick的一致性窗口
    
    # 使用中位数平滑掉瞬时撤单干扰 (防盘口欺骗)
    stable_bid_sup = np.median(st.session_state.sup_history)
    struct_sup = np.percentile(st.session_state.price_history[-20:], 20) if len(st.session_state.price_history) >= 20 else stable_bid_sup
    
    p_sup = min(stable_bid_sup, struct_sup)
    p_res = safe_weighted_avg(df_asks, '价格', '数量', fallback=curr_p)
    
    # --- C. 结构化风控与带量确认 [补丁②] ---
    # 击穿判定：跌破支撑 且 必须有一定量能支撑 (vol_ratio > 0.6)
    if curr_p < p_sup * 0.996 and vol_ratio > 0.6:
        st.session_state.break_count += 1
    else:
        st.session_state.break_count = max(0, st.session_state.break_count - 1)

    lock_trigger = (st.session_state.break_count >= 2) or (volatility > 0.003)
    min_lock_sec = max(10, int(60 * (volatility / 0.002)))
    
    if lock_trigger:
        st.session_state.risk_lock_active = True
        st.session_state.lock_timestamp = now_ts
    else:
        if st.session_state.risk_lock_active and (now_ts - st.session_state.lock_timestamp < min_lock_sec):
            pass 
        else:
            st.session_state.risk_lock_active = False

    # --- D. 对称化评分与买入钝化 [补丁③] ---
    ret_trend = (curr_p / st.session_state.price_history[-5] - 1) if len(st.session_state.price_history) >= 5 else 0
    
    # 卖方评分
    s_score = 0
    if curr_p >= p_res: s_score += 40
    if curr_p >= p_res * (1 + 2.5 * volatility): s_score += 40
    if vol_ratio > 4.0: s_score += 20 
    # 卖出钝化记忆
    if now_ts - st.session_state.last_sell_time < 60: s_score *= 0.6
    if s_score >= 70: st.session_state.last_sell_time = now_ts
    
    # 买方评分
    b_score = 0
    if not st.session_state.risk_lock_active and ret_trend > -0.0005 and volatility < 0.002:
        if abs(curr_p - p_sup)/curr_p < 0.0015: b_score += 40 
        if vol_ratio < 0.8: b_score += 40 
        if s_score < 20: b_score += 20 
    
    # 买入动作钝化 [补丁③]
    if now_ts - st.session_state.last_buy_time < 60:
        b_score *= 0.7 
    if b_score >= 70: st.session_state.last_buy_time = now_ts
        
    return {
        "p_sup": p_sup, "p_res": p_res, "vol_ratio": vol_ratio,
        "volatility_bp": volatility * 10000,
        "buy_score": b_score, "sell_score": s_score,
        "is_locked": st.session_state.risk_lock_active,
        "lock_time_left": max(0, int(min_lock_sec - (now_ts - st.session_state.lock_timestamp))),
        "break_count": st.session_state.break_count
    }

# ===================== 3. UI 交互层 =====================
st.set_page_config(page_title="Gringotts v8.4 Final Integrity", layout="wide")

def fetch_data(code):
    try:
        pre = "sh" if code.startswith('6') else "sz"
        r = requests.get(f"http://qt.gtimg.cn/q={pre}{code}", timeout=1.5)
        p = r.text.split('~')
        return {'最新价':p[3], '涨跌幅':p[32], '成交量':p[6], 
                '买盘':pd.DataFrame([{'价格':p[9+i*2], '数量':p[10+i*2]} for i in range(5)]),
                '卖盘':pd.DataFrame([{'价格':p[19+i*2], '数量':p[20+i*2]} for i in range(5)])}
    except: return None

with st.sidebar:
    st.title("🏦 Gringotts v8.4")
    target_code = st.text_input("代码", value="601898")
    if st.button("Reset State"): st.session_state.clear(); st.rerun()

if is_trading_time():
    data = fetch_data(target_code)
    if data:
        res = gringotts_kernel(data, data['买盘'], data['卖盘'])
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Price", f"¥{data['最新价']}", f"{data['涨跌幅']}%")
        
        lock_label = f"LOCK ({res['lock_time_left']}s)" if res['is_locked'] else "🟢 ACTIVE"
        c2.metric("System Status", lock_label, f"Break: {res['break_count']}")
        c3.metric("EMA Vol Ratio", f"{res['vol_ratio']:.2f}x", f"{res['volatility_bp']:.1f} bp")

        st.divider()
        b_col, s_col = st.columns(2)
        with b_col:
            st.markdown("### 🌲 买方审计")
            if res['is_locked']: st.error("🛡️ 风控保护中")
            elif res['buy_score'] >= 70: st.success(f"🔱 强烈信号: {res['buy_score']}")
            else: st.info(f"审计评分: {int(res['buy_score'])}")
            
        with s_col:
            st.markdown("### 🔥 卖方审计")
            if res['sell_score'] >= 70: st.error(f"🚨 卖出建议: {res['sell_score']}")
            else: st.write(f"当前抛压: {int(res['sell_score'])}")
else:
    st.info("🌙 非交易时段")

time.sleep(5)
st.rerun()
