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
    数理逻辑：防止跨股票数据污染
    当 current_code 变化时，强制重置所有历史记忆
    """
    if "current_code" not in st.session_state or st.session_state.current_code != target_code:
        st.session_state.current_code = target_code
        st.session_state.price_history = []
        st.session_state.sup_history = []
        st.session_state.cvd = 0.0
        st.session_state.prev_vol_cumulative = 0.0
        st.session_state.avg_vol_ema = 0.0
        st.session_state.break_count = 0
        # 强制清除旧缓存，确保支撑位重新审计
        st.toast(f"已自动切换至代码: {target_code}，正在重新建立审计记忆...")

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

# ===================== 2. 审计内核 v8.7 =====================
def gringotts_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    curr_cum_vol = safe_float(quote['成交量'])
    
    # --- A. 数据压入 ---
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-30:]
    
    # --- B. 支撑与压力审计 ---
    EPSILON = 0.0015
    # 实时盘口价
    weighted_bid_p = safe_weighted_avg(df_bids, '价格', '数量', fallback=curr_p)
    st.session_state.sup_history.append(weighted_bid_p)
    st.session_state.sup_history = st.session_state.sup_history[-5:]
    
    # 动态防御支撑：结合盘口与近期价格分布
    p_sup = min(np.median(st.session_state.sup_history), 
                np.percentile(st.session_state.price_history[-20:], 20)) if len(st.session_state.price_history)>=20 else curr_p
    p_res = safe_weighted_avg(df_asks, '价格', '数量', fallback=curr_p)
    
    min_buy = p_sup * (1 + EPSILON)
    max_sell = p_res * (1 - EPSILON)

    # --- C. 意图与动能审计 ---
    slope = get_slope(st.session_state.price_history)
    bid_v = df_bids['数量'].apply(safe_float).sum()
    ask_v = df_asks['数量'].apply(safe_float).sum()
    st.session_state.cvd = st.session_state.cvd * 0.9 + (bid_v - ask_v) * 0.1

    # --- D. 评分系统 ---
    b_score = 0
    if p_sup * 0.99 <= curr_p <= min_buy * 1.01:
        b_score = 50
        if slope > 0: b_score += 25
        if st.session_state.cvd > 0: b_score += 25
    
    s_score = 0
    if curr_p >= max_sell:
        s_score = 70
        if slope > 0.0002 and st.session_state.cvd < 0: s_score = 95 # 诱多预警

    return {
        "p_sup": p_sup, "p_res": p_res, "curr_p": curr_p,
        "min_buy": min_buy, "max_sell": max_sell,
        "b_score": b_score, "s_score": s_score, "slope": slope
    }

# ===================== 3. UI 交互层 =====================
st.set_page_config(page_title="Gringotts v8.7 Production", layout="wide")

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
    st.title("🏦 Gringotts v8.7")
    target_code = st.text_input("输入代码 (回车切换)", value="601898")
    
    # 核心：自动执行重置逻辑
    init_vault(target_code)
    
    st.write("---")
    st.write(f"🧬 **内核状态审计**")
    st.write(f"代码: `{st.session_state.current_code}`")
    st.write(f"CVD 能量: {st.session_state.cvd:.1f}")
    st.write(f"样本数: {len(st.session_state.price_history)}/30")
    
    if st.button("手动 Reset Vault", use_container_width=True):
        st.session_state.clear()
        st.rerun()

# 逻辑执行
data = fetch_data(target_code)
if data:
    res = gringotts_kernel(data, data['买盘'], data['卖盘'])
    
    # A. 顶层指标
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("当前成交价", f"¥{res['curr_p']}", f"{res['slope']*10000:.1f} bp")
    c2.metric("最低买入位 (防线)", f"¥{res['p_sup']:.2f}", "结构支撑")
    c3.metric("最高卖出位 (目标)", f"¥{res['p_res']:.2f}", "量化压力墙")
    c4.metric("审计门槛", f"≥ ¥{res['min_buy']:.2f}", "买入确认点")

    st.divider()

    # B. 核心博弈建议
    st.subheader("⚡ 实时操作审计建议")
    st.markdown(f"""
    > **博弈区间：** [ ¥{res['p_sup']:.2f} (底) <--- 震荡 ---> ¥{res['p_res']:.2f} (顶) ]  
    > **操作指令：** 确认入场位 **¥{res['min_buy']:.2f}** | 获利撤退位 **¥{res['max_sell']:.2f}**
    """)
    
    b_col, s_col = st.columns(2)
    with b_col:
        st.write("🌲 **买方审计 (入场安全度)**")
        st.progress(min(res['b_score']/100, 1.0), text=f"评分: {int(res['b_score'])}")
    with s_col:
        st.write("🔥 **卖方审计 (抛压危险度)**")
        st.progress(min(res['s_score']/100, 1.0), text=f"评分: {int(res['s_score'])}")

else:
    st.error("无法获取盘口数据，请检查代码或网络环境。")

time.sleep(5)
st.rerun()
