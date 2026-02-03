import os
import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timedelta, timezone

# ===================== 0. 环境底座 =====================
def init_vault(target_code):
    if "current_code" not in st.session_state or st.session_state.current_code != target_code:
        st.session_state.current_code = target_code
        st.session_state.price_history = []
        st.session_state.imb_history = []
        st.session_state.cvd_history = []
        st.session_state.cvd = 0.0
        st.toast(f"🏛️ v10.8 挂单决策内核已上线: {target_code}")

def safe_float(x, default=0.0):
    try: return float(str(x).replace(',', ''))
    except: return default

# ===================== 1. 数理内核 =====================
def calculate_entropy(volumes):
    probs = volumes / (sum(volumes) + 1e-9)
    return -np.sum(probs * np.log(probs + 1e-9))

def get_market_metrics(prices, imbs, cvds):
    if len(prices) < 15: return 0.2, 0.2, 0.0, 0.0, 0.0
    change = abs(prices[-1] - prices[-10])
    vol = sum(abs(np.diff(prices[-10:]))) + 1e-9
    alpha = np.clip((change / vol) * 0.4 + 0.1, 0.1, 0.5)
    imb_thresh = np.std(imbs) * 2.0 if len(imbs) > 10 else 0.2
    slope_bp = (np.polyfit(np.arange(len(prices[-10:])), prices[-10:], 1)[0]) / (prices[-1] + 1e-9)
    cvd_trend = np.polyfit(np.arange(len(cvds[-10:])), cvds[-10:], 1)[0] if len(cvds) >= 10 else 0
    atr_sim = np.std(np.diff(prices[-20:])) / (prices[-1] + 1e-9) if len(prices) >= 20 else 0.001
    return alpha, imb_thresh, slope_bp, cvd_trend, atr_sim

# ===================== 2. 审计内核 =====================
def audit_kernel(quote, df_bids, df_asks):
    curr_p = safe_float(quote['最新价'])
    st.session_state.price_history.append(curr_p)
    st.session_state.price_history = st.session_state.price_history[-50:]
    
    bid_v = df_bids['数量'].apply(safe_float).values
    ask_v = df_asks['数量'].apply(safe_float).values
    bid_p = df_bids['价格'].apply(safe_float).values
    ask_p = df_asks['价格'].apply(safe_float).values
    
    imbalance = (bid_v.sum() - ask_v.sum()) / (bid_v.sum() + ask_v.sum() + 1e-9)
    st.session_state.imb_history.append(imbalance)
    
    alpha, dyn_thresh, slope, cvd_t, vol = get_market_metrics(
        st.session_state.price_history, st.session_state.imb_history, st.session_state.cvd_history
    )
    
    st.session_state.cvd = (1 - alpha) * st.session_state.cvd + alpha * (bid_v.sum() - ask_v.sum())
    st.session_state.cvd_history.append(st.session_state.cvd)
    st.session_state.cvd_history = st.session_state.cvd_history[-50:]
    
    ask_ent = calculate_entropy(ask_v)
    
    # --- 精确点位计算 ---
    # 止盈挂高价格：如果分布熵极低（量化拦截），建议挂在卖一上方 1-2个 Tick 等待突破扫盘
    if ask_ent < 1.1:
        p_tp = ask_p[0] + 0.01 
        tp_tag = "🚀 拦截突破挂单"
    else:
        p_tp = ask_p[0] # 正常压力，卖一先行
        tp_tag = "💰 压力位先行离场"
        
    # 最低吸入抄底价：结合支撑位与斜率修正
    p_sup = np.percentile(st.session_state.price_history, 20)
    # 如果下跌趋势快(slope < 0)，挂单在买三买四附近抄底；否则挂在买二
    p_entry = min(bid_p[1], p_sup) if slope < -0.0001 else bid_p[0]
    
    return {
        "p_tp": p_tp, "tp_tag": tp_tag, "p_entry": p_entry, 
        "curr_p": curr_p, "ask_ent": ask_ent, "cvd_t": cvd_t, "imb": imbalance
    }

# ===================== 3. UI 面板 =====================
st.set_page_config(page_title="Nova Institutional Vision v10.8", layout="wide")

def fetch_data(code):
    try:
        pre = "sh" if code.startswith('6') else "sz"
        r = requests.get(f"http://qt.gtimg.cn/q={pre}{code}", timeout=1.5)
        p = r.text.split('~')
        return {'最新价':p[3], '卖盘':pd.DataFrame([{'价格':p[19+i*2], '数量':p[20+i*2]} for i in range(5)]),
                '买盘':pd.DataFrame([{'价格':p[9+i*2], '数量':p[10+i*2]} for i in range(5)])}
    except: return None

with st.sidebar:
    st.title("🏛️ Vault v10.8")
    target_code = st.text_input("股票代码", value="601898")
    init_vault(target_code)
    if st.button("RESET VAULT"): st.session_state.clear(); st.rerun()

data = fetch_data(target_code)
if data:
    res = audit_kernel(data, data['买盘'], data['卖盘'])
    
    # 第一排：Nova 挂单指令
    st.markdown("### ⚡ 交易执行实时指令")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.success("🧩 最低吸入抄底价")
        st.markdown(f"<h1 style='color:#00ff00;'>¥{res['p_entry']:.2f}</h1>", unsafe_allow_html=True)
        st.caption("策略：趋势补偿挂单 (抄底且必买到)")
    with col2:
        st.error("🎯 止盈最高挂单价")
        st.markdown(f"<h1 style='color:#ff4b4b;'>¥{res['p_tp']:.2f}</h1>", unsafe_allow_html=True)
        st.caption(f"逻辑：{res['tp_tag']}")
    with col3:
        st.info("📊 当前市场重心")
        st.markdown(f"<h1>¥{res['curr_p']:.2f}</h1>", unsafe_allow_html=True)
        st.caption("实时对冲最新价")

    st.divider()

    # 第二排：量化意图分析
    st.write("### 👁️ 对面量化审计")
    l, m, r = st.columns([2, 1, 2])
    
    with l:
        st.write("🔥 **卖盘抛压墙 (Ask Side)**")
        df_a = data['卖盘'].iloc[::-1].copy()
        df_a['数量'] = df_a['数量'].apply(safe_float)
        max_v = df_a['数量'].max()
        df_a['意图'] = df_a['数量'].apply(lambda x: "🛑 拦截大单" if x == max_v and x > 500 else " ")
        st.dataframe(df_a, use_container_width=True)
        st.progress(min(res['ask_ent']/1.6, 1.0), text=f"分布熵: {res['ask_ent']:.2f} (越低越假)")

    with m:
        st.metric("多空委比", f"{res['imb']*100:.1f}%")
        st.metric("资金动量趋势", "流入" if res['cvd_t'] > 0 else "流出")
        st.markdown("---")
        if res['ask_ent'] < 1.1:
            st.warning("⚠️ 发现诱空拦截")
        else:
            st.success("✅ 真实抛压结构")

    with r:
        st.write("🌲 **买盘承接墙 (Bid Side)**")
        df_b = data['买盤'].copy() if '买盤' in data else data['买盘']
        st.dataframe(df_b, use_container_width=True)
        st.caption("下方托单审计完成")

else:
    st.warning("数据链连接中...")

time.sleep(5)
st.rerun()
