import streamlit as st
import efinance as ef
import pandas as pd
import numpy as np
import time
from datetime import datetime

# --- 1. 显式初始化：防止逻辑歧义 ---
if "support_cache" not in st.session_state: st.session_state.support_cache = []
if "score_cache" not in st.session_state: st.session_state.score_cache = []
if "rebound_cache" not in st.session_state: st.session_state.rebound_cache = [] # 存储 (time, price)
if "prev_vol" not in st.session_state: st.session_state.prev_vol = 0
if "hit_support" not in st.session_state: st.session_state.hit_support = False
if "cooldown_until" not in st.session_state: st.session_state.cooldown_until = 0

def safe_float(x, default=0.0):
    try:
        if x in ['-', '--', None, '', 'None']: return default
        return float(x)
    except: return default

# --- 2. 专家级审计内核 ---
def gringotts_kernel_v5(quote, df_asks, df_bids):
    curr_p = safe_float(quote['最新价'])
    curr_time = time.time()
    
    # A. 盘口结构审计 (Structure Score)
    top_bids = df_bids.head(3).copy()
    top_bids['pf'] = top_bids['价格'].apply(safe_float)
    top_bids['vf'] = top_bids['数量'].apply(safe_float)
    
    # 漏洞1修复：加权均价作为防御线
    p_sup = np.average(top_bids['pf'], weights=top_bids['vf']) if top_bids['vf'].sum() > 0 else curr_p
    
    # 漏洞5修复：0.01 容差判定稳定性
    st.session_state.support_cache.append(p_sup)
    st.session_state.support_cache = st.session_state.support_cache[-5:]
    is_stable = (max(st.session_state.support_cache) - min(st.session_state.support_cache)) <= 0.02 if len(st.session_state.support_cache) >= 3 else False
    
    # B. 资金流向审计 (Flow Score)
    # 漏洞4修复：当日累计成交量阈值过滤
    curr_vol = safe_float(quote['成交量'])
    v_delta = curr_vol - st.session_state.prev_vol
    st.session_state.prev_vol = curr_vol
    # 过滤掉异常的极小增量或负值（清算导致）
    actual_v_delta = v_delta if 100 < v_delta < 50000 else 0
    
    # C. 时间维度审计 (Time Score)
    # 漏洞3修复：基于真实时间戳的回踩确认
    is_time_confirmed = False
    if curr_p <= p_sup * 1.002:
        st.session_state.hit_support = True
    
    if st.session_state.hit_support:
        st.session_state.rebound_cache.append((curr_time, curr_p))
        st.session_state.rebound_cache = [x for x in st.session_state.rebound_cache if curr_time - x[0] <= 30] # 只留30秒内数据
        
        if len(st.session_state.rebound_cache) >= 3:
            time_diff = st.session_state.rebound_cache[-1][0] - st.session_state.rebound_cache[0][0]
            # 必须持续超过 9 秒且价格未刺穿支撑
            if time_diff >= 9 and min([x[1] for x in st.session_state.rebound_cache]) > p_sup * 0.995:
                is_time_confirmed = True

    # 漏洞6修复：多点回撤保护，非暴力清空
    if curr_p < p_sup * 0.98: # 跌破2%判定彻底失败
        st.session_state.hit_support = False
        st.session_state.rebound_cache = []
        st.session_state.cooldown_until = curr_time + 300 # 冷却5分钟

    # D. 结构化评分系统
    s_score = 30 if is_stable else 0 # 盘口分
    f_score = 30 if actual_v_delta > 500 else 0 # 资金分
    t_score = 40 if is_time_confirmed else 0 # 时间分
    
    total_score = s_score + f_score + t_score
    return round(p_sup, 2), total_score, actual_v_delta, (s_score, f_score, t_score)

# --- 3. UI 界面 ---
st.set_page_config(page_title="古灵阁 Gringotts v5.0", layout="wide")
st.sidebar.title("🏦 古灵阁实战柜台")
target_code = st.sidebar.text_input("代码", value="002415")
capital = st.sidebar.number_input("拟压仓资金", value=100000)

# 替代 While True 的刷新机制
if st.sidebar.button("同步最新审计数据"):
    st.rerun()

# --- 运行逻辑 ---
try:
    df = ef.stock.get_realtime_quotes(target_code)
    quote = df.iloc[0]
    curr_p = safe_float(quote['最新价'])
    
    # 漏洞2修复：传入完整的买卖盘
    asks = pd.DataFrame([{'价格':safe_float(quote[f'卖价{i}']), '数量':safe_float(quote[f'卖量{i}'])} for i in range(1,6)])
    bids = pd.DataFrame([{'价格':safe_float(quote[f'买价{i}']), '数量':safe_float(quote[f'买量{i}'])} for i in range(1,6)])
    
    p_sup, score, v_delta, sub_scores = gringotts_kernel_v5(quote, asks, bids)
    
    # 渲染界面
    c1, c2, c3 = st.columns([1, 2, 1])
    c1.metric("现价", f"¥{curr_p}", f"{quote['涨跌幅']}%")
    
    # 冷却期显示
    if time.time() < st.session_state.cooldown_until:
        c2.error(f"🛡️ 古灵阁冷却中：支撑被击穿，锁定至 {datetime.fromtimestamp(st.session_state.cooldown_until).strftime('%H:%M:%S')}")
    else:
        score_color = "green" if score >= 70 else "yellow"
        c2.markdown(f"<h1 style='text-align: center; color: {score_color};'>意图评分: {score}</h1>", unsafe_allow_html=True)

    st.divider()
    
    # 结构化评分明细
    sc1, sc2, sc3 = st.columns(3)
    sc1.write(f"📊 盘口结构分: {sub_scores[0]}/30")
    sc2.write(f"💧 资金增量分: {sub_scores[1]}/30")
    sc3.write(f"⏳ 时间验证分: {sub_scores[2]}/40")
    
    # 仓位映射
    if score >= 70:
        st.success(f"🔥 压仓指令确认：建议投入 ¥{capital * 0.4:,.0f} (40%)")
    elif score >= 40:
        st.warning(f"🟡 试探信号：建议投入 ¥{capital * 0.1:,.0f} (10%)")
    else:
        st.info("⚪ 观望：金库防御中，等待稳定信号。")

except Exception as e:
    st.error(f"连接异常: {e}")
