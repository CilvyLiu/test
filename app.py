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
        st.session_state.cvd = 0.0
        st.session_state.prev_vol_cumulative = 0.0
        st.session_state.avg_vol_ema = 0.0
        st.toast(f"🏛️ 投行级审计内核加载: {target_code}")

def safe_float(x, default=0.0):
    try: return float(str(x).replace(',', ''))
    except: return default

# ===================== 1. 投行级数理工具 =====================

def get_advanced_metrics(prices, imbs, cvds):
    if len(prices) < 15: return 0.2, 0.15, 0.0, 0.0
    
    # 1. ER 效率比 (自适应 Alpha)
    change = abs(prices[-1] - prices[-10])
    vol = sum(abs(np.diff(prices[-10:]))) + 1e-9
    er = change / vol
    alpha = np.clip(er * 0.4 + 0.1, 0.1, 0.5)
    
    # 2. 动态阈值
    imb_thresh = np.std(imbs) * 2.0 if len(imbs) > 10 else 0.2
    
    # 3. 斜率
    slope_bp = (np.polyfit(np.arange(10), prices[-10:], 1)[0]) / (prices[-1] + 1e-9)
    
    # 4. CVD 动量 (判定资金背离)
    cvd_slope = np.polyfit(np.arange(len(cvds[-5:])), cvds[-5:], 1)[0] if len(cvds) >= 5 else 0
    
    return alpha, imb_thresh, slope_bp, cvd_slope

# ===================== 2. 审计内核 v9.5 (意图增强型) =====================
def gringotts_kernel_pro(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    curr_cum_vol = safe_float(quote['成交量'])
    
    # A. 基础压入
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-50:]
    
    bid_v_list = df_bids['数量'].apply(safe_float).values
    ask_v_list = df_asks['数量'].apply(safe_float).values
    bid_v, ask_v = bid_v_list.sum(), ask_v_list.sum()
    
    imbalance = (bid_v - ask_v) / (bid_v + ask_v + 1e-9)
    st.session_state.imb_history.append(imbalance)
    st.session_state.imb_history = st.session_state.imb_history[-50:]
    
    # B. 核心参数计算
    alpha, dyn_thresh, slope_bp, cvd_momentum = get_advanced_metrics(
        st.session_state.price_history, st.session_state.imb_history, st.session_state.cvd_history
    )
    
    # C. CVD 与资金流审计
    st.session_state.cvd = (1 - alpha) * st.session_state.cvd + alpha * (bid_v - ask_v)
    st.session_state.cvd_history.append(st.session_state.cvd)
    st.session_state.cvd_history = st.session_state.cvd_history[-50:]
    
    # D. 集中度审计 (识别诱多/洗盘)
    # 卖方集中度：如果卖一占据了卖盘的大部分，说明是“拦路虎”压单，容易突破；
    # 如果分布平均，说明真实抛压重。
    ask_concentration = ask_v_list[0] / (ask_v + 1e-9)
    bid_concentration = bid_v_list[0] / (bid_v + 1e-9)

    # E. 动态评分决策矩阵
    # --- 买方评分 ---
    b_score = 0
    p_sup = np.percentile(st.session_state.price_history[-20:], 20) if len(st.session_state.price_history)>=20 else curr_p
    if curr_p <= p_sup * 1.003:
        b_score += 30 # 位置得分
        if imbalance > dyn_thresh: b_score += 25 # 挂单得分
        if slope_bp > 0: b_score += 20 # 趋势得分
        if cvd_momentum > 0: b_score += 25 # 资金流入得分
    
    # --- 卖方评分 (增强版) ---
    s_score = 0
    p_res = np.average(df_asks['价格'].apply(safe_float).values, weights=ask_v_list) if ask_v > 0 else curr_p
    if curr_p >= p_res * 0.997:
        s_score += 30 # 位置得分
        if imbalance < -dyn_thresh: s_score += 20 # 挂单压力
        if cvd_momentum < 0 and slope_bp > 0: s_score += 40 # 【核心】缩量诱多判定：价格上行但资金流出
        if ask_concentration > 0.6: s_score -= 15 # 如果压单过于集中在卖一，判定为“假压单”，扣除抛压分

    # F. 仓位管理逻辑
    pos_advice = "观望"
    pos_percent = 0
    if b_score >= 80: pos_advice, pos_percent = "积极进场", 50
    elif b_score >= 60: pos_advice, pos_percent = "试探加仓", 20
    
    if s_score >= 85: pos_advice, pos_percent = "强制减仓", -100 # -100代表清仓
    elif s_score >= 70: pos_advice, pos_percent = "获利减仓", -50

    return {
        "curr_p": curr_p, "p_sup": p_sup, "p_res": p_res,
        "b_score": b_score, "s_score": s_score,
        "pos_advice": pos_advice, "pos_percent": pos_percent,
        "alpha": alpha, "cvd_m": cvd_momentum, "imb": imbalance
    }

# ===================== 3. UI 投行面板 =====================
st.set_page_config(page_title="🏛️ Institutional Vault v9.5", layout="wide")

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
    st.title("🏛️ Gringotts Pro")
    target_code = st.text_input("代码", value="601898")
    init_vault(target_code)
    st.divider()
    st.write(f"资金动量: {st.session_state.cvd:.0f}")
    if st.button("RESET"): st.session_state.clear(); st.rerun()

data = fetch_data(target_code)
if data:
    res = gringotts_kernel_pro(data, data['买盘'], data['卖盘'])
    
    # 核心看板
    st.write(f"### 🛡️ 实时执行审计 - {target_code}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("建议仓位", f"{res['pos_percent']}%", res['pos_advice'])
    c2.metric("资金动量", f"{res['cvd_m']:.2f}", "CVD Slope")
    c3.metric("自适应Alpha", f"{res['alpha']:.2f}")
    c4.metric("最新价", f"¥{res['curr_p']}")

    st.divider()

    # 意图评分区
    col_l, col_r = st.columns(2)
    with col_l:
        st.write("🌲 **买方入场评分**")
        st.progress(min(res['b_score']/100, 1.0), text=f"{int(res['b_score'])}")
        st.caption(f"支撑位: ¥{res['p_sup']:.2f}")
    with col_r:
        st.write("🔥 **卖方抛压评分**")
        st.progress(min(res['s_score']/100, 1.0), text=f"{int(res['s_score'])}")
        st.caption(f"阻力位: ¥{res['p_res']:.2f}")

    

    # 五档原始数据
    with st.expander("👁️ 查看原始五档深度"):
        st.table(data['卖盘'][::-1]) # 卖盘倒序符合视觉逻辑
        st.write("---")
        st.table(data['买盘'])

else:
    st.warning("数据链加载中...")

time.sleep(5)
st.rerun()
