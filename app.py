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
    st.title("🏛️ Gringotts v13.9")
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

    # 2.5 盘口厚度与意图审计 (核心：穿透量化挂单)
    avg_bid_v, avg_ask_v = np.mean(bid_v), np.mean(ask_v)
    
    def get_intent(v, avg_v, side):
        if v > avg_v * 3: return "🛑 拦截大单" if side=='ask' else "🛡️ 强力托单"
        if v < avg_v * 0.2: return "🪶 微量探测"
        return "稳定"

    # 生成意图标签
    ask_intents = [get_intent(v, avg_ask_v, 'ask') for v in ask_v]
    bid_intents = [get_intent(v, avg_bid_v, 'bid') for v in bid_v]
    
    # 盘口厚度 (Total Depth Amount)
    bid_depth = np.sum(bid_v * bid_p)
    ask_depth = np.sum(ask_v * ask_p)

    # 2.5 买入/卖出评分与原因审计 (补全逻辑)
    b_score = 0
    b_reasons = []
    if curr_p <= zvwap and weibi > 10: 
        b_score += 50
        b_reasons.append("⚖️ 低于重心+强力托单")
    if cvd_t > 0 and zema > curr_p: 
        b_score += 50
        b_reasons.append("🔄 动量翻红+ZEMA支撑")
    
    s_score = 0
    s_reasons = []
    if curr_p >= zema and weibi < -10: 
        s_score += 50
        s_reasons.append("🛑 压力拦截+委比较差")
    if total_ask_v > total_bid_v * 1.5: 
        s_score += 50
        s_reasons.append("🔥 极端压制")

    # 生成最终审计线索
    b_msg = " | ".join(b_reasons) if b_reasons else "🔭 盘口静默中"
    s_msg = " | ".join(s_reasons) if s_reasons else "🟢 暂无压制"

    return {
        "p_floor": p_floor, "p_peak": p_peak, "zvwap": zvwap, "zema": zema,
        "weibi": weibi, "weicha": weicha, "b_score": b_score, "s_score": s_score,
        "curr_p": curr_p, "bid_depth": bid_depth, "ask_depth": ask_depth,
        "ask_intents": ask_intents, "bid_intents": bid_intents,
        "b_msg": b_msg, "s_msg": s_msg  # <--- 必须补齐这两行
    }
# ===================== 3. 执行引擎 (核心驱动) =====================
st.set_page_config(page_title="Gringotts v14.0", layout="wide")

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
        
        # 第一排：价格与极端位 (高亮显示)
        # 1. 计算获利潜能与视觉标记
        profit_space = (res['p_peak'] / res['curr_p'] - 1) * 100
        space_color = "🟢" if profit_space > 0 else "🔴"
        
        # 2. 增强型标题显示
        st.subheader(f"📊 当前价格: ¥{res['curr_p']} | {space_color} 获利空间: {profit_space:.2f}%")
        # 3. 第一排核心指标
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("最低吸入位", f"¥{res['p_floor']:.2f}", "抄底点", delta_color="normal")
        c2.metric("最高获利位", f"¥{res['p_peak']:.2f}", "止盈点", delta_color="inverse")
        c3.metric("机构成本 (ZVWAP)", f"¥{res['zvwap']:.2f}")
        c4.metric("委比 / 委差", f"{res['weibi']:.1f}%", f"{int(res['weicha'])}")
        
        st.divider()

        # 第二排：双向评分与厚度显示
        l, r = st.columns(2)
        with l:
            st.write(f"🌲 **买入评分: {res['b_score']} / 100** | 承接厚度: ¥{res['bid_depth']:,.0f}")
            st.progress(res['b_score']/100)
            # 这里是你要的买入原因
            st.success(f"审计线索: {res['b_msg']}") 
            
        with r:
            st.write(f"🔥 **卖出评分: {res['s_score']} / 100** | 压制厚度: ¥{res['ask_depth']:,.0f}")
            st.progress(res['s_score']/100)
            # 这里是卖出原因
            st.warning(f"审计线索: {res['s_msg']}")

        st.write(f"🛡️ **ZEMA 基准:** ¥{res['zema']:.2f} | **当前获利空间:** {((res['p_peak']/res['curr_p']-1)*100):.2f}%")
# --- 修正后的意图审计细节表格 ---
        with st.expander("👁️ 盘口意图与挂单审计", expanded=True):
            col_a, col_b = st.columns(2)
            with col_a:
                st.write("卖方盘口 (Ask)")
                df_a = data['卖盘'].iloc[::-1].copy()
                # 确保 kernel 返回了 ask_intents
                df_a['意图审计'] = res['ask_intents'][::-1]
                st.table(df_a)
            with col_b:
                st.write("买方盘口 (Bid)")
                df_b = data['买盘'].copy()
                # 确保 kernel 返回了 bid_intents
                df_b['意图审计'] = res['bid_intents']
                st.table(df_b)
    time.sleep(refresh_rate)
    st.rerun()
else:
    st.warning(f"🚨 内核挂起: {is_trade_time()[1]}")
