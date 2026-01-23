import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import plotly.graph_objects as go
import re
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Timezones for each ISO
CENTRAL_TZ = ZoneInfo("America/Chicago")  # ERCOT
EASTERN_TZ = ZoneInfo("America/New_York")  # PJM
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")  # CAISO

ISO_TIMEZONES = {
    "ERCOT": CENTRAL_TZ,
    "PJM": EASTERN_TZ,
    "CAISO": PACIFIC_TZ,
}

# Page configuration
st.set_page_config(
    page_title="Leeward Asset Dashboard",
    layout="wide"
)

# Styling - Force dark mode and wide layout
st.markdown("""
<style>
    /* Force dark mode */
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"], 
    [data-testid="stToolbar"], [data-testid="stDecoration"], 
    [data-testid="stStatusWidget"], .main, section[data-testid="stSidebar"] {
        background-color: #1a1a1a !important;
        color: #ffffff !important;
    }
    
    /* Main container */
    .main .block-container { padding-top: 1rem; max-width: 100%; }
    
    /* Tabs styling */
    .stTabs [data-baseweb="tab-list"] { 
        gap: 20px; 
        background-color: #1a1a1a;
    }
    .stTabs [data-baseweb="tab"] {
        font-size: 20px;
        font-weight: bold;
        padding: 15px 30px;
        color: #ffffff !important;
        background-color: #1a1a1a !important;
    }
    .stTabs [aria-selected="true"] {
        background-color: #333 !important;
    }
    
    /* Button styling */
    .stButton > button {
        background-color: #333 !important;
        color: #ffffff !important;
        border: 1px solid #555 !important;
    }
    
    .main-title {
        font-size: 48px;
        font-weight: bold;
        color: #ffffff;
        margin-bottom: 5px;
    }
    
    .price-box {
        background-color: #0d0d0d;
        border: 1px solid #333;
        padding: 10px 8px;
        text-align: center;
        margin-bottom: 5px;
    }
    .node-label {
        font-size: 18px;
        color: #ffffff;
        font-weight: bold;
        margin-bottom: 5px;
    }
    .data-type {
        font-size: 14px;
        color: #888;
        margin-bottom: 8px;
    }
    .price-value {
        font-size: 48px;
        font-weight: bold;
        margin: 10px 0;
    }
    .price-green { color: #00ff00; }
    .price-red { color: #ff4444; }
    
    .refresh-text {
        font-size: 18px;
        color: #888;
        margin-bottom: 20px;
    }
</style>
""", unsafe_allow_html=True)

# YES Energy credentials from Streamlit secrets
YES_AUTH = (st.secrets["yes_energy"]["username"], st.secrets["yes_energy"]["password"])
YES_BASE = 'https://services.yesenergy.com/PS/rest'

# All nodes with YES Energy object IDs
ERCOT_NODES = {
    "Horizon Solar": 10017137187,
    "Morrow Solar": 10017467500,
    "Sweetwater": 10000698821,
    "Barilla Solar": 10004063217,
}

PJM_NODES = {
    "Big Plain Solar": 2156113380,
    "Oak Trail Solar": 2156113029,
    "Allegheny Ridge": 71856697,
    "Mendota Hills": 1552844480,
    "Crescent Ridge": 1552844482,
    "Lone Tree": 2156110042,
    "GSG Sublette": 2041988725,
    "GSG Westbrook": 1084391168,
}

CAISO_NODES = {
    "White Wing Ranch": 10017280372,
    "Sierra Pinta Battery": 10018494391,
    "Kumeyaay Wind": 20000004301,
}

# API Configuration
API_TIMEOUT = 30
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 2  # seconds between retries


def get_current_he(tz):
    """Get current Hour Ending for given timezone. 4:00 PM (hour 16) = HE17, 12:00 AM (hour 0) = HE1"""
    now = datetime.now(tz)
    return now.hour + 1


def parse_yes_html_table(html_text):
    """Parse YES Energy HTML table response into DataFrame.
    Returns None if parsing fails."""
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
        df = pd.DataFrame(data, columns=headers)
        return df
    
    return None


def _fetch_with_retry(url: str, description: str, retries: int = None) -> requests.Response:
    """Fetch URL with retry logic. Returns response or raises Exception after all retries fail."""
    max_retries = retries if retries is not None else API_RETRY_ATTEMPTS
    last_error = None
    
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, auth=YES_AUTH, timeout=API_TIMEOUT)
            if response.ok:
                return response
            else:
                last_error = f"{description} returned HTTP {response.status_code}"
                logger.warning(f"Attempt {attempt}/{max_retries}: {last_error}")
        except requests.exceptions.Timeout:
            last_error = f"{description} timed out after {API_TIMEOUT}s"
            logger.warning(f"Attempt {attempt}/{max_retries}: {last_error}")
        except requests.exceptions.RequestException as e:
            last_error = f"{description} request failed: {e}"
            logger.warning(f"Attempt {attempt}/{max_retries}: {last_error}")
        
        # Wait before retry (except on last attempt)
        if attempt < max_retries:
            time.sleep(API_RETRY_DELAY)
    
    raise Exception(last_error)


# ============================================================================
# YES Energy API Functions
# ============================================================================

@st.cache_data(ttl=120)  # 2min TTL - reduces API load during tab switching
def fetch_rt_5min(objectid, date_str, refresh_key):
    """Fetch 5-min RT LMP from YES Energy timeseries API.
    refresh_key forces cache invalidation on page refresh.
    Returns (DataFrame, latest_price) tuple or raises Exception."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    yes_date = dt.strftime('%m/%d/%Y')
    
    url = f"{YES_BASE}/timeseries/RTLMP/{objectid}?agglevel=5MIN&startdate={yes_date}&enddate={yes_date}"
    
    response = _fetch_with_retry(url, f"RT API for {objectid}")
    
    df = parse_yes_html_table(response.text)
    if df is None or df.empty:
        raise Exception(f"RT parse returned empty data for {objectid}")
    
    df['datetime'] = pd.to_datetime(df['DATETIME'])
    df['RT_Price'] = pd.to_numeric(df['AVGVALUE'], errors='coerce')
    df['time_hrs'] = df['datetime'].dt.hour + df['datetime'].dt.minute / 60.0
    df = df.sort_values('datetime')
    
    # Get the latest non-NaN price
    valid_prices = df.dropna(subset=['RT_Price'])
    if valid_prices.empty:
        raise Exception(f"No valid RT prices found for {objectid}")
    
    latest = valid_prices['RT_Price'].iloc[-1]
    
    logger.info(f"RT fetch success for {objectid}: latest=${latest:.2f}, {len(df)} rows")
    return df[['time_hrs', 'RT_Price']].copy(), latest


@st.cache_data(ttl=86400)  # 24h TTL - DA prices don't change during the day
def fetch_da_hourly(objectid, date_str):
    """Fetch hourly DA LMP from YES Energy timeseries API.
    Returns DataFrame or raises Exception."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    yes_date = dt.strftime('%m/%d/%Y')
    
    url = f"{YES_BASE}/timeseries/DALMP/{objectid}?agglevel=HOUR&startdate={yes_date}&enddate={yes_date}"
    
    response = _fetch_with_retry(url, f"DA API for {objectid}", retries=5)
    
    df = parse_yes_html_table(response.text)
    if df is None or df.empty:
        raise Exception(f"DA parse returned empty data for {objectid}")
    
    df['datetime'] = pd.to_datetime(df['DATETIME'])
    df['DA_Price'] = pd.to_numeric(df['AVGVALUE'], errors='coerce')
    
    if 'HOURENDING' in df.columns:
        df['HE'] = pd.to_numeric(df['HOURENDING'], errors='coerce')
    else:
        df['HE'] = df['datetime'].dt.hour + 1
    
    df = df.sort_values('HE')
    
    logger.info(f"DA fetch success for {objectid}: {len(df)} hours")
    return df[['HE', 'DA_Price']].copy()


# ============================================================================
# Display Functions
# ============================================================================

def render_price_boxes(display_name, da_price, rt_price):
    da_color = "price-red"
    da_str = f"${da_price:.2f}" if da_price is not None else "N/A"
    
    if rt_price is not None and da_price is not None:
        rt_color = "price-green" if rt_price >= da_price else "price-red"
    elif rt_price is not None:
        rt_color = "price-green"
    else:
        rt_color = "price-red"
    rt_str = f"${rt_price:.2f}" if rt_price is not None else "N/A"
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown(f"""
        <div class="price-box">
            <div class="node-label">{display_name}</div>
            <div class="data-type">DA LMP</div>
            <div class="price-value {da_color}">{da_str}</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        st.markdown(f"""
        <div class="price-box">
            <div class="node-label">{display_name}</div>
            <div class="data-type">RT LMP</div>
            <div class="price-value {rt_color}">{rt_str}</div>
        </div>
        """, unsafe_allow_html=True)


def create_price_chart(da_df, rt_5min_df):
    """Create chart with hourly DA (step) and 5-min RT"""
    fig = go.Figure()
    
    # RT line (white) - 5-min data
    if rt_5min_df is not None and not rt_5min_df.empty:
        fig.add_trace(
            go.Scatter(
                x=rt_5min_df['time_hrs'],
                y=rt_5min_df['RT_Price'],
                mode='lines',
                name='RT',
                line=dict(color='#ffffff', width=2),
                hovertemplate='%{x:.2f}h<br>RT: $%{y:.2f}<extra></extra>'
            )
        )
    
    # DA hourly step line (red)
    if da_df is not None and not da_df.empty:
        da_x = []
        da_y = []
        for _, row in da_df.iterrows():
            he = int(row['HE'])
            price = row['DA_Price']
            start_hr = he - 1
            end_hr = he
            da_x.extend([start_hr, end_hr])
            da_y.extend([price, price])
        
        fig.add_trace(
            go.Scatter(
                x=da_x,
                y=da_y,
                mode='lines',
                name='DA',
                line=dict(color='#ff4444', width=2, shape='hv'),
                hovertemplate='HE%{x:.0f}<br>DA: $%{y:.2f}<extra></extra>'
            )
        )
    
    fig.update_layout(
        paper_bgcolor='#0d0d0d',
        plot_bgcolor='#0d0d0d',
        height=300,
        margin=dict(l=60, r=20, t=20, b=50),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=0.99,
            xanchor="right",
            x=0.99,
            font=dict(size=16, color='#ffffff'),
            bgcolor='rgba(0,0,0,0.7)'
        ),
        hovermode='x unified',
        xaxis=dict(
            tickmode='array',
            tickvals=[0, 4, 8, 12, 16, 20, 24],
            ticktext=['00', '04', '08', '12', '16', '20', '24'],
            tickfont=dict(size=16, color='#ffffff'),
            gridcolor='#444',
            showline=True,
            linecolor='#666',
            range=[0, 24],
            title=None
        ),
        yaxis=dict(
            tickfont=dict(size=16, color='#ffffff'),
            gridcolor='#444',
            showline=True,
            linecolor='#666',
            tickprefix='$',
            zeroline=False
        )
    )
    
    return fig


def render_node(display_name, objectid, date_str, current_he, refresh_key):
    """Render a single node panel with price boxes and chart"""
    # Fetch RT data - refresh_key forces fresh fetch on page refresh
    rt_5min_df = None
    current_rt = None
    try:
        rt_5min_df, current_rt = fetch_rt_5min(objectid, date_str, refresh_key)
    except Exception as e:
        logger.error(f"RT fetch failed for {display_name}: {e}")
    
    # Fetch DA data - no hour_key needed, DA is fixed for the day
    da_df = None
    try:
        da_df = fetch_da_hourly(objectid, date_str)
    except Exception as e:
        logger.error(f"DA fetch failed for {display_name}: {e}")
    
    # Get current DA price for display (match current HE)
    current_da = None
    if da_df is not None and not da_df.empty:
        da_row = da_df[da_df['HE'] == current_he]
        if not da_row.empty:
            current_da = da_row['DA_Price'].iloc[0]
    
    render_price_boxes(display_name, current_da, current_rt)
    
    fig = create_price_chart(da_df, rt_5min_df)
    st.plotly_chart(fig, use_container_width=True, key=f"chart_{objectid}")


def render_ercot_tab():
    tz = ISO_TIMEZONES["ERCOT"]
    date_str = datetime.now(tz).strftime('%Y-%m-%d')
    current_he = get_current_he(tz)
    refresh_key = datetime.now(tz).strftime('%Y-%m-%d %H:%M')[:15]  # Changes every 5 min
    
    ercot_list = list(ERCOT_NODES.items())
    
    ercot_row1 = st.columns(3)
    for i in range(3):
        with ercot_row1[i]:
            display_name, objectid = ercot_list[i]
            render_node(display_name, objectid, date_str, current_he, refresh_key)
    
    ercot_row2 = st.columns(3)
    with ercot_row2[0]:
        display_name, objectid = ercot_list[3]
        render_node(display_name, objectid, date_str, current_he, refresh_key)


def render_pjm_tab():
    tz = ISO_TIMEZONES["PJM"]
    date_str = datetime.now(tz).strftime('%Y-%m-%d')
    current_he = get_current_he(tz)
    refresh_key = datetime.now(tz).strftime('%Y-%m-%d %H:%M')[:15]  # Changes every 5 min
    
    pjm_list = list(PJM_NODES.items())
    
    pjm_row1 = st.columns(3)
    for i in range(3):
        with pjm_row1[i]:
            display_name, objectid = pjm_list[i]
            render_node(display_name, objectid, date_str, current_he, refresh_key)
    
    pjm_row2 = st.columns(3)
    for i in range(3, 6):
        with pjm_row2[i-3]:
            display_name, objectid = pjm_list[i]
            render_node(display_name, objectid, date_str, current_he, refresh_key)
    
    pjm_row3 = st.columns(3)
    for i in range(6, 8):
        with pjm_row3[i-6]:
            display_name, objectid = pjm_list[i]
            render_node(display_name, objectid, date_str, current_he, refresh_key)


def render_caiso_tab():
    tz = ISO_TIMEZONES["CAISO"]
    date_str = datetime.now(tz).strftime('%Y-%m-%d')
    current_he = get_current_he(tz)
    refresh_key = datetime.now(tz).strftime('%Y-%m-%d %H:%M')[:15]  # Changes every 5 min
    
    caiso_cols = st.columns(len(CAISO_NODES))
    
    for i, (display_name, objectid) in enumerate(CAISO_NODES.items()):
        with caiso_cols[i]:
            render_node(display_name, objectid, date_str, current_he, refresh_key)


def _get_rt_price(objectid, date_str, refresh_key):
    """Helper to fetch RT price only."""
    try:
        _, current_rt = fetch_rt_5min(objectid, date_str, refresh_key)
        return current_rt
    except Exception:
        return None


def render_all_rt_tab():
    """Render All-RT tab with 3 columns showing all assets and RT prices"""
    col1, col2, col3 = st.columns(3)
    
    # Custom CSS for this tab
    st.markdown("""
    <style>
        .rt-header {
            font-size: 24px;
            font-weight: bold;
            color: #ffffff;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #444;
        }
        .rt-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #333;
        }
        .rt-asset {
            font-size: 18px;
            color: #ffffff;
        }
        .rt-price {
            font-size: 18px;
            font-weight: bold;
        }
    </style>
    """, unsafe_allow_html=True)
    
    def render_iso_column(iso_name, nodes_dict):
        tz = ISO_TIMEZONES[iso_name]
        date_str = datetime.now(tz).strftime('%Y-%m-%d')
        refresh_key = datetime.now(tz).strftime('%Y-%m-%d %H:%M')[:15]
        
        st.markdown(f'<div class="rt-header">{iso_name}</div>', unsafe_allow_html=True)
        for display_name, objectid in nodes_dict.items():
            current_rt = _get_rt_price(objectid, date_str, refresh_key)
            
            if current_rt is not None:
                price_str = f"${current_rt:.2f}"
                color = "#00ff00" if current_rt >= 0 else "#ff4444"
            else:
                price_str = "N/A"
                color = "#888"
            
            st.markdown(f'''
            <div class="rt-row">
                <span class="rt-asset">{display_name}</span>
                <span class="rt-price" style="color: {color};">{price_str}</span>
            </div>
            ''', unsafe_allow_html=True)
    
    with col1:
        render_iso_column("ERCOT", ERCOT_NODES)
    
    with col2:
        render_iso_column("PJM", PJM_NODES)
    
    with col3:
        render_iso_column("CAISO", CAISO_NODES)


def main():
    st.markdown('<div class="main-title">Leeward Asset Dashboard</div>', unsafe_allow_html=True)
    
    now = datetime.now(CENTRAL_TZ)
    current_he = get_current_he()
    
    # Calculate next 5-min interval refresh time - use :45 for data availability buffer
    current_minute = now.minute
    next_5min = ((current_minute // 5) + 1) * 5
    if next_5min >= 60:
        next_5min_refresh = (now + timedelta(hours=1)).replace(minute=0, second=45, microsecond=0)
    else:
        next_5min_refresh = now.replace(minute=next_5min, second=45, microsecond=0)
    if (next_5min_refresh - now).total_seconds() < 5:
        next_5min_refresh = next_5min_refresh + timedelta(minutes=5)
    
    seconds_until_refresh = int((next_5min_refresh - now).total_seconds())
    
    # Inject HTML meta refresh tag
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
