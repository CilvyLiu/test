import os
import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timedelta, timezone

# ===================== 0. 环境底座与时间门禁 =====================
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
        st.toast(f"🏛️ v11.8 终极全功能内核挂载: {target_code}")

def safe_float(x, default=0.0):
    try: return float(str(x).replace(',', ''))
    except: return default

# ===================== 1. 数理工具箱 =====================
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

# ===================== 2. 核心审计内核 =====================
def institutional_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-100:]
    
    bid_v_list = df_bids['数量'].apply(safe_float).values
    ask_v_list = df_asks['数量'].apply(safe_float).values
    bid_p_list = df_bids['价格'].apply(safe_float).values
    ask_p_list = df_asks['价格'].apply(safe_float).values
    
    bid_v_total, ask_v_total = bid_v_list.sum(), ask_v_list.sum()
    imbalance = (bid_v_total - ask_v_total) / (bid_v_total + ask_v_total + 1e-9)
    st.session_state.imb_history.append(imbalance)
    
    # 逻辑复原：高阶参数
    alpha, dyn_thresh, slope_bp, cvd_trend, vol_idx = get_market_metrics(
        st.session_state.price_history, st.session_state.imb_history, st.session_state.cvd_history
    )
    
    st.session_state.cvd = (1 - alpha) * st.session_state.cvd + alpha * (bid_v_total - ask_v_total)
    st.session_state.cvd_history.append(st.session_state.cvd)
    
    ask_ent = calculate_entropy(ask_v_list)
    bid_ent = calculate_entropy(bid_v_list)
    
    # 逻辑复原：支撑/阻力/止损
    p_sup = np.percentile(st.session_state.price_history[-30:], 20) if len(st.session_state.price_history)>=30 else curr_p
    p_res = np.average(ask_p_list, weights=ask_v_list) if ask_v_total > 0 else curr_p
    p_stop = p_sup * 0.995 

    # 逻辑复原：买/卖评分矩阵
    b_score = 0
    if curr_p > p_stop:
        if curr_p <= p_sup * 1.003: b_score += 20
        if imbalance > dyn_thresh: b_score += 20
        if slope_bp > 0: b_score += 20
        if cvd_trend > 0: b_score += 20
        if bid_ent > 1.2: b_score += 20 

    s_score = 0
    if curr_p >= p_res * 0.997:
        s_score += 20
        if imbalance < -dyn_thresh: s_score += 20
        if cvd_trend < 0 and slope_bp > 0: s_score += 40 
        if ask_ent < 0.8: s_score -= 30 

    # 逻辑复原：仓位管理
    vol_adj = np.clip(1 - vol_idx * 100, 0.5, 1.0)
    pos_percent = 0
    if b_score >= 80: pos_percent = 80 * vol_adj
    elif b_score >= 60: pos_percent = 40 * vol_adj
    if s_score >= 80: pos_percent = -100 
    elif s_score >= 60: pos_percent = -50  

    # 逻辑合并：执行点位逻辑
    if bid_ent < 1.0:
        bid_audit_msg = "⚠️ 虚假托单：避开诱多"
        p_entry = bid_p_list[2]
    elif imbalance > 0.3:
        bid_audit_msg = "💎 真实支撑"
        p_entry = bid_p_list[0]
    else:
        bid_audit_msg = "⚖️ 承接中性"
        p_entry = bid_p_list[1]

    p_tp = ask_p_list[0] + 0.01 if ask_ent < 1.1 else ask_p_list[0]

    return {
        "p_tp": p_tp, "p_entry": p_entry, "p_stop": p_stop, "curr_p": curr_p,
        "b_score": b_score, "s_score": s_score, "pos_percent": pos_percent,
        "ask_ent": ask_ent, "bid_ent": bid_ent, "cvd_t": cvd_trend, "bid_audit_msg": bid_audit_msg
    }

# ===================== 3. UI 复原 =====================
st.set_page_config(page_title="Nova Institutional Vision v11.8", layout="wide")
trading, trade_msg = is_trade_time()

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
    st.title("🏛️ Vault v11.8")
    target_code = st.text_input("代码", value="601898")
    init_vault(target_code)
    st.info(f"审计状态: {trade_msg}")
    if st.button("RESET"): st.session_state.clear(); st.rerun()

if trading:
    data = fetch_data(target_code)
    if data:
        res = institutional_kernel(data, data['买盘'], data['卖盘'])
        
        # UI复原：核心监控区
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("执行建议(仓位)", f"{res['pos_percent']:.0f}%")
        c2.metric("抄底点位", f"¥{res['p_entry']:.2f}", res['bid_audit_msg'])
        c3.metric("止盈点位", f"¥{res['p_tp']:.2f}", "拦截突破" if res['ask_ent'] < 1.1 else "常规压力")
        c4.metric("动态止损", f"¥{res['p_stop']:.2f}")

        st.divider()

        # UI复原：评分仪表盘与趋势
        l, r = st.columns(2)
        with l:
            st.write("🌲 **买方多维意图评分**")
            st.progress(min(res['b_score']/100, 1.0), text=f"Score: {int(res['b_score'])}")
            st.metric("买盘真实度 (熵)", f"{res['bid_ent']:.2f}", "真实" if res['bid_ent'] > 1.2 else "量化托单")
        with r:
            st.write("🔥 **卖方意图与背离审计**")
            st.progress(min(res['s_score']/100, 1.0), text=f"Score: {int(res['s_score'])}")
            st.metric("卖盘拦截度 (熵)", f"{res['ask_ent']:.2f}", "拦截严重" if res['ask_ent'] < 1.1 else "抛压分散")

        st.divider()
        st.write(f"📈 **资金动量 (CVD Trend):** {res['cvd_t']:.4f} | **当前对冲价:** ¥{res['curr_p']}")

        # UI复原：细节审计
        with st.expander("👁️ 盘口深度与量化标签细节"):
            col_a, col_b = st.columns(2)
            with col_a:
                df_a = data['卖盘'].iloc[::-1].copy()
                df_a['意图'] = df_a['数量'].apply(lambda x: "🛑 拦路虎" if safe_float(x) > 500 and res['ask_ent'] < 1.1 else "")
                st.table(df_a)
            with col_b:
                df_b = data['买盤'].copy() if '买盤' in data else data['买盘']
                df_b['意图'] = df_b['数量'].apply(lambda x: "🛡️ 诱多托单" if safe_float(x) > 500 and res['bid_ent'] < 1.0 else "")
                st.table(df_b)

    time.sleep(5)
    st.rerun()
else:
    st.warning(f"🚨 内核已休眠: {trade_msg}")
