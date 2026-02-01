import os
import sys
import time
import types
from pathlib import Path
from datetime import datetime
import pandas as pd
import numpy as np
import streamlit as st

# ===================== 0. 内存级拦截 (权限解) =====================
fake_home = Path("/tmp/gringotts_data")
fake_home.mkdir(parents=True, exist_ok=True)

if 'efinance.config' not in sys.modules:
    cfg = types.ModuleType('efinance.config')
    cfg.DATA_DIR = fake_home
    cfg.SEARCH_RESULT_CACHE_PATH = fake_home / "search_cache"
    cfg.MAX_CONNECTIONS = 10
    sys.modules['efinance.config'] = cfg

import efinance as ef

# ===================== 1. 状态初始化 =====================
def init_vault():
    state_keys = {
        "support_cache": [], "score_cache": [], "rebound_cache": [],
        "prev_vol": 0, "hit_support": False, "cooldown_until": 0
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

# ===================== 2. 审计引擎 =====================
def gringotts_kernel(quote, df_bids):
    curr_p = safe_float(quote['最新价'])
    curr_time = time.time()

    # ---- A. 盘口结构审计 ----
    top_bids = df_bids.head(3).copy()
    top_bids['pf'] = top_bids['价格'].apply(safe_float)
    top_bids['vf'] = top_bids['数量'].apply(safe_float)
    p_sup = np.average(top_bids['pf'], weights=top_bids['vf']) if top_bids['vf'].sum() > 0 else curr_p

    # ---- B. 支撑稳定性 ----
    st.session_state.support_cache.append(p_sup)
    st.session_state.support_cache = st.session_state.support_cache[-5:]
    is_stable = (max(st.session_state.support_cache) - min(st.session_state.support_cache)) <= 0.02 if len(st.session_state.support_cache) >= 3 else False

    # ---- C. 资金流向审计 ----
    curr_vol = safe_float(quote['成交量'])
    v_delta = curr_vol - st.session_state.prev_vol
    st.session_state.prev_vol = curr_vol
    actual_v_delta = v_delta if 100 < v_delta < 50000 else 0

    # ---- D. 时间回踩确认 ----
    is_time_confirmed = False
    if curr_p <= p_sup * 1.002:
        st.session_state.hit_support = True

    if st.session_state.hit_support:
        st.session_state.rebound_cache.append((curr_time, curr_p))
        st.session_state.rebound_cache = [x for x in st.session_state.rebound_cache if curr_time - x[0] <= 30]
        if len(st.session_state.rebound_cache) >= 3:
            time_diff = st.session_state.rebound_cache[-1][0] - st.session_state.rebound_cache[0][0]
            if time_diff >= 9 and min([x[1] for x in st.session_state.rebound_cache]) > p_sup * 0.995:
                is_time_confirmed = True

    # ---- E. 保护机制 ----
    if curr_p < p_sup * 0.98:
        st.session_state.hit_support = False
        st.session_state.rebound_cache = []
        st.session_state.cooldown_until = curr_time + 300

    # ---- F. 评分系统 ----
    s_score = 30 if is_stable else 0
    f_score = 30 if actual_v_delta > 500 else 0
    t_score = 40 if is_time_confirmed else 0
    total_score = s_score + f_score + t_score

    st.session_state.score_cache.append(total_score)
    st.session_state.score_cache = st.session_state.score_cache[-5:]
    score_stable = len(st.session_state.score_cache) >= 3 and min(st.session_state.score_cache[-3:]) >= 70

    return round(p_sup, 2), total_score, is_stable, (s_score, f_score, t_score), score_stable

# ===================== 3. UI 界面 =====================
st.set_page_config(page_title="Gringotts v5.5", layout="wide")
st.sidebar.title("🏦 古灵阁实战柜台")
target_code = st.sidebar.text_input("股票代码", value="002415")
capital = st.sidebar.number_input("拟压仓资金", value=100000)

# 【关键】替换 While True，使用自动定时刷新或手动按钮
auto_run = st.sidebar.toggle("开启实时审计 (5s)", value=True)

# ===================== 3. UI 实时获取逻辑修正 =====================
try:
    # 修复点 1：确保 target_code 是列表，且去掉可能存在的空格
    code_list = [target_code.strip()] 
    
    # 修复点 2：调用接口时显式传入列表
    df = ef.stock.get_realtime_quotes(code_list)
    
    # 修复点 3：增加严密的空值审计
    if df is None or len(df) == 0:
        st.warning(f"🏦 古灵阁正在搜寻代码 {target_code}... 请确保代码正确（如 002415）")
    else:
        # 即使返回了数据，也要确保我们抓到的是那一只
        quote = df.iloc[0]
        
        # 某些情况下 efinance 会返回多行，过滤出我们想要的
        if '代码' in df.columns:
            target_df = df[df['代码'] == target_code]
            if not target_df.empty:
                quote = target_df.iloc[0]

        curr_p = safe_float(quote['最新价'])
        
        # 整理买卖盘
        bids = pd.DataFrame([{'价格':safe_float(quote[f'买价{i}']), '数量':safe_float(quote[f'买量{i}'])} for i in range(1,6)])
        
        # 运行内核
        p_sup, score, is_stable, sub_scores, score_stable = gringotts_kernel(quote, bids)

        # UI 渲染
        c1, c2, c3 = st.columns([1,2,1])
        c1.metric("现价", f"¥{curr_p}", f"{quote['涨跌幅']}%")

        if time.time() < st.session_state.cooldown_until:
            c2.error(f"🛡️ 冷却中，锁定至 {datetime.fromtimestamp(st.session_state.cooldown_until).strftime('%H:%M:%S')}")
        else:
            color = "green" if score_stable else ("yellow" if score >= 40 else "red")
            c2.markdown(f"<h1 style='text-align:center; color:{color};'>意图评分: {score}</h1>", unsafe_allow_html=True)

        c3.metric("加权支撑线", f"¥{p_sup}", "稳定" if is_stable else "波动")
        
        st.divider()
        sc1, sc2, sc3 = st.columns(3)
        sc1.write(f"📊 盘口结构: {sub_scores[0]}/30")
        sc2.write(f"💧 资金增量: {sub_scores[1]}/30")
        sc3.write(f"⏳ 时间验证: {sub_scores[2]}/40")

        if score_stable:
            st.success(f"🔥 重仓压仓：建议建议 ¥{capital * 0.4:,.0f} (40%)")
        elif score >= 40:
            st.warning(f"🟡 试探建仓：建议建议 ¥{capital * 0.1:,.0f} (10%)")
        else:
            st.info("⚪ 观望：金库防御中...")

    # 如果开启自动刷新，5秒后重新运行脚本
    if auto_run:
        time.sleep(5)
        st.rerun()

except Exception as e:
    st.error(f"审计异常: {e}")
