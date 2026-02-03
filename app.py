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

# ===================== 1. 高阶数理工具箱 (v14.0 增强版) =====================
def calculate_zema(data, period=10):
    """Zero Lag Exponential Moving Average - 消除量化常见的均线滞后"""
    ema1 = pd.Series(data).ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    return (ema1 + (ema1 - ema2)).iloc[-1]

def calculate_zvwap(prices, volumes):
    """Zero Lag VWAP - 判定机构真实的持仓成本重心"""
    prices, volumes = np.array(prices), np.array(volumes)
    typical_p = prices 
    v_cum = volumes.cumsum()
    pv_cum = (typical_p * volumes).cumsum()
    vwap = pv_cum / (v_cum + 1e-9)
    # 引入零滞后修正
    vwap_ema = pd.Series(vwap).ewm(span=10).mean()
    return (vwap * 2 - vwap_ema).iloc[-1]

def get_market_sentiment(quote):
    """提取基础情绪指标：量比、换手率"""
    v_ratio = safe_float(quote.get('量比', 1.0))
    turnover = safe_float(quote.get('换手率', 0.0))
    return v_ratio, turnover

# ===================== UI 侧边栏交互补全 =====================
with st.sidebar:
    st.title("🏛️ Vault v13.9")
    target_code = st.text_input("代码", value="601898")
    total_capital = st.number_input("总投放金额 (CNY)", value=100000)
    refresh_rate = st.slider("审计刷新频率 (秒)", 1, 10, 3)
    init_vault(target_code)
    st.info(f"审计状态: {is_trade_time()[1]}")
    if st.button("RESET"): st.session_state.clear(); st.rerun()
# --- 补在此处 ---
def fetch_data(code):
    try:
        pre = "sh" if code.startswith('6') else "sz"
        # 实时请求腾讯接口
        r = requests.get(f"http://qt.gtimg.cn/q={pre}{code}", timeout=1.5)
        p = r.text.split('~')
        # 核心：必须抓取完整的五档挂单数据
        return {
            '最新价': p[3], '成交量': p[6], '量比': p[45] if len(p)>45 else 1.0,
            '买盘': pd.DataFrame([{'价格':p[9+i*2], '数量':p[10+i*2]} for i in range(5)]),
            '卖盘': pd.DataFrame([{'价格':p[19+i*2], '数量':p[20+i*2]} for i in range(5)])
        }
    except: return None
# --- 补在此处结束 ---
# ===================== 2. 核心审计内核 (高阶逻辑) =====================
def institutional_kernel(quote, df_bids, df_asks):
    # 2.1 基础盘口数据提取
    curr_p = safe_float(quote['最新价'])
    bid_v, ask_v = df_bids['数量'].apply(safe_float).values * 100, df_asks['数量'].apply(safe_float).values * 100
    bid_p, ask_p = df_bids['价格'].apply(safe_float).values, df_asks['价格'].apply(safe_float).values
    
    # 2.2 委比 & 委差 (实时意图：衡量量化对冲压制力)
    total_bid_v, total_ask_v = bid_v.sum(), ask_v.sum()
    weicha = total_bid_v - total_ask_v  # 委差
    weibi = (weicha / (total_bid_v + total_ask_v + 1e-9)) * 100 # 委比
    
    # 2.3 ZEMA & ZVWAP 动态基准
    zema = calculate_zema(st.session_state.price_history)
    zvwap = calculate_zvwap(st.session_state.price_history, st.session_state.imb_history) # 模拟量加权
    
    # 2.4 极端价格预测 (情绪动态模型)
    # 最抄底价：基于 ZVWAP 的负偏离 + 委比支撑
    p_floor = min(bid_p) * (1 - (abs(weibi)/1000)) if weibi < -20 else bid_p[-1]
    # 极度获利位：基于 ZEMA 的正偏离 + CVD 动量
    cvd_t = st.session_state.cvd_history[-1] if st.session_state.cvd_history else 0
    p_peak = max(ask_p) * (1 + (cvd_t/1e8)) if cvd_t > 0 else ask_p[-1]

    # 2.5 买入/卖出评分时机 (Trader Logic)
    b_score = 0
    if curr_p <= zvwap and weibi > 10: b_score += 50  # 价格在重心下方且买盘占优
    if cvd_t > 0 and zema > curr_p: b_score += 50    # 动量反转触发
    
    s_score = 0
    if curr_p >= zema and weibi < -10: s_score += 50 # 价格超涨且卖盘拦截
    if total_ask_v > total_bid_v * 1.5: s_score += 50 # 极端拦截压制

    return {
        "p_floor": p_floor, "p_peak": p_peak, "zvwap": zvwap, "zema": zema,
        "weibi": weibi, "weicha": weicha, "b_score": b_score, "s_score": s_score,
        "curr_p": curr_p, "pos_percent": 80 if b_score > 80 else 0
    }
    # UI: 第一排 - 极端位与成本重心
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("抄底建议位", f"¥{res['p_floor']:.2f}", "最强支撑")
        c2.metric("极度获利位", f"¥{res['p_peak']:.2f}", "警惕回落")
        c3.metric("ZVWAP 重心", f"¥{res['zvwap']:.2f}")
        c4.metric("委比 / 委差", f"{res['weibi']:.1f}%", f"{int(res['weicha'])}")

        st.divider()

        # UI: 动量审计行
        st.write(f"🛡️ **ZEMA 基准:** ¥{res['zema']:.2f} | **当前获利空间:** {((res['p_peak']/res['curr_p']-1)*100):.2f}%")
# ===================== 3. 执行引擎 (核心驱动) =====================
st.set_page_config(page_title="Vault v14.0", layout="wide")

if is_trade_time()[0]:
    data = fetch_data(target_code)
    if data:
        # 1. 压入价格历史用于 ZEMA 计算
        st.session_state.price_history.append(safe_float(data['最新价']))
        st.session_state.price_history = st.session_state.price_history[-100:]
        # 模拟 IMB 历史用于 ZVWAP 权重
        st.session_state.imb_history.append(safe_float(data['成交量']))
        st.session_state.imb_history = st.session_state.imb_history[-100:]
        
        # 2. 运行审计内核
        res = institutional_kernel(data, data['买盘'], data['卖盘'])
        
        # 3. 渲染 UI 第一排：极端位与成本重心
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("抄底建议位", f"¥{res['p_floor']:.2f}", "最强支撑")
        c2.metric("极度获利位", f"¥{res['p_peak']:.2f}", "警惕回落")
        c3.metric("ZVWAP 重心", f"¥{res['zvwap']:.2f}")
        c4.metric("委比 / 委差", f"{res['weibi']:.1f}%", f"{int(res['weicha'])}")

        st.divider()

        # 4. 渲染 UI 第二排：评分时机与 ZEMA 偏离
        l, r = st.columns(2)
        with l:
            st.write("🌲 **买入审计评分**")
            st.progress(res['b_score']/100)
            st.write(f"评分原因：{'重合 ZVWAP' if res['b_score']>0 else '观望'}")
        with r:
            st.write("🔥 **卖出审计评分**")
            st.progress(res['s_score']/100)
            st.write(f"评分原因：{'触发 ZEMA 压力' if res['s_score']>0 else '持有'}")

        st.write(f"🛡️ **ZEMA 基准:** ¥{res['zema']:.2f} | **当前获利空间:** {((res['p_peak']/res['curr_p']-1)*100):.2f}%")

    time.sleep(refresh_rate)
    st.rerun()
else:
    st.warning(f"🚨 内核挂起: {is_trade_time()[1]}")
