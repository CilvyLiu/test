import os
import sys
import time
import types
from pathlib import Path
from datetime import datetime
import pandas as pd
import numpy as np
import streamlit as st

# ===================== 0. 权限与内存劫持 (必须在 import ef 之前) =====================
fake_home = Path("/tmp/gringotts_data")
fake_home.mkdir(parents=True, exist_ok=True)

if 'efinance.config' not in sys.modules:
    cfg = types.ModuleType('efinance.config')
    cfg.DATA_DIR = fake_home
    cfg.SEARCH_RESULT_CACHE_PATH = fake_home / "search_cache"
    cfg.MAX_CONNECTIONS = 10
    sys.modules['efinance.config'] = cfg

import efinance as ef

# ===================== 1. 状态锁初始化 =====================
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

    # ---- A. 盘口结构审计 (Structure) ----
    top_bids = df_bids.head(3).copy()
    top_bids['pf'] = top_bids['价格'].apply(safe_float)
    top_bids['vf'] = top_bids['数量'].apply(safe_float)
    p_sup = np.average(top_bids['pf'], weights=top_bids['vf']) if top_bids['vf'].sum() > 0 else curr_p

    # ---- B. 支撑稳定性 (Stability) ----
    st.session_state.support_cache.append(p_sup)
    st.session_state.support_cache = st.session_state.support_cache[-5:]
    is_stable = (max(st.session_state.support_cache) - min(st.session_state.support_cache)) <= 0.02 if len(st.session_state.support_cache) >= 3 else False

    # ---- C. 资金流向审计 (Flow) ----
    curr_vol = safe_float(quote['成交量'])
    v_delta = curr_vol - st.session_state.prev_vol
    st.session_state.prev_vol = curr_vol
    actual_v_delta = v_delta if 100 < v_delta < 500000 else 0 # 宽容大票成交量

    # ---- D. 时间回踩确认 (Time Audit) ----
    is_time_confirmed = False
    if curr_p > 0 and curr_p <= p_sup * 1.002:
        st.session_state.hit_support = True

    if st.session_state.hit_support:
        st.session_state.rebound_cache.append((curr_time, curr_p))
        st.session_state.rebound_cache = [x for x in st.session_state.rebound_cache if curr_time - x[0] <= 30]
        if len(st.session_state.rebound_cache) >= 3:
            time_diff = st.session_state.rebound_cache[-1][0] - st.session_state.rebound_cache[0][0]
            # 核心修正：基于真实秒数的时间窗口
            if time_diff >= 9 and min([x[1] for x in st.session_state.rebound_cache]) > p_sup * 0.995:
                is_time_confirmed = True

    # ---- E. 保护机制 (Risk Control) ----
    if curr_p > 0 and curr_p < p_sup * 0.98:
        st.session_state.hit_support = False
        st.session_state.rebound_cache = []
        st.session_state.cooldown_until = curr_time + 300

    # ---- F. 结构化评分 ----
    s_score = 30 if is_stable else 0
    f_score = 30 if actual_v_delta > 500 else 0
    t_score = 40 if is_time_confirmed else 0
    total_score = s_score + f_score + t_score

    st.session_state.score_cache.append(total_score)
    st.session_state.score_cache = st.session_state.score_cache[-5:]
    score_stable = len(st.session_state.score_cache) >= 3 and min(st.session_state.score_cache[-3:]) >= 70

    return round(p_sup, 2), total_score, is_stable, (s_score, f_score, t_score), score_stable

# ===================== 3. UI 界面层 =====================
st.set_page_config(page_title="Gringotts Pro v5.6", layout="wide")

with st.sidebar:
    st.title("🏦 古灵阁实战柜台")
    target_code = st.text_input("股票代码 (如 002415)", value="002415").strip()
    capital = st.number_input("拟压仓资金", value=100000)
    auto_run = st.toggle("开启实时审计 (5s)", value=True)
    st.divider()
    st.caption("注：非交易日数据可能显示为待机状态")

# 主展示区容器
main_container = st.empty()

# ===================== 3. UI 实时获取逻辑 (参数加固版) =====================
try:
    # 修复点：自动补全市场前缀 (efinance 规范：深市 0.xxxxxx, 沪市 1.xxxxxx)
    symbol = target_code.strip()
    if "." not in symbol:
        # 6 开头为沪市，其余（00, 30, 002）通常为深市
        full_code = f"1.{symbol}" if symbol.startswith('6') else f"0.{symbol}"
    else:
        full_code = symbol

    # 调用接口时使用带前缀的完整代码
    df = ef.stock.get_realtime_quotes([full_code])
    
    if df is None or df.empty:
        # 如果带前缀还查不到，尝试原始代码（容错机制）
        df = ef.stock.get_realtime_quotes([symbol])

    if df is not None and not df.empty:
        # 这里的匹配逻辑也要同步适配
        quote = df.iloc[0]
        curr_p = safe_float(quote['最新价'])
        
        # 整理买卖盘数据
        bids = pd.DataFrame([{'价格':safe_float(quote[f'买价{i}']), '数量':safe_float(quote[f'买量{i}'])} for i in range(1,6)])
        
        # 执行审计
        p_sup, score, is_stable, sub_scores, score_stable = gringotts_kernel(quote, bids)

        # 渲染内容
        with main_container.container():
            c1, c2, c3 = st.columns([1,2,1])
            c1.metric("市场报价", f"¥{curr_p}", f"{quote.get('涨跌幅', '--')}%")
            
            # 状态判定
            if time.time() < st.session_state.cooldown_until:
                c2.error(f"🛡️ 冷却保护中... 预计重启时间: {datetime.fromtimestamp(st.session_state.cooldown_until).strftime('%H:%M:%S')}")
            else:
                score_color = "green" if score_stable else ("yellow" if score >= 40 else "red")
                c2.markdown(f"<h1 style='text-align:center; color:{score_color};'>审计意图评分: {score}</h1>", unsafe_allow_html=True)
            
            c3.metric("加权支撑线", f"¥{p_sup}", "稳定" if is_stable else "波动")
            
            st.divider()
            
            # 评分详情
            sc1, sc2, sc3 = st.columns(3)
            sc1.write(f"📊 盘口结构分: **{sub_scores[0]}**/30")
            sc2.write(f"💧 资金增量分: **{sub_scores[1]}**/30")
            sc3.write(f"⏳ 时间验证分: **{sub_scores[2]}**/40")
            
            # 交易指令
            st.subheader("🏦 压仓决策建议")
            if score_stable:
                st.success(f"🔥 指令：【重仓压入】。建议规模：¥{capital * 0.4:,.0f} (40%)")
            elif score >= 40:
                st.warning(f"🟡 指令：【轻仓试探】。建议规模：¥{capital * 0.1:,.0f} (10%)")
            else:
                st.info("⚪ 指令：【金库待命】。目前无显著主力介入信号。")

    else:
        st.warning("⚠️ 接口响应中：非交易日或代码输入错误，请等待或检查代码。")

    # 循环刷新逻辑
    if auto_run:
        time.sleep(5)
        st.rerun()

except Exception as e:
    st.error(f"古灵阁运行审计异常: {e}")
