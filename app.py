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
        st.toast(f"🏛️ v10.0 投行高频审计内核挂载: {target_code}")

def safe_float(x, default=0.0):
    try: return float(str(x).replace(',', ''))
    except: return default

# ===================== 1. 投行高阶工具箱 =====================

def calculate_entropy(volumes):
    """数理逻辑：分布熵。用于识别盘口挂单是否由量化机器人操纵。"""
    probs = volumes / (sum(volumes) + 1e-9)
    return -np.sum(probs * np.log(probs + 1e-9))

def get_market_metrics(prices, imbs, cvds):
    if len(prices) < 20: return 0.2, 0.2, 0.0, 0.0, 0.0
    
    # 1. 动态权重 Alpha (ER效率比)
    change = abs(prices[-1] - prices[-15])
    vol = sum(abs(np.diff(prices[-15:]))) + 1e-9
    alpha = np.clip((change / vol) * 0.4 + 0.1, 0.1, 0.5)
    
    # 2. 动态委比阈值
    imb_thresh = np.std(imbs) * 2.0 if len(imbs) > 10 else 0.2
    
    # 3. 价格斜率
    slope_bp = (np.polyfit(np.arange(10), prices[-10:], 1)[0]) / (prices[-1] + 1e-9)
    
    # 4. CVD 趋势降噪 (取15 tick窗口)
    cvd_trend = np.polyfit(np.arange(len(cvds[-15:])), cvds[-15:], 1)[0] if len(cvds) >= 15 else 0
    
    # 5. 波动率指数 (用于仓位缩减)
    atr_sim = np.std(np.diff(prices[-20:])) / (prices[-1] + 1e-9)
    
    return alpha, imb_thresh, slope_bp, cvd_trend, atr_sim

# ===================== 2. 审计内核 v10.0 =====================
def institutional_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    curr_cum_vol = safe_float(quote['成交量'])
    
    # A. 基础压入
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-100:]
    
    bid_v_list = df_bids['数量'].apply(safe_float).values
    ask_v_list = df_asks['数量'].apply(safe_float).values
    bid_v, ask_v = bid_v_list.sum(), ask_v_list.sum()
    
    imbalance = (bid_v - ask_v) / (bid_v + ask_v + 1e-9)
    st.session_state.imb_history.append(imbalance)
    st.session_state.imb_history = st.session_state.imb_history[-100:]
    
    # B. 高阶参数计算
    alpha, dyn_thresh, slope_bp, cvd_trend, vol_idx = get_market_metrics(
        st.session_state.price_history, st.session_state.imb_history, st.session_state.cvd_history
    )
    
    # C. 挂单分布熵分析
    ask_entropy = calculate_entropy(ask_v_list)
    bid_entropy = calculate_entropy(bid_v_list)
    
    # D. CVD 动量平滑
    st.session_state.cvd = (1 - alpha) * st.session_state.cvd + alpha * (bid_v - ask_v)
    st.session_state.cvd_history.append(st.session_state.cvd)
    st.session_state.cvd_history = st.session_state.cvd_history[-100:]
    
    # E. 评分决策矩阵 (改进版)
    p_sup = np.percentile(st.session_state.price_history[-30:], 20) if len(st.session_state.price_history)>=30 else curr_p
    p_res = np.average(df_asks['价格'].apply(safe_float).values, weights=ask_v_list) if ask_v > 0 else curr_p
    p_stop = p_sup * 0.995 # 动态止损线
    
    # --- 买方评分 ---
    b_score = 0
    if curr_p > p_stop:
        if curr_p <= p_sup * 1.003: b_score += 20
        if imbalance > dyn_thresh: b_score += 20
        if slope_bp > 0: b_score += 20
        if cvd_trend > 0: b_score += 20
        if bid_entropy > 1.2: b_score += 20 # 买盘分布均匀，真实接盘力强
        
    # --- 卖方评分 (强化意图识别) ---
    s_score = 0
    if curr_p >= p_res * 0.997:
        s_score += 20
        if imbalance < -dyn_thresh: s_score += 20
        if cvd_trend < 0 and slope_bp > 0: s_score += 40 # 典型诱多背离
        if ask_entropy < 0.8: s_score -= 30 # 卖盘极度集中，判定为虚假压单（拦截）

    # F. 仓位管理 (波动率调节)
    vol_adj = np.clip(1 - vol_idx * 100, 0.5, 1.0) # 波动越大，仓位倍率越低
    pos_percent = 0
    if b_score >= 80: pos_percent = 80 * vol_adj
    elif b_score >= 60: pos_percent = 40 * vol_adj
    
    if s_score >= 80: pos_percent = -100 # 信号清仓
    elif s_score >= 60: pos_percent = -50  # 减仓

    return {
        "p_sup": p_sup, "p_res": p_res, "p_stop": p_stop,
        "curr_p": curr_p, "b_score": b_score, "s_score": s_score,
        "pos_percent": pos_percent, "ask_ent": ask_entropy,
        "cvd_t": cvd_trend, "vol_idx": vol_idx
    }

# ===================== 3. UI 投行面板 =====================
st.set_page_config(page_title="Institutional Vision v10.0", layout="wide")

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
    st.title("🏛️ Vault v10.0")
    target_code = st.text_input("代码", value="601898")
    init_vault(target_code)
    if st.button("RESET VAULT"): st.session_state.clear(); st.rerun()

data = fetch_data(target_code)
if data:
    res = institutional_kernel(data, data['买盘'], data['卖盘'])
    
    # 顶部监控区
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("执行建议", f"{res['pos_percent']:.0f}%", "仓位权重")
    c2.metric("卖盘熵值", f"{res['ask_ent']:.2f}", "低熵=假压单" if res['ask_ent'] < 1.0 else "高熵=真抛压")
    c3.metric("资金动量趋势", f"{res['cvd_t']:.2f}", "降噪CVD")
    c4.metric("动态止损价", f"¥{res['p_stop']:.2f}")

    st.divider()
    
    # 意图评分仪表盘
    l, r = st.columns(2)
    with l:
        st.write("🌲 **买方多维意图评分**")
        st.progress(min(res['b_score']/100, 1.0), text=f"Score: {int(res['b_score'])}")
    with r:
        st.write("🔥 **卖方意图与背离审计**")
        st.progress(min(res['s_score']/100, 1.0), text=f"Score: {int(res['s_score'])}")

    # 交易员观测
    with st.expander("👁️ 原始深度与熵值分布"):
        st.write(f"当前波动率系数: {res['vol_idx']:.5f}")
        col1, col2 = st.columns(2)
        col1.table(data['卖盘'][::-1])
        col2.table(data['买盘'])

time.sleep(5)
st.rerun()
