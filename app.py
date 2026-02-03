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
    """
    逻辑：彻底杜绝幽灵数据。
    换股时，强制初始化所有交易员观测指标。
    """
    if "current_code" not in st.session_state or st.session_state.current_code != target_code:
        st.session_state.current_code = target_code
        st.session_state.price_history = []
        st.session_state.imb_history = []
        st.session_state.cvd = 0.0
        st.session_state.prev_vol_cumulative = 0.0
        st.session_state.avg_vol_ema = 0.0
        st.session_state.break_count = 0 
        st.toast(f"🚨 交易员面板已切换: {target_code}")

def safe_float(x, default=0.0):
    try: return float(str(x).replace(',', ''))
    except: return default

# ===================== 1. 数理内核：全透明审计 =====================

def get_market_pulse(prices, imbs):
    if len(prices) < 10: return 0.2, 0.15, 0.0
    
    # 1. 动态权重 (Alpha) - 反映市场效率
    change = abs(prices[-1] - prices[-10])
    vol = sum(abs(np.diff(prices[-10:]))) + 1e-9
    er = change / vol
    alpha = np.clip(er * 0.4 + 0.1, 0.1, 0.5)
    
    # 2. 动态委比阈值 - 对抗量化假单
    imb_thresh = np.std(imbs) * 1.5 if len(imbs) > 10 else 0.15
    
    # 3. BP 斜率 - 交易员的“盘感”量化
    x = np.arange(len(prices[-10:]))
    slope, _ = np.polyfit(x, prices[-10:], 1)
    slope_bp = slope / (prices[-1] + 1e-9)
    
    return alpha, max(0.1, min(imb_thresh, 0.4)), slope_bp

# ===================== 2. 审计内核 v9.0 =====================
def gringotts_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    curr_cum_vol = safe_float(quote['成交量'])
    
    # A. 基础压入
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-30:]
    
    bid_v = df_bids['数量'].apply(safe_float).sum()
    ask_v = df_asks['数量'].apply(safe_float).sum()
    imbalance = (bid_v - ask_v) / (bid_v + ask_v + 1e-9)
    st.session_state.imb_history.append(imbalance)
    st.session_state.imb_history = st.session_state.imb_history[-30:]
    
    # B. 提取交易员关键指标
    alpha, dyn_thresh, slope_bp = get_market_pulse(st.session_state.price_history, st.session_state.imb_history)
    
    # C. 计算量比 (Vol Ratio)
    tick_vol = max(0, curr_cum_vol - st.session_state.prev_vol_cumulative)
    st.session_state.prev_vol_cumulative = curr_cum_vol
    st.session_state.avg_vol_ema = 0.2 * tick_vol + 0.8 * st.session_state.avg_vol_ema if st.session_state.avg_vol_ema > 0 else tick_vol
    vol_ratio = tick_vol / (st.session_state.avg_vol_ema + 1e-9)

    # D. 统计边界 (Z-Score 核心支撑压力)
    p_sup = np.percentile(st.session_state.price_history[-20:], 20) if len(st.session_state.price_history) >= 20 else curr_p
    p_res = np.average(df_asks['价格'].apply(safe_float).values, 
                       weights=df_asks['数量'].apply(safe_float).values) if ask_v > 0 else curr_p
    
    min_buy = p_sup * 1.0015
    max_sell = p_res * 0.9985

    # E. 评分系统
    st.session_state.cvd = (1 - alpha) * st.session_state.cvd + alpha * (bid_v - ask_v)
    b_score = 0
    if curr_p <= min_buy * 1.001:
        b_score = 50
        if imbalance > dyn_thresh: b_score += 25
        if slope_bp > 0: b_score += 25

    return {
        "p_sup": p_sup, "p_res": p_res, "curr_p": curr_p,
        "min_buy": min_buy, "max_sell": max_sell,
        "b_score": b_score, "vol_ratio": vol_ratio,
        "alpha": alpha, "thresh": dyn_thresh, "slope": slope_bp, "imbalance": imbalance,
        "bid_v": bid_v, "ask_v": ask_v
    }

# ===================== 3. UI 交易面板 =====================
st.set_page_config(page_title="🏦 Trader Vision v9.0", layout="wide")

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
    st.title("🏦 Trader Vision")
    target_code = st.text_input("输入代码", value="601898")
    init_vault(target_code)
    st.divider()
    st.metric("实时 CVD 净流", f"{st.session_state.cvd:.0f}")
    if st.button("RESET ALL"): st.session_state.clear(); st.rerun()

data = fetch_data(target_code)
if data:
    res = gringotts_kernel(data, data['买盘'], data['卖盤'])
    
    # --- 第一层：原始数据观测区 ---
    st.write("### 👁️ 原始观测（数据之眼）")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("最新价格", f"¥{res['curr_p']}", f"{res['slope']*10000:.1f} bp")
    m2.metric("实时量比", f"{res['vol_ratio']:.2f}x")
    m3.metric("盘口委比", f"{res['imbalance']*100:.1f}%", f"阈值 {res['thresh']:.2f}")
    m4.metric("买盘总挂单", f"{res['bid_v']:.0f}")
    m5.metric("卖盘总挂单", f"{res['ask_v']:.0f}")

    st.divider()

    # --- 第二层：决策与评分 ---
    st.subheader("🎯 交易意图与审计评分")
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        st.success(f"最低建议买点: ¥{res['min_buy']:.2f}")
    with c2:
        st.error(f"最高建议获利: ¥{res['max_sell']:.2f}")
    with c3:
        st.progress(min(res['b_score']/100, 1.0), text=f"买方综合评分: {int(res['b_score'])}")

    

    # --- 第三层：五档盘口直视 ---
    st.write("### 🪜 五档深度")
    col_a, col_b = st.columns(2)
    with col_a:
        st.write("买五深度")
        st.dataframe(data['买盘'], use_container_width=True)
    with col_b:
        st.write("卖五深度")
        st.dataframe(data['卖盘'], use_container_width=True)

else:
    st.warning("正在等待行情接入...")

time.sleep(5)
st.rerun()
