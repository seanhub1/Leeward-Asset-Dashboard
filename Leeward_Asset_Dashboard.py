import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import plotly.graph_objects as go
import re
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CENTRAL_TZ = ZoneInfo("America/Chicago")
EASTERN_TZ = ZoneInfo("America/New_York")

st.set_page_config(page_title="Leeward Asset Dashboard", layout="wide")

st.markdown("""
<style>
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"], 
    [data-testid="stToolbar"], [data-testid="stDecoration"], 
    [data-testid="stStatusWidget"], .main, section[data-testid="stSidebar"] {
        background-color: #1a1a1a !important;
        color: #ffffff !important;
    }
    .main .block-container { padding-top: 1rem; max-width: 100%; }
    .stTabs [data-baseweb="tab-list"] { gap: 20px; background-color: #1a1a1a; }
    .stTabs [data-baseweb="tab"] {
        font-size: 20px; font-weight: bold; padding: 15px 30px;
        color: #ffffff !important; background-color: #1a1a1a !important;
    }
    .stTabs [aria-selected="true"] { background-color: #333 !important; }
    .stButton > button {
        background-color: #333 !important; color: #ffffff !important;
        border: 1px solid #555 !important;
    }
    .main-title { font-size: 48px; font-weight: bold; color: #ffffff; margin-bottom: 5px; }
    .price-box {
        background-color: #0d0d0d; border: 1px solid #333;
        padding: 15px 20px; text-align: center; margin-bottom: 5px;
    }
    .node-label { font-size: 18px; color: #ffffff; font-weight: bold; margin-bottom: 5px; }
    .data-type { font-size: 14px; color: #888; margin-bottom: 8px; }
    .price-value { font-size: 48px; font-weight: bold; margin: 10px 0; }
    .price-green { color: #00ff00; }
    .price-red { color: #ff4444; }
    .refresh-text { font-size: 18px; color: #888; margin-bottom: 20px; }
</style>
""", unsafe_allow_html=True)


ERCOT_NODES = {
    "Horizon Solar":  "HRZN_SLR_UN1",
    "Sweetwater":     "SWEETWN3_3",
    "Barilla Solar":  "HOVEY_GEN",
    "Morrow Solar":   "MROW_SLR_RN",
}

PJM_NODES = {
    "Big Plain Solar":  "DEERCR  34.5 KV BIGPL2SP",
    "Oak Trail Solar":  "PUDDNRID34.5 KV OAKTRASP",
    "Allegheny Ridge":  "BEARROCK34.5 KV ARIDGWF1",
    "Mendota Hills":    "979 MEND34.5 KV MENDOTWF",
    "Crescent Ridge":   "981 CRES34.5 KV LONETRWF",
    "Lone Tree":        "981 CRES34.5 KV LONETRWF",
    "GSG Sublette":     "107 DIXO34.5 KV SUBLETTE",
    "GSG Westbrook":    "139 MEND34.5 KV WBROOKWF",
}

CAISO_NODES = {
    "White Wing Ranch":    10017280372,
    "Sierra Pinta Battery": 10018494391,
    "Kumeyaay Wind":       20000004301,
}

API_TIMEOUT = 30
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 2


def _ercot_auth():
    uid = st.secrets["ercot"]["username"]
    pwd = st.secrets["ercot"]["password"]
    sub = st.secrets["ercot"]["subscription"]
    auth_url = (
        "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
        "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
    )
    data = {
        'username': uid, 'password': pwd,
        'scope': 'openid fec253ea-0d06-4272-a5e6-b478baeecd70 offline_access',
        'client_id': 'fec253ea-0d06-4272-a5e6-b478baeecd70',
        'response_type': 'id_token', 'grant_type': 'password',
    }
    try:
        resp = requests.post(auth_url, data=data, timeout=60)
        if resp.ok:
            token = resp.json().get("access_token")
            return {"Authorization": f"Bearer {token}", "Ocp-Apim-Subscription-Key": sub}
    except Exception:
        pass
    return None


def _pjm_headers():
    return {"Ocp-Apim-Subscription-Key": st.secrets["pjm"]["subscription_key"]}


YES_AUTH = (st.secrets["yes_energy"]["username"], st.secrets["yes_energy"]["password"])
YES_BASE = 'https://services.yesenergy.com/PS/rest'


def get_current_he():
    now = datetime.now(CENTRAL_TZ)
    return now.hour + 1


def fetch_ercot_rt(settlement_point, date_str):
    """Fetch 5-min SCED LMPs from np6-788-cd, return (df[time_hrs, RT_Price], latest_price)."""
    auths = _ercot_auth()
    if not auths:
        raise Exception("ERCOT auth failed")
    url = (
        f"https://api.ercot.com/api/public-reports/np6-788-cd/lmp_node_zone_hub"
        f"?SCEDTimestampFrom={date_str}T00:00:00&SCEDTimestampTo={date_str}T23:59:59"
        f"&settlementPoint={settlement_point}&size=200000"
    )
    resp = requests.get(url, headers=auths, timeout=API_TIMEOUT)
    if not resp.ok:
        raise Exception(f"ERCOT RT HTTP {resp.status_code}")
    result = resp.json()
    rows = result.get("data", [])
    if not rows:
        raise Exception(f"No RT data for {settlement_point}")
    fields = [f['name'] for f in result.get("fields", [])]
    df = pd.DataFrame(rows, columns=fields)
    df['LMP'] = pd.to_numeric(df['LMP'], errors='coerce')
    df['_ts'] = pd.to_datetime(df['SCEDTimestamp'], errors='coerce')
    df = df.dropna(subset=['LMP', '_ts'])
    df['time_hrs'] = df['_ts'].dt.hour + df['_ts'].dt.minute / 60.0
    df['RT_Price'] = df['LMP']
    df = df.sort_values('_ts')
    latest = df['RT_Price'].iloc[-1]
    return df[['time_hrs', 'RT_Price']].copy(), latest


@st.cache_data(ttl=3600)
def fetch_ercot_da(settlement_point, date_str):
    """Fetch DAM SPP from np4-190-cd, return df[HE, DA_Price]."""
    auths = _ercot_auth()
    if not auths:
        raise Exception("ERCOT auth failed")
    url = (
        f"https://api.ercot.com/api/public-reports/np4-190-cd/dam_stlmnt_pnt_prices"
        f"?deliveryDateFrom={date_str}&deliveryDateTo={date_str}"
        f"&settlementPoint={settlement_point}&size=100"
    )
    resp = requests.get(url, headers=auths, timeout=API_TIMEOUT)
    if not resp.ok:
        raise Exception(f"ERCOT DA HTTP {resp.status_code}")
    result = resp.json()
    rows = result.get("data", [])
    if not rows:
        raise Exception(f"No DA data for {settlement_point}")
    fields = [f['name'] for f in result.get("fields", [])]
    df = pd.DataFrame(rows, columns=fields)
    df['DA_Price'] = pd.to_numeric(df['settlementPointPrice'], errors='coerce')
    if df['hourEnding'].dtype == 'object' and df['hourEnding'].str.contains(':').any():
        df['HE'] = df['hourEnding'].str.split(':').str[0].astype(int)
    else:
        df['HE'] = pd.to_numeric(df['hourEnding'], errors='coerce')
    df = df.dropna(subset=['HE', 'DA_Price'])
    df['HE'] = df['HE'].astype(int)
    df = df.sort_values('HE')
    return df[['HE', 'DA_Price']].copy()


def fetch_pjm_rt(pnode_name, date_str):
    """Fetch unverified 5-min RT LMP from PJM, return (df[time_hrs, RT_Price], latest_price).
    Uses rt_unverified_fivemin_lmps with pnode_id for 5-min granularity."""
    pnode_id = _get_pjm_pnode_id(pnode_name)
    if not pnode_id:
        raise Exception(f"Could not find pnode_id for {pnode_name}")
    hdrs = _pjm_headers()
    date_filter = f"{date_str} 00:00 to {date_str} 23:59"
    url = (
        f"https://api.pjm.com/api/v1/rt_unverified_fivemin_lmps"
        f"?download=true&rowCount=50000"
        f"&sort=datetime_beginning_ept&order=Asc&startRow=1"
        f"&datetime_beginning_ept={quote(date_filter)}"
        f"&pnode_id={pnode_id}"
        f"&fields=datetime_beginning_ept,total_lmp_rt,pnode_id"
    )
    resp = requests.get(url, headers=hdrs, timeout=API_TIMEOUT)
    if not resp.ok:
        raise Exception(f"PJM RT HTTP {resp.status_code}")
    data = resp.json()
    if not data:
        raise Exception(f"No PJM RT data for {pnode_name}")
    df = pd.json_normalize(data)
    if 'total_lmp_rt' not in df.columns:
        raise Exception(f"No total_lmp_rt in PJM response")
    df['datetime'] = pd.to_datetime(df['datetime_beginning_ept'])
    df['RT_Price'] = pd.to_numeric(df['total_lmp_rt'], errors='coerce')
    df['time_hrs'] = df['datetime'].dt.hour + df['datetime'].dt.minute / 60.0
    df = df.dropna(subset=['RT_Price']).sort_values('datetime')
    latest = df['RT_Price'].iloc[-1]
    return df[['time_hrs', 'RT_Price']].copy(), latest


@st.cache_data(ttl=86400)
def _get_pjm_pnode_id(pnode_name):
    """Look up pnode_id from pnode_name via PJM pnode API. Cached for the day."""
    hdrs = _pjm_headers()
    url = (
        f"https://api.pjm.com/api/v1/pnode"
        f"?pnode_name={quote(pnode_name)}"
        f"&rowCount=10&startRow=1&download=true"
        f"&fields=pnode_id,pnode_name"
    )
    try:
        resp = requests.get(url, headers=hdrs, timeout=API_TIMEOUT)
        if resp.ok:
            data = resp.json()
            if data:
                for item in data:
                    if item.get('pnode_name', '').upper() == pnode_name.upper():
                        return item.get('pnode_id')
                return data[0].get('pnode_id')
    except Exception:
        pass
    return None


@st.cache_data(ttl=3600)
def fetch_pjm_da(pnode_name, date_str):
    """Fetch DA hourly LMP from PJM, return df[HE, DA_Price].
    Looks up pnode_id first (cached), then queries da_hrl_lmps by pnode_id."""
    pnode_id = _get_pjm_pnode_id(pnode_name)
    if not pnode_id:
        raise Exception(f"Could not find pnode_id for {pnode_name}")
    hdrs = _pjm_headers()
    url = (
        f"https://api.pjm.com/api/v1/da_hrl_lmps"
        f"?download=true&rowCount=50000"
        f"&sort=datetime_beginning_ept&order=Asc&startRow=1"
        f"&datetime_beginning_ept={date_str}%2000:00to{date_str}%2023:59"
        f"&pnode_id={pnode_id}"
        f"&fields=datetime_beginning_ept,total_lmp_da,pnode_id"
    )
    resp = requests.get(url, headers=hdrs, timeout=API_TIMEOUT)
    if not resp.ok:
        raise Exception(f"PJM DA HTTP {resp.status_code}")
    data = resp.json()
    if not data:
        raise Exception(f"No PJM DA data for {pnode_name} (pnode_id={pnode_id})")
    df = pd.json_normalize(data)
    if 'total_lmp_da' not in df.columns:
        raise Exception(f"No total_lmp_da in PJM response")
    df['datetime'] = pd.to_datetime(df['datetime_beginning_ept'])
    df['HE'] = df['datetime'].dt.hour + 1
    df['DA_Price'] = pd.to_numeric(df['total_lmp_da'], errors='coerce')
    df = df.dropna(subset=['DA_Price']).sort_values('HE')
    return df[['HE', 'DA_Price']].copy()


def parse_yes_html_table(html_text):
    if not html_text or not isinstance(html_text, str):
        return None
    rows = re.findall(r'<tr>(.*?)</tr>', html_text, re.DOTALL)
    if not rows:
        return None
    data = []
    headers = None
    for row in rows:
        if '<th>' in row:
            headers = re.findall(r'<th>(.*?)</th>', row)
            headers = [h.replace('&#160;', ' ').strip() for h in headers]
        elif '<td>' in row:
            cells = re.findall(r'<td>(.*?)</td>', row)
            cells = [c.replace('&#160;', ' ').strip() for c in cells]
            if cells:
                data.append(cells)
    if headers and data:
        return pd.DataFrame(data, columns=headers)
    return None


def _fetch_yes_with_retry(url, description):
    last_error = None
    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            response = requests.get(url, auth=YES_AUTH, timeout=API_TIMEOUT)
            if response.ok:
                return response
            last_error = f"{description} HTTP {response.status_code}"
        except requests.exceptions.RequestException as e:
            last_error = f"{description}: {e}"
        if attempt < API_RETRY_ATTEMPTS:
            time.sleep(API_RETRY_DELAY)
    raise Exception(last_error)


def fetch_caiso_rt(objectid, date_str):
    """Fetch 5-min RT LMP from YES Energy for CAISO."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    yes_date = dt.strftime('%m/%d/%Y')
    url = f"{YES_BASE}/timeseries/RTLMP/{objectid}?agglevel=5MIN&startdate={yes_date}&enddate={yes_date}"
    response = _fetch_yes_with_retry(url, f"CAISO RT {objectid}")
    df = parse_yes_html_table(response.text)
    if df is None or df.empty:
        raise Exception(f"No CAISO RT data for {objectid}")
    df['datetime'] = pd.to_datetime(df['DATETIME'])
    df['RT_Price'] = pd.to_numeric(df['AVGVALUE'], errors='coerce')
    df['time_hrs'] = df['datetime'].dt.hour + df['datetime'].dt.minute / 60.0
    df = df.sort_values('datetime')
    valid = df.dropna(subset=['RT_Price'])
    if valid.empty:
        raise Exception(f"No valid CAISO RT prices for {objectid}")
    latest = valid['RT_Price'].iloc[-1]
    return df[['time_hrs', 'RT_Price']].copy(), latest


@st.cache_data(ttl=3600)
def fetch_caiso_da(objectid, date_str):
    """Fetch hourly DA LMP from YES Energy for CAISO."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    yes_date = dt.strftime('%m/%d/%Y')
    url = f"{YES_BASE}/timeseries/DALMP/{objectid}?agglevel=HOUR&startdate={yes_date}&enddate={yes_date}"
    response = _fetch_yes_with_retry(url, f"CAISO DA {objectid}")
    df = parse_yes_html_table(response.text)
    if df is None or df.empty:
        raise Exception(f"No CAISO DA data for {objectid}")
    df['datetime'] = pd.to_datetime(df['DATETIME'])
    df['DA_Price'] = pd.to_numeric(df['AVGVALUE'], errors='coerce')
    if 'HOURENDING' in df.columns:
        df['HE'] = pd.to_numeric(df['HOURENDING'], errors='coerce')
    else:
        df['HE'] = df['datetime'].dt.hour + 1
    df = df.sort_values('HE')
    return df[['HE', 'DA_Price']].copy()


def render_price_boxes(display_name, da_price, rt_price):
    da_color = "price-red"
    da_str = f"${da_price:.2f}" if da_price is not None else "N/A"
    if da_price is not None and da_price >= 0:
        da_color = "price-green"

    rt_color = "price-red"
    rt_str = f"${rt_price:.2f}" if rt_price is not None else "N/A"
    if rt_price is not None and rt_price >= 0:
        rt_color = "price-green"

    st.markdown(f"""
    <div class="price-box">
        <div class="node-label">{display_name}</div>
        <div class="data-type">DA LMP</div>
        <div class="price-value {da_color}">{da_str}</div>
    </div>
    <div class="price-box">
        <div class="data-type">RT LMP</div>
        <div class="price-value {rt_color}">{rt_str}</div>
    </div>
    """, unsafe_allow_html=True)


def create_price_chart(da_df, rt_5min_df):
    fig = go.Figure()
    if rt_5min_df is not None and not rt_5min_df.empty:
        fig.add_trace(go.Scatter(
            x=rt_5min_df['time_hrs'], y=rt_5min_df['RT_Price'],
            mode='lines', name='RT', line=dict(color='#ffffff', width=2),
            hovertemplate='%{x:.2f}h<br>RT: $%{y:.2f}<extra></extra>'
        ))
    if da_df is not None and not da_df.empty:
        da_x, da_y = [], []
        for _, row in da_df.iterrows():
            he = int(row['HE'])
            price = row['DA_Price']
            da_x.extend([he - 1, he])
            da_y.extend([price, price])
        fig.add_trace(go.Scatter(
            x=da_x, y=da_y, mode='lines', name='DA',
            line=dict(color='#ff4444', width=2, shape='hv'),
            hovertemplate='HE%{x:.0f}<br>DA: $%{y:.2f}<extra></extra>'
        ))
    fig.update_layout(
        paper_bgcolor='#0d0d0d', plot_bgcolor='#0d0d0d', height=300,
        margin=dict(l=60, r=20, t=20, b=50),
        legend=dict(orientation="h", yanchor="top", y=0.99, xanchor="right", x=0.99,
                    font=dict(size=16, color='#ffffff'), bgcolor='rgba(0,0,0,0.7)'),
        hovermode='x unified',
        xaxis=dict(tickmode='array', tickvals=[0, 4, 8, 12, 16, 20, 24],
                   ticktext=['00', '04', '08', '12', '16', '20', '24'],
                   tickfont=dict(size=16, color='#ffffff'), gridcolor='#444',
                   showline=True, linecolor='#666', range=[0, 24], title=None),
        yaxis=dict(tickfont=dict(size=16, color='#ffffff'), gridcolor='#444',
                   showline=True, linecolor='#666', tickprefix='$')
    )
    return fig


def render_ercot_node(display_name, settlement_point, date_str, current_he):
    rt_df, current_rt = None, None
    try:
        rt_df, current_rt = fetch_ercot_rt(settlement_point, date_str)
    except Exception as e:
        logger.error(f"ERCOT RT failed for {display_name}: {e}")
    da_df = None
    try:
        da_df = fetch_ercot_da(settlement_point, date_str)
    except Exception as e:
        logger.error(f"ERCOT DA failed for {display_name}: {e}")
    current_da = None
    if da_df is not None and not da_df.empty:
        da_row = da_df[da_df['HE'] == current_he]
        if not da_row.empty:
            current_da = da_row['DA_Price'].iloc[0]
    render_price_boxes(display_name, current_da, current_rt)
    fig = create_price_chart(da_df, rt_df)
    st.plotly_chart(fig, use_container_width=True, key=f"chart_ercot_{settlement_point}")


def render_pjm_node(display_name, pnode_name, date_str, current_he):
    rt_df, current_rt = None, None
    try:
        rt_df, current_rt = fetch_pjm_rt(pnode_name, date_str)
    except Exception as e:
        logger.error(f"PJM RT failed for {display_name}: {e}")
    da_df = None
    try:
        da_df = fetch_pjm_da(pnode_name, date_str)
    except Exception as e:
        logger.error(f"PJM DA failed for {display_name}: {e}")
    current_da = None
    if da_df is not None and not da_df.empty:
        da_row = da_df[da_df['HE'] == current_he]
        if not da_row.empty:
            current_da = da_row['DA_Price'].iloc[0]
    render_price_boxes(display_name, current_da, current_rt)
    fig = create_price_chart(da_df, rt_df)
    st.plotly_chart(fig, use_container_width=True, key=f"chart_pjm_{hash(pnode_name)}_{display_name}")


def render_caiso_node(display_name, objectid, date_str, current_he):
    rt_df, current_rt = None, None
    try:
        rt_df, current_rt = fetch_caiso_rt(objectid, date_str)
    except Exception as e:
        logger.error(f"CAISO RT failed for {display_name}: {e}")
    da_df = None
    try:
        da_df = fetch_caiso_da(objectid, date_str)
    except Exception as e:
        logger.error(f"CAISO DA failed for {display_name}: {e}")
    current_da = None
    if da_df is not None and not da_df.empty:
        da_row = da_df[da_df['HE'] == current_he]
        if not da_row.empty:
            current_da = da_row['DA_Price'].iloc[0]
    render_price_boxes(display_name, current_da, current_rt)
    fig = create_price_chart(da_df, rt_df)
    st.plotly_chart(fig, use_container_width=True, key=f"chart_caiso_{objectid}")


def render_ercot_tab():
    date_str = datetime.now(CENTRAL_TZ).strftime('%Y-%m-%d')
    current_he = get_current_he()
    cols = st.columns(len(ERCOT_NODES))
    for i, (name, sp) in enumerate(ERCOT_NODES.items()):
        with cols[i]:
            render_ercot_node(name, sp, date_str, current_he)


def render_pjm_tab():
    now_et = datetime.now(EASTERN_TZ)
    date_str = now_et.strftime('%Y-%m-%d')
    current_he = now_et.hour + 1
    pjm_list = list(PJM_NODES.items())
    row1 = st.columns(4)
    for i in range(4):
        with row1[i]:
            name, pnode = pjm_list[i]
            render_pjm_node(name, pnode, date_str, current_he)
    row2 = st.columns(4)
    for i in range(4, 8):
        with row2[i - 4]:
            name, pnode = pjm_list[i]
            render_pjm_node(name, pnode, date_str, current_he)


def render_caiso_tab():
    date_str = datetime.now(CENTRAL_TZ).strftime('%Y-%m-%d')
    current_he = get_current_he()
    cols = st.columns(len(CAISO_NODES))
    for i, (name, oid) in enumerate(CAISO_NODES.items()):
        with cols[i]:
            render_caiso_node(name, oid, date_str, current_he)


def _get_rt_price_ercot(sp, date_str):
    try:
        _, price = fetch_ercot_rt(sp, date_str)
        return price
    except Exception:
        return None


def _get_rt_price_pjm(pnode, date_str):
    try:
        _, price = fetch_pjm_rt(pnode, date_str)
        return price
    except Exception:
        return None


def _get_rt_price_caiso(oid, date_str):
    try:
        _, price = fetch_caiso_rt(oid, date_str)
        return price
    except Exception:
        return None


def render_all_rt_tab():
    ercot_date = datetime.now(CENTRAL_TZ).strftime('%Y-%m-%d')
    pjm_date = datetime.now(EASTERN_TZ).strftime('%Y-%m-%d')

    st.markdown("""
    <style>
        .rt-header { font-size: 24px; font-weight: bold; color: #ffffff;
                     margin-bottom: 15px; padding-bottom: 10px; border-bottom: 2px solid #444; }
        .rt-row { display: flex; justify-content: space-between; padding: 8px 0;
                  border-bottom: 1px solid #333; }
        .rt-asset { font-size: 18px; color: #ffffff; }
        .rt-price { font-size: 18px; font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown('<div class="rt-header">ERCOT</div>', unsafe_allow_html=True)
        for name, sp in ERCOT_NODES.items():
            price = _get_rt_price_ercot(sp, ercot_date)
            price_str = f"${price:.2f}" if price is not None else "N/A"
            color = "#00ff00" if price is not None and price >= 0 else "#ff4444" if price is not None else "#888"
            st.markdown(f'<div class="rt-row"><span class="rt-asset">{name}</span>'
                        f'<span class="rt-price" style="color:{color};">{price_str}</span></div>',
                        unsafe_allow_html=True)

    with col2:
        st.markdown('<div class="rt-header">PJM</div>', unsafe_allow_html=True)
        for name, pnode in PJM_NODES.items():
            price = _get_rt_price_pjm(pnode, pjm_date)
            price_str = f"${price:.2f}" if price is not None else "N/A"
            color = "#00ff00" if price is not None and price >= 0 else "#ff4444" if price is not None else "#888"
            st.markdown(f'<div class="rt-row"><span class="rt-asset">{name}</span>'
                        f'<span class="rt-price" style="color:{color};">{price_str}</span></div>',
                        unsafe_allow_html=True)

    with col3:
        st.markdown('<div class="rt-header">CAISO</div>', unsafe_allow_html=True)
        for name, oid in CAISO_NODES.items():
            price = _get_rt_price_caiso(oid, ercot_date)
            price_str = f"${price:.2f}" if price is not None else "N/A"
            color = "#00ff00" if price is not None and price >= 0 else "#ff4444" if price is not None else "#888"
            st.markdown(f'<div class="rt-row"><span class="rt-asset">{name}</span>'
                        f'<span class="rt-price" style="color:{color};">{price_str}</span></div>',
                        unsafe_allow_html=True)


def main():
    st.markdown('<div class="main-title">Leeward Asset Dashboard</div>', unsafe_allow_html=True)

    now = datetime.now(CENTRAL_TZ)
    current_he = get_current_he()

    current_minute = now.minute
    next_5min = ((current_minute // 5) + 1) * 5
    if next_5min >= 60:
        next_5min_refresh = (now + timedelta(hours=1)).replace(minute=0, second=35, microsecond=0)
    else:
        next_5min_refresh = now.replace(minute=next_5min, second=35, microsecond=0)
    if (next_5min_refresh - now).total_seconds() < 5:
        next_5min_refresh = next_5min_refresh + timedelta(minutes=5)

    seconds_until_refresh = int((next_5min_refresh - now).total_seconds())
    st.markdown(f'<meta http-equiv="refresh" content="{seconds_until_refresh}">', unsafe_allow_html=True)

    col1, col2 = st.columns([6, 1])
    with col1:
        st.markdown(f'<p class="refresh-text">Last refresh: {now.strftime("%Y-%m-%d %H:%M:%S")} (HE{current_he}) | Next: {next_5min_refresh.strftime("%H:%M:%S")}</p>', unsafe_allow_html=True)
    with col2:
        if st.button("Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    tab_all_rt, tab_ercot, tab_pjm, tab_caiso = st.tabs(["All - RT", "ERCOT", "PJM", "CAISO"])

    with tab_all_rt:
        render_all_rt_tab()
    with tab_ercot:
        render_ercot_tab()
    with tab_pjm:
        render_pjm_tab()
    with tab_caiso:
        render_caiso_tab()


if __name__ == "__main__":
    main()
