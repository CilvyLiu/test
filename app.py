import os
import streamlit as st
import efinance as ef
import pandas as pd
import numpy as np
import time
from datetime import datetime

# --- [环境适配] ---
os.environ["EFINANCE_DATA_DIR"] = "/tmp/efinance" 

# --- [1. 核心状态锁：确保 session_state 绝对稳定] ---
def init_gringotts_vault():
    state_defaults = {
        "support_cache": [],   # 支撑价格滑动窗口
        "score_cache": [],     # 评分稳定性窗口
        "rebound_cache": [],   # 存储格式: (timestamp, price)
        "prev_vol": 0.0,       # 上一次累计成交量
        "hit_support": False,  # 是否触碰过支撑线
        "cooldown_until": 0.0, # 风险冷却截止时间
        "last_update": ""      # 最后审计时间
    }
    for key, value in state_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

init_gringotts_vault()

# --- [2. 专家级审计逻辑：时间戳与容差加固] ---
def gringotts_kernel(quote, df_bids):
    curr_p = float(quote['最新价'])
    curr_time = time.time()
    
    # A. 分散托盘识别 (前三档加权)
    top_bids = df_bids.head(3).copy()
    top_bids['pf'] = top_bids['价格'].apply(lambda x: float(x) if x != '-' else curr_p)
    top_bids['vf'] = top_bids['数量'].apply(lambda x: float(x) if x != '-' else 0)
    
    p_sup = np.average(top_bids['pf'], weights=top_bids['vf']) if top_bids['vf'].sum() > 0 else curr_p
    
    # B. 稳定性审计 (0.02 容差)
    st.session_state.support_cache.append(p_sup)
    st.session_state.support_cache = st.session_state.support_cache[-5:]
    is_stable = (max(st.session_state.support_cache) - min(st.session_state.support_cache)) <= 0.02 if len(st.session_state.support_cache) >= 3 else False
    
    # C. 资金流向审计 (累计量差值过滤)
    curr_vol = float(quote['成交量'])
    v_delta = curr_vol - st.session_state.prev_vol
    st.session_state.prev_vol = curr_vol
    # 过滤掉非交易时段的异常跳值
    actual_v_delta = v_delta if 100 < v_delta < 1000000 else 0
    
    # D. 9秒时间审计 (基于真实时间戳)
    is_time_confirmed = False
    if curr_p <= p_sup * 1.002:
        st.session_state.hit_support = True
    
    if st.session_state.hit_support:
        st.session_state.rebound_cache.append((curr_time, curr_p))
        # 只保留 60 秒内的尝试记录
        st.session_state.rebound_cache = [x for x in st.session_state.rebound_cache if curr_time - x[0] <= 60]
        
        if len(st.session_state.rebound_cache) >= 3:
            dur = st.session_state.rebound_cache[-1][0] - st.session_state.rebound_cache[0][0]
            # 持续 9 秒且最低回踩点未有效击穿
            if dur >= 9 and min([x[1] for x in st.session_state.rebound_cache]) > p_sup * 0.99:
                is_time_confirmed = True

    # E. 止损冷却保护 (跌破2%判定防御失败)
    if curr_p < p_sup * 0.98:
        st.session_state.hit_support = False
        st.session_state.rebound_cache = []
        st.session_state.cooldown_until = curr_time + 300 # 封盘5分钟

    # F. 结构化评分 (3:3:4)
    s_score = 30 if is_stable else 0
    f_score = 30 if actual_v_delta > 500 else 0
    t_score = 40 if is_time_confirmed else 0
    
    total_score = s_score + f_score + t_score
    st.session_state.score_cache.append(total_score)
    st.session_state.score_cache = st.session_state.score_cache[-3:]
    
    # 信号平滑：连续3次稳定高分
    is_score_stable = len(st.session_state.score_cache) == 3 and min(st.session_state.score_cache) >= 70
    
    return round(p_sup, 2), total_score, is_score_stable, (s_score, f_score, t_score)

# --- [3. UI 层：生产级看板布局] ---
st.set_page_config(page_title="Gringotts v5.0 Pro", layout="wide")
st.title("🏦 古灵阁 (Gringotts) 资产审计内核")

# 侧边栏配置
with st.sidebar:
    st.header("金库配置")
    target_code = st.text_input("股票代码", value="002415")
    capital = st.number_input("拟投入金额", value=100000)
    refresh_rate = st.slider("同步频率(秒)", 2, 10, 3)
    # 使用按钮手动刷新或通过外部组件实现 autorefresh
    do_refresh = st.button("🔄 同步最新审计结果")

# 获取数据
try:
    df = ef.stock.get_realtime_quotes(target_code)
    quote = df.iloc[0]
    curr_p = float(quote['最新价'])
    
    # 整理买卖盘 (此处df_asks可用于后续“压力审计”扩展)
    bids = pd.DataFrame([{'价格':quote[f'买价{i}'], '数量':quote[f'买量{i}']} for i in range(1,6)])
    
    # 运行审计
    p_sup, score, s_stable, sub_s = gringotts_kernel(quote, bids)
    
    # 渲染顶部指标
    col1, col2, col3 = st.columns([1, 2, 1])
    col1.metric("报价行情", f"¥{curr_p}", f"{quote['涨跌幅']}%")
    
    # 冷却状态检查
    if time.time() < st.session_state.cooldown_until:
        col2.error(f"🚫 风险防御已激活：锁定至 {datetime.fromtimestamp(st.session_state.cooldown_until).strftime('%H:%M:%S')}")
    else:
        color = "#00ff00" if s_stable else ("#ffff00" if score >= 40 else "#ff4b4b")
        col2.markdown(f"<h1 style='text-align:center; color:{color};'>意图评分: {score}</h1>", unsafe_allow_html=True)
        if s_stable: col2.markdown("<p style='text-align:center;'>✅ 信号稳定性已确认</p>", unsafe_allow_html=True)

    col3.metric("防御基准线", f"¥{p_sup}")

    st.divider()

    # 决策分级建议
    pos_l, pos_r = st.columns(2)
    with pos_l:
        st.subheader("💰 仓位映射建议")
        if s_stable:
            pos, status = 0.4, "🔥 建议重仓压仓"
        elif score >= 60:
            pos, status = 0.2, "🟡 建议试探建仓"
        elif score >= 40:
            pos, status = 0.1, "🔵 极轻仓观察"
        else:
            pos, status = 0.0, "⚪ 保持观望"
        
        st.markdown(f"### {status}")
        st.markdown(f"## 建议入场: <span style='color:cyan'>¥{capital*pos:,.0f}</span>", unsafe_allow_html=True)

    with pos_r:
        st.subheader("📝 结构化审计日志")
        st.write(f"· 盘口结构评分 (稳定性): {sub_s[0]}/30")
        st.write(f"· 资金流向评分 (增量): {sub_s[1]}/30")
        st.write(f"· 9秒回踩验证评分 (时间): {sub_s[2]}/40")
        if st.session_state.hit_support and not sub_s[2]:
            st.info("⏳ 监控中：已进入支撑区，等待 9 秒稳定性确认...")

except Exception as e:
    st.error(f"古灵阁连接中断: {e}")

st.caption(f"系统运行中 | 环境: {os.environ.get('EFINANCE_DATA_DIR')} | 更新时间: {datetime.now().strftime('%H:%M:%S')}")
