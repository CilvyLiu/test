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

def init_vault(target_code):
    """
    数理逻辑：防止数据污染。
    如果代码切换，强制清空历史，重新计算 Z-Score 边界。
    """
    if "current_code" not in st.session_state or st.session_state.current_code != target_code:
        st.session_state.current_code = target_code
        st.session_state.price_history = []
        st.session_state.sup_history = []
        st.session_state.cvd = 0.0
        st.session_state.prev_vol_cumulative = 0.0
        st.session_state.avg_vol_ema = 0.0
        st.session_state.break_count = 0
        st.toast(f"Switched to {target_code}. Memory Reset.")

def safe_float(x, default=0.0):
    try:
        if x in ['-', '--', None, '', 'None']: return default
        return float(str(x).replace(',', ''))
    except: return default

# ===================== 1. 数理内核工具 =====================

def get_slope(prices):
    if len(prices) < 10: return 0.0
    y = np.array(prices[-10:])
    x = np.arange(len(y))
    slope, _ = np.polyfit(x, y, 1)
    return slope / (prices[-1] + 1e-9)

def safe_weighted_avg(df, price_col, vol_col, fallback):
    try:
        p = df[price_col].apply(safe_float).values
        v = df[vol_col].apply(safe_float).values
        v_sum = v.sum()
        return np.average(p, weights=v) if v_sum > 0 else fallback
    except: return fallback

# ===================== 2. 审计内核 v8.8 =====================
def gringotts_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    curr_cum_vol = safe_float(quote['成交量'])
    
    # --- A. 数据归一化 ---
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-30:]
    
    # EMA 量比 (Volume Ratio)
    tick_vol = max(0, curr_cum_vol - st.session_state.prev_vol_cumulative)
    st.session_state.prev_vol_cumulative = curr_cum_vol
    st.session_state.avg_vol_ema = 0.2 * tick_vol + 0.8 * st.session_state.avg_vol_ema if st.session_state.avg_vol_ema > 0 else tick_vol
    vol_ratio = min(tick_vol / (st.session_state.avg_vol_ema + 1e-9), 10.0)

    # --- B. 委比/委差 (Sentiment) ---
    bid_v_total = df_bids['数量'].apply(safe_float).sum()
    ask_v_total = df_asks['数量'].apply(safe_float).sum()
    order_imbalance = (bid_v_total - ask_v_total) / (bid_v_total + ask_v_total + 1e-9)

    # --- C. 支撑/压力 (Z-Score 变体) ---
    EPSILON = 0.0015
    weighted_bid_p = safe_weighted_avg(df_bids, '价格', '数量', fallback=curr_p)
    st.session_state.sup_history.append(weighted_bid_p)
    st.session_state.sup_history = st.session_state.sup_history[-5:]
    
    # 准确最低吸入价逻辑：综合盘口重心与统计底点
    p_sup = min(np.median(st.session_state.sup_history), 
                np.percentile(st.session_state.price_history[-20:], 20)) if len(st.session_state.price_history)>=20 else curr_p
    
    # 准确最高获利价逻辑：卖盘加权重心
    p_res = safe_weighted_avg(df_asks, '价格', '数量', fallback=curr_p)
    
    min_buy = p_sup * (1 + EPSILON)
    max_sell = p_res * (1 - EPSILON)

    # --- D. 动能审计 ---
    slope = get_slope(st.session_state.price_history)
    st.session_state.cvd = st.session_state.cvd * 0.9 + (bid_v_total - ask_v_total) * 0.1

    # --- E. 评分系统 (对抗量化) ---
    if curr_p < p_sup * 0.996 and vol_ratio > 1.2: st.session_state.break_count += 1
    else: st.session_state.break_count = max(0, st.session_state.break_count - 1)
    is_locked = (st.session_state.break_count >= 2)

    b_score = 0
    if not is_locked and p_sup <= curr_p <= min_buy * 1.005:
        b_score = 60
        if order_imbalance > 0.1: b_score += 20
        if slope > 0: b_score += 20

    s_score = 0
    if curr_p >= max_sell:
        s_score = 70
        if order_imbalance < -0.1 and st.session_state.cvd < 0: s_score = 98 # 诱多背离

    return {
        "p_sup": p_sup, "p_res": p_res, "curr_p": curr_p,
        "min_buy": min_buy, "max_sell": max_sell,
        "b_score": b_score, "s_score": s_score,
        "slope": slope, "vol_ratio": vol_ratio, "imbalance": order_imbalance, "is_locked": is_locked
    }

# ===================== 3. UI 交互层 =====================
st.set_page_config(page_title="Gringotts v8.8 Final", layout="wide")

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
    st.title("🏦 Gringotts v8.8")
    target_code = st.text_input("审计代码", value="601898")
    init_vault(target_code)
    st.divider()
    st.write(f"🧬 **内核状态**")
    st.write(f"CVD: {st.session_state.cvd:.0f}")
    if st.button("Reset Vault"): st.session_state.clear(); st.rerun()

data = fetch_data(target_code)
if data:
    res = gringotts_kernel(data, data['买盘'], data['卖盘'])
    
    # A. 核心指标列
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("当前成交价", f"¥{res['curr_p']}", f"{res['slope']*10000:.1f} bp")
    c2.metric("实时量比", f"{res['vol_ratio']:.2f}x")
    c3.metric("盘口委比", f"{res['imbalance']*100:.1f}%")
    c4.metric("风险锁定", "🔒 LOCKED" if res['is_locked'] else "🔓 ACTIVE")

    st.divider()

    # B. 准确意图点位
    st.write(f"📊 **审计建议**: 最低吸入点 ≥ **¥{res['min_buy']:.2f}** | 最高获利点 ≤ **¥{res['max_sell']:.2f}**")
    
    

    # C. 评分仪表盘
    b_col, s_col = st.columns(2)
    with b_col:
        st.write("🌲 **买方入场审计评分**")
        st.progress(min(res['b_score']/100, 1.0), text=f"评分: {int(res['b_score'])}")
    with s_col:
        st.write("🔥 **卖方抛压审计评分**")
        st.progress(min(res['s_score']/100, 1.0), text=f"评分: {int(res['s_score'])}")

else:
    st.warning("等待数据流接入...")

time.sleep(5)
st.rerun()
