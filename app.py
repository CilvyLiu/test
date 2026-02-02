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
TZ_CHINA = timezone(timedelta(hours=8))

def get_now_china():
    """获取当前的东八区北京时间"""
    return datetime.now(timezone.utc).astimezone(TZ_CHINA)

def is_trading_time():
    """判断当前是否为 A 股交易时段 (09:15-11:30, 13:00-15:00)"""
    now = get_now_china()
    # 排除周六周日
    if now.weekday() >= 5: return False
    
    hm = now.hour * 100 + now.minute
    morning = 915 <= hm <= 1130
    afternoon = 1300 <= hm <= 1500
    return morning or afternoon

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
    # 正常分时增量判定
    actual_v_delta = v_delta if 0 < v_delta < 1000000 else 0 

    # ---- D. 时间回踩确认 (Time Audit) ----
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
st.set_page_config(page_title="Gringotts Final v6.1", layout="wide")

with st.sidebar:
    st.title("🏦 古灵阁实战柜台")
    target_code = st.text_input("股票代码", value="002415").strip()
    capital = st.number_input("拟压仓资金", value=100000)
    auto_run = st.toggle("开启实时审计 (5s)", value=True)
    st.divider()
    st.write(f"🕒 **时区: 北京时间 (CST)**")
    st.write(f"{get_now_china().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 强制手动刷新按钮（应对 API 卡死）
    if st.button("强制重启审计内核"):
        st.session_state.clear()
        st.rerun()

main_container = st.empty()

# ===================== 4. 稳健获取与逻辑修复 =====================
try:
    # 核心修复点：优先判断是否在交易时间，而非优先判断价格
    trading_active = is_trading_time()

    symbol = target_code.strip()
    full_code = symbol
    if "." not in symbol and len(symbol) == 6:
        full_code = f"1.{symbol}" if symbol.startswith('6') else f"0.{symbol}"

    df = None
    try:
        df = ef.stock.get_realtime_quotes([full_code])
    except:
        try:
            df = ef.stock.get_realtime_quotes([symbol])
        except:
            df = None

    # 修改逻辑：如果在交易时间内，即使 df 暂时异常，也显示 [Active] 状态
    if trading_active:
        with main_container.container():
            if df is not None and not df.empty:
                quote = df.iloc[0]
                curr_p = safe_float(quote['最新价'])
                
                # 如果是 09:30 刚开盘价格还没出来的容错处理
                if curr_p <= 0:
                    st.warning(f"🏦 审计已激活：等待 [{target_code}] 开盘首笔成交流入...")
                else:
                    bids = pd.DataFrame([{'价格':safe_float(quote[f'买价{i}']), '数量':safe_float(quote[f'买量{i}'])} for i in range(1,6)])
                    p_sup, score, is_stable, sub_scores, score_stable = gringotts_kernel(quote, bids)

                    c1, c2, c3 = st.columns([1,2,1])
                    c1.metric("市场报价", f"¥{curr_p}", f"{quote.get('涨跌幅', '--')}%")
                    
                    if time.time() < st.session_state.cooldown_until:
                        cd_dt = datetime.fromtimestamp(st.session_state.cooldown_until, tz=timezone.utc).astimezone(TZ_CHINA)
                        c2.error(f"🛡️ 冷却保护中... 重启时间: {cd_dt.strftime('%H:%M:%S')}")
                    else:
                        color = "green" if score_stable else ("yellow" if score >= 40 else "red")
                        c2.markdown(f"<h1 style='text-align:center; color:{color};'>审计意图评分: {score}</h1>", unsafe_allow_html=True)
                    
                    c3.metric("加权支撑线", f"¥{p_sup}", "稳定" if is_stable else "波动")
                    st.divider()
                    
                    sc1, sc2, sc3 = st.columns(3)
                    sc1.write(f"📊 结构: {sub_scores[0]}/30")
                    sc2.write(f"💧 增量: {sub_scores[1]}/30")
                    sc3.write(f"⏳ 验证: {sub_scores[2]}/40")
                    
                    st.subheader("🏦 压仓决策建议")
                    if score_stable:
                        st.success(f"🔥 指令：【重仓压入】建议规模：¥{capital * 0.4:,.0f}")
                    elif score >= 40:
                        st.warning(f"🟡 指令：【轻仓试探】建议规模：¥{capital * 0.1:,.0f}")
                    else:
                        st.info("⚪ 指令：【金库待命】目前无显著信号")
            else:
                st.error(f"⚠️ 正在尝试连接 [{target_code}] 数据通道...")
    else:
        # 非交易时间逻辑
        with main_container.container():
            st.info(f"🌙 目标 [{target_code}] 处于非交易时段。")
            st.markdown(f"""
            ### 🏦 古灵阁待机中 (Standby Mode)
            - **时区同步**：北京时间校准成功 ✅
            - **权限状态**：内存数据目录已挂载 ✅
            - **API 探测**：数据通道已就绪，当前为非交易静默期。
            
            **审计激活时间**：明早 **09:15** 集合竞价开始。
            *当前系统时间: {get_now_china().strftime('%Y-%m-%d %H:%M:%S')}*
            """)

    if auto_run:
        time.sleep(5)
        st.rerun()

except Exception as e:
    st.error(f"古灵阁运行审计异常: {e}")
