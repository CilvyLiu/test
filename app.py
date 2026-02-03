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
        "sup_history": [],        
        "cvd_history": [],         # 新增：CVD平滑历史
        "prev_vol_cumulative": 0.0, 
        "risk_lock_active": False,
        "lock_timestamp": 0.0,     
        "last_valid_vol": 0.0005,  
        "avg_vol_ema": 0.0,        
        "last_sell_time": 0.0,
        "last_buy_time": 0.0,      
        "break_count": 0,
        "cvd": 0.0,                # 新增：CVD累积值
        "op_info": "系统初始化完成"   # 新增：操作提示
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

# ===================== 1. 核心工具函数 =====================

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

def get_slope(prices):
    """计算最近10个Tick的价格变化斜率"""
    if len(prices) < 10: return 0.0
    y = np.array(prices[-10:])
    x = np.arange(len(y))
    slope, _ = np.polyfit(x, y, 1)
    return slope / (prices[-1] + 1e-9) # 归一化斜率

# ===================== 2. 审计内核 v8.6 =====================
def gringotts_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    curr_cum_vol = safe_float(quote['成交量'])
    now_ts = time.time()
    
    # --- A. 基础数据处理 ---
    if curr_cum_vol < st.session_state.prev_vol_cumulative:
        st.session_state.prev_vol_cumulative = curr_cum_vol
        tick_vol = 0
    else:
        tick_vol = max(0, curr_cum_vol - st.session_state.prev_vol_cumulative)
    st.session_state.prev_vol_cumulative = curr_cum_vol
    
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-30:]
    volatility = get_filtered_volatility(st.session_state.price_history)
    
    alpha = 0.2
    st.session_state.avg_vol_ema = alpha * tick_vol + (1 - alpha) * st.session_state.avg_vol_ema if st.session_state.avg_vol_ema > 0 else tick_vol
    vol_ratio = min(tick_vol / (st.session_state.avg_vol_ema + 1e-9), 10.0)

    # --- B. 支撑/压力与 Epsilon 缓冲区 ---
    EPSILON = 0.0015
    weighted_bid_p = safe_weighted_avg(df_bids, '价格', '数量', fallback=curr_p)
    st.session_state.sup_history.append(weighted_bid_p)
    st.session_state.sup_history = st.session_state.sup_history[-5:] 
    
    stable_bid_sup = np.median(st.session_state.sup_history)
    p_sup = min(stable_bid_sup, np.percentile(st.session_state.price_history[-20:], 20)) if len(st.session_state.price_history) >= 20 else stable_bid_sup
    p_res = safe_weighted_avg(df_asks, '价格', '数量', fallback=curr_p)
    
    min_buy_price = p_sup * (1 + EPSILON)
    max_sell_price = p_res * (1 - EPSILON)

    # --- C. 核心进化：斜率与 CVD 意图分析 ---
    slope = get_slope(st.session_state.price_history)
    
    # CVD 计算 (处理字符串并累积)
    bid_v_sum = df_bids['数量'].apply(safe_float).sum()
    ask_v_sum = df_asks['数量'].apply(safe_float).sum()
    delta = bid_v_sum - ask_v_sum
    
    # CVD 衰减累积，更灵敏地反映当前主力意图
    st.session_state.cvd = st.session_state.cvd * 0.9 + delta * 0.1 
    is_bullish_cvd = st.session_state.cvd > 0

    # --- D. 决策评分与风控 ---
    if curr_p < p_sup * 0.996 and vol_ratio > 0.6:
        st.session_state.break_count += 1
    else:
        st.session_state.break_count = max(0, st.session_state.break_count - 1)

    lock_trigger = (st.session_state.break_count >= 2) or (volatility > 0.003)
    if lock_trigger:
        st.session_state.risk_lock_active = True
        st.session_state.lock_timestamp = now_ts
    elif not (st.session_state.risk_lock_active and (now_ts - st.session_state.lock_timestamp < 30)):
        st.session_state.risk_lock_active = False

    # 买卖区判定
    is_in_buy_zone = p_sup <= curr_p <= (min_buy_price * 1.002)
    is_in_sell_zone = curr_p >= max_sell_price

    # 初始分值
    b_score = 50 if (not st.session_state.risk_lock_active and is_in_buy_zone) else 0
    s_score = 40 if is_in_sell_zone else 0

    # --- 博弈修正 ( Nova's Logic ) ---
    st.session_state.op_info = "市场处于均衡状态"
    
    # 1. 坑洞压制：快速下跌 + CVD走弱
    if slope < -0.0002 and not is_bullish_cvd:
        b_score *= 0.3
        st.session_state.op_info = "⚠️ 动能杀跌，避开接刀坑"
    
    # 2. 动能奖励：斜率回归转正 + CVD走强
    elif slope > 0.0001 and is_bullish_cvd:
        b_score *= 1.3
        st.session_state.op_info = "✅ 能量确认，斜率回归买入"

    # 3. 卖方修正：价格上涨但 CVD 走弱 (诱多)
    if slope > 0.0002 and not is_bullish_cvd:
        s_score *= 1.4
        st.session_state.op_info = "🚨 缩量诱多背离，建议撤退"
    elif slope > 0 and is_bullish_cvd:
        s_score *= 0.7 # 强势上涨中减少卖出倾向

    return {
        "p_sup": p_sup, "p_res": p_res, "min_buy": min_buy_price, "max_sell": max_sell_price,
        "curr_price": curr_p, "buy_score": b_score, "sell_score": s_score,
        "slope": slope, "cvd": st.session_state.cvd, "op_info": st.session_state.op_info,
        "is_locked": st.session_state.risk_lock_active
    }

# ===================== 3. UI 交互层 =====================
st.set_page_config(page_title="Gringotts v8.6 Slope+CVD", layout="wide")

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
    st.metric("CVD 能量流", f"{st.session_state.cvd:.0f}", delta="主力流入" if st.session_state.cvd > 0 else "主力流出")
    if st.button("Reset State"): st.session_state.clear(); st.rerun()

data = fetch_data(target_code)
if data:
    res = gringotts_kernel(data, data['买盘'], data['卖盘'])
    
    st.subheader(f"执行决策：{res['op_info']}")
    
    c1, c2, c3 = st.columns(3)
    c1.metric("当前价", f"¥{res['curr_price']}", f"斜率: {res['slope']*10000:.1f} bp")
    c2.metric("审计支撑", f"¥{res['p_sup']:.2f}", f"买入门槛: ¥{res['min_buy']:.2f}")
    c3.metric("审计压力", f"¥{res['p_res']:.2f}", f"获利撤退: ¥{res['max_sell']:.2f}")

    st.divider()
    b_col, s_col = st.columns(2)
    with b_col:
        st.write("🌲 **买入评分仪表**")
        st.progress(min(res['buy_score']/100, 1.0), text=f"综合评分: {int(res['buy_score'])}")
    with s_col:
        st.write("🔥 **卖出评分仪表**")
        st.progress(min(res['sell_score']/100, 1.0), text=f"抛压评分: {int(res['sell_score'])}")

time.sleep(5)
st.rerun()
