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
        "sup_history": [],        
        "cvd": 0.0,                # 累计成交量差值 (Cumulative Volume Delta)
        "prev_vol_cumulative": 0.0, 
        "risk_lock_active": False,
        "lock_timestamp": 0.0,     
        "last_valid_vol": 0.0005,  
        "avg_vol_ema": 0.0,        
        "last_sell_time": 0.0,
        "last_buy_time": 0.0,      
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

# ===================== 1. 数理内核工具 =====================

def get_slope(prices):
    """
    数理逻辑：测算价格一阶导 (Slope)
    公式：Linear Regression Slope / Current Price
    用途：识别惯性杀跌，避免“接刀”坑
    """
    if len(prices) < 10: return 0.0
    y = np.array(prices[-10:])
    x = np.arange(len(y))
    slope, _ = np.polyfit(x, y, 1)
    return slope / (prices[-1] + 1e-9)

def safe_weighted_avg(df, price_col, vol_col, fallback):
    """
    数理逻辑：成交量加权平均价 (VWAP)
    公式：Σ(Price * Volume) / ΣVolume
    """
    try:
        p = df[price_col].apply(safe_float).values
        v = df[vol_col].apply(safe_float).values
        v_sum = v.sum()
        return np.average(p, weights=v) if v_sum > 0 else fallback
    except: return fallback

# ===================== 2. 审计内核 v8.6 =====================
def gringotts_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    curr_cum_vol = safe_float(quote['成交量'])
    now_ts = time.time()
    
    # --- A. 量能归一化 (EMA 滤波) ---
    tick_vol = max(0, curr_cum_vol - st.session_state.prev_vol_cumulative) if curr_cum_vol >= st.session_state.prev_vol_cumulative else 0
    st.session_state.prev_vol_cumulative = curr_cum_vol
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-30:]
    
    # EMA 相对量比公式：V_ratio = Tick_Vol / EMA(Vol)
    st.session_state.avg_vol_ema = 0.2 * tick_vol + 0.8 * st.session_state.avg_vol_ema if st.session_state.avg_vol_ema > 0 else tick_vol
    vol_ratio = min(tick_vol / (st.session_state.avg_vol_ema + 1e-9), 10.0)

    # --- B. 支撑/压力与 ε-缓冲区 ---
    # Epsilon (ε) = 0.15% 作为博弈确认空间
    EPSILON = 0.0015
    weighted_bid_p = safe_weighted_avg(df_bids, '价格', '数量', fallback=curr_p)
    st.session_state.sup_history.append(weighted_bid_p)
    st.session_state.sup_history = st.session_state.sup_history[-5:]
    
    # 支撑逻辑：取盘口中位数与价格百分位的极小值（防御性审计）
    p_sup = min(np.median(st.session_state.sup_history), np.percentile(st.session_state.price_history[-20:], 20)) if len(st.session_state.price_history)>=20 else curr_p
    p_res = safe_weighted_avg(df_asks, '价格', '数量', fallback=curr_p)
    
    min_buy = p_sup * (1 + EPSILON)  # 入场门槛 (确认上涨动能)
    max_sell = p_res * (1 - EPSILON) # 撤退门槛 (避免撞压力墙)

    # --- C. 斜率与 CVD 联合审计 ---
    slope = get_slope(st.session_state.price_history)
    bid_v = df_bids['数量'].apply(safe_float).sum()
    ask_v = df_asks['数量'].apply(safe_float).sum()
    # CVD 累积公式：CVD_t = CVD_t-1 * 0.9 + (Bid_sum - Ask_sum) * 0.1
    st.session_state.cvd = st.session_state.cvd * 0.9 + (bid_v - ask_v) * 0.1
    is_bullish_cvd = st.session_state.cvd > 0

    # --- D. 综合评分决策系统 ---
    # 结构化风控锁逻辑
    if curr_p < p_sup * 0.996 and vol_ratio > 0.6: st.session_state.break_count += 1
    else: st.session_state.break_count = max(0, st.session_state.break_count - 1)
    
    is_locked = (st.session_state.break_count >= 2)
    
    # 买方评分 (基于位置、斜率回归与能量验证)
    b_score = 0
    if not is_locked and p_sup <= curr_p <= min_buy * 1.002:
        b_score = 70
        if slope < -0.0002 and not is_bullish_cvd: b_score *= 0.3 # 坑洞回避逻辑
        elif slope > 0.0001 and is_bullish_cvd: b_score *= 1.2    # 动能共振奖励

    # 卖方评分
    s_score = 0
    if curr_p >= max_sell:
        s_score = 70
        if slope > 0.0002 and not is_bullish_cvd: s_score *= 1.4  # 缩量诱多背离
        
    return {
        "p_sup": p_sup, "p_res": p_res, "curr_p": curr_p,
        "min_buy": min_buy, "max_sell": max_sell,
        "b_score": b_score, "s_score": s_score,
        "slope": slope, "cvd": st.session_state.cvd, "is_locked": is_locked
    }

# ===================== 3. UI 交互层 =====================
st.set_page_config(page_title="Gringotts v8.6 Final", layout="wide")

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
    st.title("🏦 Gringotts v8.6")
    target_code = st.text_input("代码", value="601898")
    st.write("---")
    st.write(f"🧬 **内核状态**")
    st.write(f"CVD: {st.session_state.cvd:.0f}")
    if st.button("Reset Vault"): st.session_state.clear(); st.rerun()

data = fetch_data(target_code)
if data:
    res = gringotts_kernel(data, data['买盘'], data['卖盘'])
    
    # A. 核心指标列 (四个关键价格)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("当前成交价", f"¥{res['curr_p']}", f"{res['slope']*10000:.1f} bp (斜率)")
    c2.metric("最低买入位 (防线)", f"¥{res['p_sup']:.2f}", "结构支撑")
    c3.metric("最高卖出位 (目标)", f"¥{res['p_res']:.2f}", "量化压力")
    c4.metric("风险锁定状态", "🔒 LOCKED" if res['is_locked'] else "🔓 ACTIVE")

    st.divider()

    # B. 操作门槛显示
    st.write(f"📊 **审计门槛**: 入场确认价 ≥ **¥{res['min_buy']:.2f}** | 获利先行价 ≤ **¥{res['max_sell']:.2f}**")
    
    # C. 评分仪表盘
    b_col, s_col = st.columns(2)
    with b_col:
        st.write("🌲 **买方审计评分**")
        st.progress(min(res['b_score']/100, 1.0), text=f"评分: {int(res['b_score'])}")
    with s_col:
        st.write("🔥 **卖方审计评分**")
        st.progress(min(res['s_score']/100, 1.0), text=f"评分: {int(res['s_score'])}")

else:
    st.warning("数据链连接异常，检查网络或代码...")

time.sleep(5)
st.rerun()
