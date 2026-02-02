import os
import sys
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import streamlit as st

# ===================== 0. 环境初始化 =====================
TZ_CHINA = timezone(timedelta(hours=8))

def get_now_china():
    return datetime.now(timezone.utc).astimezone(TZ_CHINA)

def is_trading_time():
    now = get_now_china()
    if now.weekday() >= 5: return False
    hm = now.hour * 100 + now.minute
    # 稍微拓宽一点监控边界
    return (915 <= hm <= 1135) or (1255 <= hm <= 1505)

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

    top_bids = df_bids.head(3).copy()
    top_bids['pf'] = top_bids['价格'].apply(safe_float)
    top_bids['vf'] = top_bids['数量'].apply(safe_float)
    p_sup = np.average(top_bids['pf'], weights=top_bids['vf']) if top_bids['vf'].sum() > 0 else curr_p

    st.session_state.support_cache.append(p_sup)
    st.session_state.support_cache = st.session_state.support_cache[-5:]
    is_stable = (max(st.session_state.support_cache) - min(st.session_state.support_cache)) <= 0.02 if len(st.session_state.support_cache) >= 3 else False

    curr_vol = safe_float(quote['成交量'])
    v_delta = curr_vol - st.session_state.prev_vol
    st.session_state.prev_vol = curr_vol
    actual_v_delta = v_delta if 0 < v_delta < 1000000 else 0 

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

    if curr_p > 0 and curr_p < p_sup * 0.98:
        st.session_state.hit_support = False
        st.session_state.rebound_cache = []
        st.session_state.cooldown_until = curr_time + 300

    s_score = 30 if is_stable else 0
    f_score = 30 if actual_v_delta > 500 else 0
    t_score = 40 if is_time_confirmed else 0
    total_score = s_score + f_score + t_score

    st.session_state.score_cache.append(total_score)
    st.session_state.score_cache = st.session_state.score_cache[-5:]
    score_stable = len(st.session_state.score_cache) >= 3 and min(st.session_state.score_cache[-3:]) >= 70

    return round(p_sup, 2), total_score, is_stable, (s_score, f_score, t_score), score_stable

# ===================== 3. UI 界面层 =====================
st.set_page_config(page_title="Gringotts Final v6.2", layout="wide")

# 自定义深蓝色风格 CSS
st.markdown("""
    <style>
    .reportview-container .main .block-container { color: #1A5276; }
    h1, h2, h3 { color: #1A5276 !important; }
    </style>
    """, unsafe_allow_html=True)

with st.sidebar:
    st.title("🏦 古灵阁实战柜台")
    target_code = st.text_input("股票代码 (如 002206)", value="002206").strip()
    capital = st.number_input("拟压仓资金", value=100000)
    auto_run = st.toggle("开启实时审计 (5s)", value=True)
    st.divider()
    st.write(f"🕒 **北京时间: {get_now_china().strftime('%H:%M:%S')}**")
    
    if st.button("强制重启审计内核"):
        st.session_state.clear()
        st.rerun()

main_container = st.empty()

# ===================== 4. 稳健获取 (原生接口版) =====================
def fetch_tencent_data(code):
    if not code or len(code) < 6: return None
    try:
        prefix = "sh" if code.startswith('6') else "sz"
        url = f"http://qt.gtimg.cn/q={prefix}{code}"
        r = requests.get(url, timeout=2)
        if r.status_code != 200: return None
        
        parts = r.text.split('~')
        if len(parts) < 30: return None
        
        return {
            '最新价': parts[3],
            '涨跌幅': parts[32],
            '成交量': parts[6],
            '买价1': parts[9], '买量1': parts[10],
            '买价2': parts[11], '买量2': parts[12],
            '买价3': parts[13], '买量3': parts[14],
            '买价4': parts[15], '买量4': parts[16],
            '买价5': parts[17], '买量5': parts[18],
        }
    except: return None

try:
    if is_trading_time():
        with main_container.container():
            data = fetch_tencent_data(target_code)
            if data:
                curr_p = safe_float(data['最新价'])
                bids = pd.DataFrame([{'价格':safe_float(data[f'买价{i}']), '数量':safe_float(data[f'买量{i}'])} for i in range(1,6)])
                
                p_sup, score, is_stable, sub_scores, score_stable = gringotts_kernel(data, bids)

                c1, c2, c3 = st.columns([1,2,1])
                c1.metric("市场报价", f"¥{curr_p}", f"{data['涨跌幅']}%")
                
                if time.time() < st.session_state.cooldown_until:
                    c2.error("🛡️ 冷却保护中...")
                else:
                    # 调色盘：深墨绿(稳)、暗金(中)、深蓝(低)
                    color = "#145A32" if score_stable else ("#9A7D0A" if score >= 40 else "#1A5276")
                    c2.markdown(f"<h1 style='text-align:center; color:{color};'>审计评分: {score}</h1>", unsafe_allow_html=True)
                
                c3.metric("加权支撑线", f"¥{p_sup}", "稳定" if is_stable else "波动")
                st.divider()
                
                st.subheader("🏦 压仓决策建议")
                if score_stable:
                    st.success(f"🔱 指令：【重仓压入】建议规模：¥{capital * 0.4:,.0f}")
                elif score >= 40:
                    st.warning(f"🏺 指令：【轻仓试探】建议规模：¥{capital * 0.1:,.0f}")
                else:
                    st.info("📜 指令：【金库待命】目前无显著信号")
            else:
                st.error(f"⚠️ 正在尝试连接数据源，请确认代码 [{target_code}] 是否正确...")
    else:
        st.info(f"🌙 目标 [{target_code}] 处于非交易时段。")

    if auto_run:
        time.sleep(5)
        st.rerun()

except Exception as e:
    st.error(f"审计异常: {e}")
