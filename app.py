import os
import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timedelta, timezone

# ===================== 0. 环境底座与时间门禁 (v11保留) =====================
TZ_CHINA = timezone(timedelta(hours=8))

def is_trade_time():
    """审计当前是否为 A 股合法交易时段"""
    now = datetime.now(TZ_CHINA)
    if now.weekday() >= 5:
        return False, "😴 非交易日 (休息中)"
    curr_time = now.strftime("%H:%M:%S")
    if ("09:15:00" <= curr_time <= "11:30:30") or ("13:00:00" <= curr_time <= "15:02:00"):
        return True, "⚡ 审计内核运行中"
    return False, "🌙 非交易时段 (已挂起)"

def init_vault(target_code):
    if "current_code" not in st.session_state or st.session_state.current_code != target_code:
        st.session_state.current_code = target_code
        st.session_state.price_history = []
        st.session_state.imb_history = []
        st.session_state.cvd_history = []
        st.session_state.cvd = 0.0
        st.toast(f"🏛️ v12.8 全量功能内核挂载: {target_code}")

def safe_float(x, default=0.0):
    try: return float(str(x).replace(',', ''))
    except: return default

# ===================== 1. 数理工具箱 (v10底座) =====================
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

# ===================== UI 侧边栏交互补全 =====================
with st.sidebar:
    st.title("🏛️ Vault v13.9")
    target_code = st.text_input("代码", value="601898")
    total_capital = st.number_input("总投放金额 (CNY)", value=100000)
    refresh_rate = st.slider("审计刷新频率 (秒)", 1, 10, 3)
    init_vault(target_code)
    st.info(f"审计状态: {is_trade_time()[1]}")
    if st.button("RESET"): st.session_state.clear(); st.rerun()# ===================== 2. 核心审计内核 (全逻辑合并) =====================
def institutional_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    total_vol_day = safe_float(quote['成交量']) * 100 
    
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-100:]
    
    bid_v = df_bids['数量'].apply(safe_float).values
    ask_v = df_asks['数量'].apply(safe_float).values
    bid_p = df_bids['价格'].apply(safe_float).values
    ask_p = df_asks['价格'].apply(safe_float).values
    
    bid_v_total, ask_v_total = bid_v.sum(), ask_v.sum()
    imbalance = (bid_v_total - ask_v_total) / (bid_v_total + ask_v_total + 1e-9)
    st.session_state.imb_history.append(imbalance)
    
    # 2.1 高阶参数与CVD计算 (v10原逻辑)
    alpha, dyn_thresh, slope_bp, cvd_trend, vol_idx = get_market_metrics(
        st.session_state.price_history, st.session_state.imb_history, st.session_state.cvd_history
    )
    st.session_state.cvd = (1 - alpha) * st.session_state.cvd + alpha * (bid_v_total - ask_v_total)
    st.session_state.cvd_history.append(st.session_state.cvd)
    
    ask_ent = calculate_entropy(ask_v)
    bid_ent = calculate_entropy(bid_v)
    avg_ask_v, avg_bid_v = np.mean(ask_v), np.mean(bid_v)
    
    # 核心：补回微量单标记 (小于平均30%且<50手)
    def audit_logic(v, avg_v, entropy, side):
        if (v >= 500) or (v > avg_v * 2.2):
            if side == 'ask' and entropy < 1.35: return "🛑 拦截大单"
            if side == 'bid' and entropy < 1.35: return "🛡️ 诱多托单"
        if (v < avg_v * 0.3) and (v < 50): return "🪶 微量拆单"
        return ""

    ask_labels = [audit_logic(v, avg_ask_v, ask_ent, 'ask') for v in ask_v]
    bid_labels = [audit_logic(v, avg_bid_v, bid_ent, 'bid') for v in bid_v]

    # 2.3 评分矩阵 (补齐卖方逻辑)
    p_sup = np.percentile(st.session_state.price_history[-30:], 20) if len(st.session_state.price_history)>=30 else curr_p
    p_res = np.average(ask_p, weights=ask_v) if ask_v_total > 0 else curr_p
    p_stop = p_sup * 0.995 

    b_score = 0
    if curr_p > p_stop:
        if imbalance > dyn_thresh: b_score += 25
        if cvd_trend > 0: b_score += 25
        if bid_ent > 1.2: b_score += 50 

    s_score = 0
    if cvd_trend < 0: s_score += 40
    if ask_ent < 1.1: s_score += 40
    if "🛑 拦截大单" in ask_labels: s_score += 20

    vol_adj = np.clip(1 - vol_idx * 100, 0.5, 1.0)
    pos_percent = 0
    if b_score >= 80: pos_percent = 80 * vol_adj
    elif b_score >= 50: pos_percent = 40 * vol_adj
    if s_score >= 80: pos_percent = -100 

    liq_idx = (np.sum(bid_v * bid_p) * 100 / (total_vol_day * curr_p + 1e-9)) * 100

    return {
        "p_tp": ask_p[0], "p_entry": bid_p[2], "p_stop": p_stop, "p_sup": p_sup, "p_res": p_res,
        "curr_p": curr_p, "liq_idx": liq_idx, "b_score": b_score, "s_score": s_score,
        "pos_percent": pos_percent, "ask_ent": ask_ent, "bid_ent": bid_ent, "cvd_t": cvd_trend,
        "ask_labels": ask_labels, "bid_labels": bid_labels
    }# ===================== 3. UI 投行面板 (全要素显示) =====================
st.set_page_config(page_title="Nova Institutional Vault v13.9", layout="wide")
trading, trade_msg = is_trade_time()

def fetch_data(code):
    try:
        pre = "sh" if code.startswith('6') else "sz"
        r = requests.get(f"http://qt.gtimg.cn/q={pre}{code}", timeout=refresh_rate/2)
        p = r.text.split('~')
        return {'最新价':p[3], '成交量':p[6], 
                '买盘':pd.DataFrame([{'价格':p[9+i*2], '数量':p[10+i*2]} for i in range(5)]),
                '卖盘':pd.DataFrame([{'价格':p[19+i*2], '数量':p[20+i*2]} for i in range(5)])}
    except: return None

if trading:
    data = fetch_data(target_code)
    if data:
        res = institutional_kernel(data, data['买盘'], data['卖盘'])
        
        # UI: 第一排 - 执行核心
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("执行仓位 %", f"{res['pos_percent']:.0f}%", f"投放 ¥{total_capital*res['pos_percent']/100:,.0f}")
        c2.metric("动态止损 (p_stop)", f"¥{res['p_stop']:.2f}")
        c3.metric("支撑/压力", f"¥{res['p_sup']:.2f} / ¥{res['p_res']:.2f}")
        c4.metric("流通性厚度", f"{res['liq_idx']:.2f}%")

        st.divider()

        # UI: 第二排 - 评分仪表盘
        l, r = st.columns(2)
        with l:
            st.write("🌲 **买方审计评分**")
            st.progress(min(res['b_score']/100, 1.0), text=f"Score: {int(res['b_score'])}")
            st.metric("买盘真实熵", f"{res['bid_ent']:.2f}", "真实承接" if res['bid_ent']>1.2 else "托单嫌疑")
        with r:
            st.write("🔥 **卖方审计评分**")
            st.progress(min(res['s_score']/100, 1.0), text=f"Score: {int(res['s_score'])}")
            st.metric("卖盘拦截熵", f"{res['ask_ent']:.2f}", "抛压分散" if res['ask_ent']>1.2 else "拦截嫌疑")

        st.divider()
        st.write(f"📈 **资金动量 (CVD):** {res['cvd_t']:.4f} | **最新价:** ¥{res['curr_p']}")
        if len(st.session_state.cvd_history) > 2:
            st.line_chart(st.session_state.cvd_history[-30:])

        with st.expander("👁️ 盘口意图审计细节", expanded=True):
            col_a, col_b = st.columns(2)
            with col_a:
                st.write("卖盘审计 (Ask)")
                df_a = data['卖盘'].iloc[::-1].copy()
                df_a['意图审计'] = res['ask_labels'][::-1]
                st.table(df_a)
            with col_b:
                st.write("买盘审计 (Bid)")
                df_b = data['买盘'].copy()
                df_b['意图审计'] = res['bid_labels']
                st.table(df_b)

    time.sleep(refresh_rate); st.rerun()
else:
    st.warning(f"🚨 内核挂起: {trade_msg}")
