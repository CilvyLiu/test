import os
import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timedelta, timezone

# ===================== 0. 环境底座 =====================
TZ_CHINA = timezone(timedelta(hours=8))

def init_vault(target_code):
    if "current_code" not in st.session_state or st.session_state.current_code != target_code:
        st.session_state.current_code = target_code
        st.session_state.price_history = []
        st.session_state.imb_history = []
        st.session_state.cvd_history = []
        st.session_state.prev_vol_cumulative = 0.0
        st.session_state.avg_vol_ema = 0.0
        st.session_state.cvd = 0.0
        st.toast(f"🏛️ v10.5 挂单执行内核已挂载: {target_code}")

def safe_float(x, default=0.0):
    try: return float(str(x).replace(',', ''))
    except: return default

# ===================== 1. 投行高阶工具箱 =====================

def calculate_entropy(volumes):
    probs = volumes / (sum(volumes) + 1e-9)
    return -np.sum(probs * np.log(probs + 1e-9))

def get_market_metrics(prices, imbs, cvds):
    if len(prices) < 20: return 0.2, 0.2, 0.0, 0.0, 0.0
    change = abs(prices[-1] - prices[-15])
    vol = sum(abs(np.diff(prices[-15:]))) + 1e-9
    alpha = np.clip((change / vol) * 0.4 + 0.1, 0.1, 0.5)
    imb_thresh = np.std(imbs) * 2.0 if len(imbs) > 10 else 0.2
    slope_bp = (np.polyfit(np.arange(10), prices[-10:], 1)[0]) / (prices[-1] + 1e-9)
    cvd_trend = np.polyfit(np.arange(len(cvds[-15:])), cvds[-15:], 1)[0] if len(cvds) >= 15 else 0
    atr_sim = np.std(np.diff(prices[-20:])) / (prices[-1] + 1e-9)
    return alpha, imb_thresh, slope_bp, cvd_trend, atr_sim

# ===================== 2. 审计内核 v10.5 (增加精确挂单逻辑) =====================
def institutional_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-100:]
    
    bid_v_list = df_bids['数量'].apply(safe_float).values
    ask_v_list = df_asks['数量'].apply(safe_float).values
    bid_p_list = df_bids['价格'].apply(safe_float).values
    ask_p_list = df_asks['价格'].apply(safe_float).values
    
    bid_v, ask_v = bid_v_list.sum(), ask_v_list.sum()
    imbalance = (bid_v - ask_v) / (bid_v + ask_v + 1e-9)
    st.session_state.imb_history.append(imbalance)
    
    alpha, dyn_thresh, slope_bp, cvd_trend, vol_idx = get_market_metrics(
        st.session_state.price_history, st.session_state.imb_history, st.session_state.cvd_history
    )
    
    ask_ent = calculate_entropy(ask_v_list)
    bid_ent = calculate_entropy(bid_v_list)
    st.session_state.cvd = (1 - alpha) * st.session_state.cvd + alpha * (bid_v - ask_v)
    st.session_state.cvd_history.append(st.session_state.cvd)
    
    # --- 核心：挂单位计算逻辑 ---
    
    # 1. 最低吸入抄底位 (Entry Price)
    # 逻辑：结合支撑位和斜率补偿。若下跌趋势快(slope_bp < 0)，挂单位在买一的基础上往下沉。
    p_sup = np.percentile(st.session_state.price_history[-30:], 20) if len(st.session_state.price_history)>=30 else curr_p
    slope_buffer = abs(slope_bp) * curr_p * 2 # 动态缓冲
    p_entry = min(bid_p_list[0], p_sup) - (0.01 if slope_bp < 0 else -0.01)
    
    # 2. 最高止盈挂单位 (TP Price)
    # 逻辑：若卖盘熵低(假压单)，说明卖一是量化拦路，建议挂在卖一上方 1-2个tick (卖二附近)
    if ask_ent < 1.0:
        p_tp = ask_p_list[0] + 0.02 # 突破挂单
    else:
        # 若是真实抛压，建议挂在卖一位置，甚至在卖一前逃逸
        p_tp = ask_p_list[0]
        
    p_stop = p_sup * 0.995

    # 3. 评分
    b_score = 0
    if curr_p > p_stop:
        if curr_p <= p_entry * 1.002: b_score += 30
        if imbalance > dyn_thresh: b_score += 30
        if cvd_trend > 0: b_score += 40
        
    s_score = 0
    if curr_p >= p_tp * 0.998:
        s_score += 30
        if cvd_trend < 0 and slope_bp > 0: s_score += 50
        if ask_ent < 0.8: s_score -= 20

    vol_adj = np.clip(1 - vol_idx * 100, 0.5, 1.0)
    pos_percent = (80 if b_score >= 80 else 40 if b_score >= 60 else 0) * vol_adj
    if s_score >= 80: pos_percent = -100

    return {
        "p_entry": p_entry, "p_tp": p_tp, "p_stop": p_stop,
        "curr_p": curr_p, "b_score": b_score, "s_score": s_score,
        "pos_percent": pos_percent, "ask_ent": ask_ent, "cvd_t": cvd_trend
    }

# ===================== 3. UI 投行面板 =====================
st.set_page_config(page_title="Institutional Vision v10.5", layout="wide")

def fetch_data(code):
    try:
        pre = "sh" if code.startswith('6') else "sz"
        r = requests.get(f"http://qt.gtimg.cn/q={pre}{code}", timeout=1.5)
        p = r.text.split('~')
        return {'最新价':p[3], '成交量':p[6], 
                '买盘':pd.DataFrame([{'价格':p[9+i*2], '数量':p[10+i*2]} for i in range(5)]),
                '卖盘':pd.DataFrame([{'价格':p[19+i*2], '数量':p[20+i*2]} for i in range(5)])}
    except: return None

with st.sidebar:
    st.title("🏛️ Trader Vault")
    target_code = st.text_input("代码", value="601898")
    init_vault(target_code)
    st.divider()
    st.metric("CVD 动量", f"{st.session_state.cvd:.0f}")
    if st.button("RESET"): st.session_state.clear(); st.rerun()

data = fetch_data(target_code)
if data:
    res = institutional_kernel(data, data['买盘'], data['卖盘'])
    
    # --- 交易执行核心区 ---
    st.write("### 🎯 精确挂单决策审计")
    c1, c2, c3 = st.columns(3)
    c1.metric("止盈最高挂单位", f"¥{res['p_tp']:.2f}", "卖一溢价位")
    c2.metric("抄底最低吸入位", f"¥{res['p_entry']:.2f}", "趋势补偿位")
    c3.metric("风险止损线", f"¥{res['p_stop']:.2f}", delta_color="inverse")

    st.divider()
    
    # 仓位与评分
    m1, m2 = st.columns([1, 2])
    with m1:
        st.metric("建议执行仓位", f"{res['pos_percent']:.0f}%")
    with m2:
        st.write(f"买/卖评分动态: {int(res['b_score'])} / {int(res['s_score'])}")
        st.progress(max(res['b_score'], res['s_score'])/100)

    

    with st.expander("👁️ 盘口深度审计记录"):
        st.write(f"卖盘分布熵: {res['ask_ent']:.2f} (熵低说明量化拦截严重)")
        col_ask, col_bid = st.columns(2)
        col_ask.table(data['卖盘'][::-1])
        col_bid.table(data['买盘'])

time.sleep(5)
st.rerun()
