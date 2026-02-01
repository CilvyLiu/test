import os
import sys
import time
import types
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import streamlit as st

# ===================== 0. 权限与内存劫持 =====================
fake_home = Path("/tmp/gringotts_data")
fake_home.mkdir(parents=True, exist_ok=True)

if 'efinance.config' not in sys.modules:
    cfg = types.ModuleType('efinance.config')
    cfg.DATA_DIR = fake_home
    cfg.SEARCH_RESULT_CACHE_PATH = fake_home / "search_cache"
    cfg.MAX_CONNECTIONS = 10
    sys.modules['efinance.config'] = cfg

import efinance as ef

# ===================== 1. 时区与状态初始化 =====================
# 强制定义东八区
TZ_CHINA = timezone(timedelta(hours=8))

def get_now_china():
    """获取当前的东八区时间"""
    return datetime.now(timezone.utc).astimezone(TZ_CHINA)

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

# ===================== 2. 核心审计引擎 =====================
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
    actual_v_delta = v_delta if 100 < v_delta < 500000 else 0 

    # ---- D. 时间回踩确认 ----
    is_time_confirmed = False
    if curr_p > 0 and curr_p <= p_sup * 1.002:
        st.session_state.hit_support = True

    if st.session_state.hit_support:
        st.session_state.rebound_cache.append((curr_time, curr_p))
        st.session_state.rebound_cache = [x for x in st.session_state.rebound_cache if curr_time - x[0] <= 30]
        if len(st.session_state.rebound_cache) >= 3:
            time_diff = st.session_state.rebound_cache[-1][0] - st.session_state.rebound_cache[0][0]
            if time_diff >= 9 and min([x[1] for x in st.session_state.rebound_cache]) > p_sup * 0.995:
                is_time_confirmed = True

    # ---- E. 保护机制 ----
    if curr_p > 0 and curr_p < p_sup * 0.98:
        st.session_state.hit_support = False
        st.session_state.rebound_cache = []
        # 冷却 5 分钟
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

# ===================== 3. UI 界面层 =====================
st.set_page_config(page_title="Gringotts TimeFix v5.9", layout="wide")

with st.sidebar:
    st.title("🏦 古灵阁实战柜台")
    target_code = st.text_input("股票代码", value="002415").strip()
    capital = st.number_input("拟压仓资金", value=100000)
    auto_run = st.toggle("开启实时审计 (5s)", value=True)
    st.divider()
    st.write(f"🕒 系统时区: **北京时间 (UTC+8)**")
    st.write(f"当前时间: {get_now_china().strftime('%H:%M:%S')}")

main_container = st.empty()

try:
    symbol = target_code.strip()
    if "." not in symbol and len(symbol) == 6:
        full_code = f"1.{symbol}" if symbol.startswith('6') else f"0.{symbol}"
    else:
        full_code = symbol

    df = ef.stock.get_realtime_quotes([full_code])
    
    if df is not None and not df.empty and safe_float(df.iloc[0]['最新价']) > 0:
        quote = df.iloc[0]
        curr_p = safe_float(quote['最新价'])
        bids = pd.DataFrame([{'价格':safe_float(quote[f'买价{i}']), '数量':safe_float(quote[f'买量{i}'])} for i in range(1,6)])
        
        p_sup, score, is_stable, sub_scores, score_stable = gringotts_kernel(quote, bids)

        with main_container.container():
            c1, c2, c3 = st.columns([1,2,1])
            c1.metric("市场报价", f"¥{curr_p}", f"{quote.get('涨跌幅', '--')}%")
            
            # 使用东八区时间渲染冷却
            if time.time() < st.session_state.cooldown_until:
                # 将 timestamp 转为东八区 datetime
                cd_dt = datetime.fromtimestamp(st.session_state.cooldown_until, tz=timezone.utc).astimezone(TZ_CHINA)
                c2.error(f"🛡️ 冷却保护中... 预计重启: {cd_dt.strftime('%H:%M:%S')}")
            else:
                score_color = "green" if score_stable else ("yellow" if score >= 40 else "red")
                c2.markdown(f"<h1 style='text-align:center; color:{score_color};'>审计意图评分: {score}</h1>", unsafe_allow_html=True)
            
            c3.metric("加权支撑线", f"¥{p_sup}", "稳定" if is_stable else "波动")
            st.divider()
            
            sc1, sc2, sc3 = st.columns(3)
            sc1.write(f"📊 盘口结构分: **{sub_scores[0]}**/30")
            sc2.write(f"💧 资金增量分: **{sub_scores[1]}**/30")
            sc3.write(f"⏳ 时间验证分: **{sub_scores[2]}**/40")
            
            st.subheader("🏦 压仓决策建议")
            if score_stable:
                st.success(f"🔥 指令：【重仓压入】 (40%)")
            elif score >= 40:
                st.warning(f"🟡 指令：【轻仓试探】 (10%)")
            else:
                st.info("⚪ 指令：【金库待命】")
            
    else:
        with main_container.container():
            st.info(f"🌙 目标 [{target_code}] 处于非交易时段。")
            st.markdown(f"""
            **古灵阁休眠指令 (Standby)：**
            * 逻辑：环境与 API 隧道正常。
            * 时区确认：已强制同步至 **北京时间 (CST)**。
            * 当前北京时间: `{get_now_china().strftime('%Y-%m-%d %H:%M:%S')}`
            """)

    if auto_run:
        time.sleep(5)
        st.rerun()

except Exception as e:
    st.error(f"古灵阁运行审计异常: {e}")
