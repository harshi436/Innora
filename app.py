"""
Hotel AI Voice Assistant — Admin Dashboard
==========================================
Complete Streamlit frontend covering:
  - Live system health
  - Hotel management (register, update, delete)
  - PDF knowledge base upload & management
  - Food orders, room cleaning, spa, essentials tracking
  - Call logs & conversation viewer
  - Inquiry tracker
  - WhatsApp integration
  - Outbound call trigger
  - Analytics & charts
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
import logging
logging.getLogger("streamlit").setLevel(logging.ERROR)

import streamlit as st
import requests
import json
import time
from datetime import datetime
from typing import Optional, Dict, List, Any
import os

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="Hotel AI Voice Assistant",
    page_icon="🏨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# CUSTOM CSS — Premium Dark Theme
# ─────────────────────────────────────────────

st.markdown("""
<style>
/* ── Global font ── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

/* ── Background ── */
.stApp { background: #0f1117; }
section[data-testid="stSidebar"] { background: #161b27 !important; border-right: 1px solid #2a2f3e; }

/* ── Sidebar title ── */
.sidebar-brand {
    text-align: center; padding: 20px 0 10px;
    font-size: 22px; font-weight: 700; color: #f8c471;
    letter-spacing: 0.5px;
}
.sidebar-subtitle { text-align: center; color: #7f8c8d; font-size: 12px; margin-bottom: 20px; }

/* ── Metric cards ── */
.metric-card {
    background: linear-gradient(135deg, #1e2436, #252d40);
    border: 1px solid #2e3650;
    border-radius: 14px;
    padding: 20px 24px;
    margin-bottom: 16px;
    position: relative;
    overflow: hidden;
    transition: transform 0.2s, box-shadow 0.2s;
}
.metric-card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.4); }
.metric-card::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
    background: linear-gradient(90deg, #f8c471, #e74c3c);
}
.metric-label { color: #95a0b0; font-size: 12px; font-weight: 500; text-transform: uppercase; letter-spacing: 1px; }
.metric-value { color: #ffffff; font-size: 32px; font-weight: 700; margin: 6px 0 2px; }
.metric-sub { color: #6b7280; font-size: 12px; }

/* ── Section headers ── */
.section-header {
    color: #f8c471; font-size: 20px; font-weight: 700;
    margin: 24px 0 16px; padding-bottom: 8px;
    border-bottom: 2px solid #2e3650;
    display: flex; align-items: center; gap: 8px;
}

/* ── Data cards ── */
.data-card {
    background: #1a2035; border: 1px solid #2a3050;
    border-radius: 12px; padding: 16px 20px;
    margin-bottom: 12px; transition: border-color 0.2s;
}
.data-card:hover { border-color: #f8c471; }

/* ── Status badges ── */
.badge-green  { background:#0d4429; color:#4ade80; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; }
.badge-yellow { background:#3f3000; color:#fbbf24; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; }
.badge-red    { background:#400e0e; color:#f87171; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; }
.badge-blue   { background:#0c2545; color:#60a5fa; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; }
.badge-purple { background:#2d1b50; color:#c084fc; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; }

/* ── Chat bubbles ── */
.chat-container { max-height: 500px; overflow-y: auto; padding: 10px; }
.msg-agent {
    background: linear-gradient(135deg, #1a3a5c, #1e4070);
    border-left: 3px solid #3b82f6;
    border-radius: 0 12px 12px 12px;
    padding: 10px 14px; margin: 8px 40px 8px 0; color: #e2e8f0;
}
.msg-guest {
    background: linear-gradient(135deg, #2d1f3d, #3d2855);
    border-right: 3px solid #a855f7;
    border-radius: 12px 0 12px 12px;
    padding: 10px 14px; margin: 8px 0 8px 40px; color: #e2e8f0;
}
.msg-role { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
.msg-agent .msg-role { color: #60a5fa; }
.msg-guest .msg-role { color: #c084fc; text-align: right; }
.msg-time { font-size: 10px; color: #6b7280; margin-top: 4px; }

/* ── Buttons ── */
.stButton > button {
    background: linear-gradient(135deg, #f8c471, #e67e22) !important;
    color: #1a1a2e !important; font-weight: 700 !important;
    border: none !important; border-radius: 8px !important;
    padding: 8px 20px !important; transition: all 0.2s !important;
}
.stButton > button:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(248,196,113,0.3) !important; }

/* ── Input fields ── */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div { background: #1e2436 !important; border: 1px solid #2e3650 !important; color: #e2e8f0 !important; border-radius: 8px !important; }

/* ── Expander ── */
.streamlit-expanderHeader {
    background: #1a2035 !important; border-radius: 8px !important;
    color: #e2e8f0 !important; border: 1px solid #2e3650 !important;
}

/* ── Table ── */
.stDataFrame { border-radius: 10px; overflow: hidden; }

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] { background: #161b27; border-radius: 10px; padding: 4px; }
.stTabs [data-baseweb="tab"] { color: #7f8c8d !important; border-radius: 8px; }
.stTabs [aria-selected="true"] { background: #252d40 !important; color: #f8c471 !important; }

/* ── Alert boxes ── */
.stSuccess { background: #0d2e1a !important; border: 1px solid #15803d !important; color: #4ade80 !important; }
.stError   { background: #2d0a0a !important; border: 1px solid #7f1d1d !important; color: #f87171 !important; }
.stWarning { background: #2d1f00 !important; border: 1px solid #78350f !important; color: #fbbf24 !important; }
.stInfo    { background: #0c2040 !important; border: 1px solid #1e40af !important; color: #60a5fa !important; }

/* ── Order items list ── */
.order-item {
    background: #252d40; border-radius: 8px;
    padding: 6px 12px; margin: 4px 0;
    display: flex; align-items: center; gap: 8px;
    color: #e2e8f0; font-size: 14px;
}
.order-dot { width: 8px; height: 8px; border-radius: 50%; background: #f8c471; flex-shrink: 0; }

/* ── Hotel selector card ── */
.hotel-selector {
    background: linear-gradient(135deg, #1e2436, #252d40);
    border: 1px solid #2e3650; border-radius: 12px; padding: 12px 16px;
    color: #e2e8f0; cursor: pointer;
}

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #161b27; }
::-webkit-scrollbar-thumb { background: #2e3650; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #f8c471; }

/* ── Divider ── */
hr { border-color: #2e3650 !important; }

/* ── Footer ── */
.footer { text-align: center; color: #4a5568; font-size: 11px; padding: 20px 0; margin-top: 40px; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# SESSION STATE & CONFIG
# ─────────────────────────────────────────────

if "api_base" not in st.session_state:
    st.session_state.api_base = "http://localhost:8000"
if "selected_hotel" not in st.session_state:
    st.session_state.selected_hotel = None
if "page" not in st.session_state:
    st.session_state.page = "🏠 Dashboard"


# ─────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────

def api(method: str, path: str, **kwargs) -> Optional[Dict]:
    url = f"{st.session_state.api_base}{path}"
    try:
        r = getattr(requests, method)(url, timeout=10, **kwargs)
        if r.status_code in (200, 201):
            return r.json()
        return {"error": r.text, "status": r.status_code}
    except requests.exceptions.ConnectionError:
        return {"error": "Cannot connect to backend. Is the server running?"}
    except Exception as e:
        return {"error": str(e)}


def api_health() -> bool:
    try:
        r = requests.get(f"{st.session_state.api_base}/health", timeout=3)
        return r.status_code == 200
    except:
        return False


def fmt_time(ts) -> str:
    if not ts:
        return "—"
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", ""))
            return dt.strftime("%d %b %Y, %I:%M %p")
        return str(ts)
    except:
        return str(ts)


def badge(text: str, color: str = "blue") -> str:
    return f'<span class="badge-{color}">{text}</span>'


def metric_card(label: str, value: str, sub: str = "", icon: str = "") -> str:
    return f"""
    <div class="metric-card">
        <div class="metric-label">{icon} {label}</div>
        <div class="metric-value">{value}</div>
        <div class="metric-sub">{sub}</div>
    </div>
    """


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────

with st.sidebar:
    st.markdown('<div class="sidebar-brand">🏨 Hotel AI</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-subtitle">Voice Assistant Dashboard</div>', unsafe_allow_html=True)

    # Server config
    with st.expander("⚙️ Server Config", expanded=False):
        base = st.text_input("API Base URL", value=st.session_state.api_base, key="api_base_input")
        if st.button("Connect", key="connect_btn"):
            st.session_state.api_base = base
            st.rerun()

    # Health indicator
    healthy = api_health()
    status_color = "#4ade80" if healthy else "#f87171"
    status_text  = "Online" if healthy else "Offline"
    st.markdown(f"""
        <div style="text-align:center; padding:8px; background:#1a2035;
             border-radius:8px; margin:8px 0; border:1px solid #2e3650;">
            <span style="color:{status_color}; font-size:12px;">⬤</span>
            <span style="color:#9ca3af; font-size:12px; margin-left:6px;">Backend: <b style="color:{status_color};">{status_text}</b></span>
        </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # Navigation
    st.markdown('<div style="color:#7f8c8d;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;padding:4px 0;">Navigation</div>', unsafe_allow_html=True)

    pages = [
        ("🏠", "Dashboard"),
        ("🏨", "Hotels"),
        ("📄", "Knowledge Base"),
        ("📞", "Call Management"),
        ("🍽️", "Food Orders"),
        ("🧹", "Room Cleaning"),
        ("💆", "Spa Services"),
        ("🪥", "Essential Needs"),
        ("❓", "Inquiries"),
        ("💬", "Conversation Logs"),
        ("📲", "WhatsApp"),
        ("🚀", "Outbound Calls"),
        ("📊", "Analytics"),
    ]

    for icon, name in pages:
        full_name = f"{icon} {name}"
        is_active = st.session_state.page == full_name
        btn_style = "background:#252d40;border:1px solid #f8c471;" if is_active else "background:transparent;border:1px solid transparent;"
        if st.button(f"{icon} {name}", key=f"nav_{name}", use_container_width=True):
            st.session_state.page = full_name
            st.rerun()

    st.markdown("---")

    # Hotel selector (used across pages)
    hotels_resp = api("get", "/admin/hotels")
    hotels = hotels_resp.get("hotels", []) if hotels_resp and "hotels" in hotels_resp else []

    if hotels:
        hotel_names = {h.get("hotel_name", h.get("hotel_id", "?")): h.get("hotel_id") for h in hotels}
        selected_name = st.selectbox(
            "🏨 Active Hotel",
            options=list(hotel_names.keys()),
            key="hotel_selector",
        )
        st.session_state.selected_hotel = hotel_names.get(selected_name)
    else:
        st.info("No hotels registered yet.")
        st.session_state.selected_hotel = None

    st.markdown(f'<div class="footer">Hotel AI v2.3.0<br>© 2025 All rights reserved</div>', unsafe_allow_html=True)


# ─────────────────────────────────────────────
# HELPER: get selected hotel details
# ─────────────────────────────────────────────

def get_hotel_detail(hotel_id: str) -> Dict:
    if not hotel_id:
        return {}
    resp = api("get", f"/admin/hotels/{hotel_id}")
    return resp if resp and "error" not in resp else {}


# ═══════════════════════════════════════════════════════
# PAGE: DASHBOARD
# ═══════════════════════════════════════════════════════

if st.session_state.page == "🏠 Dashboard":
    st.markdown('<div class="section-header">🏠 System Dashboard</div>', unsafe_allow_html=True)

    # Top metrics
    c1, c2, c3, c4 = st.columns(4)

    total_hotels = len(hotels)
    with c1:
        st.markdown(metric_card("Total Hotels", str(total_hotels), "Registered tenants", "🏨"), unsafe_allow_html=True)

    healthy_badge = "🟢 Online" if healthy else "🔴 Offline"
    with c2:
        st.markdown(metric_card("Backend Status", healthy_badge, st.session_state.api_base, "⚡"), unsafe_allow_html=True)

    # Count total food orders
    total_orders = 0
    total_calls  = 0
    if st.session_state.selected_hotel:
        fo = api("get", f"/admin/hotels/{st.session_state.selected_hotel}/food-orders")
        if fo and "guests" in fo:
            total_orders = sum(
                len(g.get("food_order", [])) for g in fo["guests"].values()
            )
        cl = api("get", f"/admin/hotels/{st.session_state.selected_hotel}/calls")
        if cl and "guests" in cl:
            total_calls = len(cl["guests"])

    with c3:
        st.markdown(metric_card("Food Orders", str(total_orders), "Current hotel", "🍽️"), unsafe_allow_html=True)
    with c4:
        st.markdown(metric_card("Total Calls", str(total_calls), "Call logs", "📞"), unsafe_allow_html=True)

    st.markdown("---")

    # Hotel overview table
    st.markdown('<div class="section-header">🏨 Registered Hotels</div>', unsafe_allow_html=True)
    if hotels:
        for h in hotels:
            c_a, c_b, c_c, c_d = st.columns([3, 2, 2, 1])
            with c_a:
                st.markdown(f"**{h.get('hotel_name', 'Unknown')}**")
                st.caption(f"ID: `{h.get('hotel_id', '—')}`")
            with c_b:
                st.markdown(f"📞 `{h.get('dialed_number', '—')}`")
            with c_c:
                st.markdown(f"👤 `{h.get('manager_contact', '—')}`")
            with c_d:
                st.markdown(badge("Active", "green"), unsafe_allow_html=True)
            st.markdown('<hr style="margin:8px 0;border-color:#2e3650;">', unsafe_allow_html=True)
    else:
        st.info("No hotels registered. Go to 🏨 Hotels to add one.")

    # Quick actions
    st.markdown('<div class="section-header">⚡ Quick Actions</div>', unsafe_allow_html=True)
    qa1, qa2, qa3, qa4 = st.columns(4)
    with qa1:
        if st.button("🏨 Register Hotel", use_container_width=True):
            st.session_state.page = "🏨 Hotels"
            st.rerun()
    with qa2:
        if st.button("📄 Upload PDF", use_container_width=True):
            st.session_state.page = "📄 Knowledge Base"
            st.rerun()
    with qa3:
        if st.button("🚀 Make Call", use_container_width=True):
            st.session_state.page = "🚀 Outbound Calls"
            st.rerun()
    with qa4:
        if st.button("📊 View Analytics", use_container_width=True):
            st.session_state.page = "📊 Analytics"
            st.rerun()

    # Health check details
    if healthy:
        health_resp = api("get", "/health")
        if health_resp:
            st.markdown('<div class="section-header">✅ Health Check</div>', unsafe_allow_html=True)
            st.json(health_resp)


# ═══════════════════════════════════════════════════════
# PAGE: HOTELS
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "🏨 Hotels":
    st.markdown('<div class="section-header">🏨 Hotel Management</div>', unsafe_allow_html=True)

    tab1, tab2, tab3 = st.tabs(["📋 All Hotels", "➕ Register New", "✏️ Update Hotel"])

    with tab1:
        if not hotels:
            st.info("No hotels registered yet.")
        else:
            for h in hotels:
                with st.expander(f"🏨 {h.get('hotel_name', '?')} — `{h.get('hotel_id', '?')}`"):
                    c1, c2 = st.columns(2)
                    with c1:
                        st.markdown("**Hotel Details**")
                        st.write(f"🆔 ID: `{h.get('hotel_id', '—')}`")
                        st.write(f"📞 Twilio Number: `{h.get('dialed_number', '—')}`")
                        st.write(f"📱 Hotel Number: `{h.get('hotel_number', '—')}`")
                        st.write(f"👤 Manager: `{h.get('manager_contact', '—')}`")
                    with c2:
                        st.markdown("**Contact & Location**")
                        st.write(f"📧 Email: `{h.get('hotel_email', '—')}`")
                        st.write(f"📍 Address: {h.get('hotel_address', '—')}")
                        created = fmt_time(h.get('created_at'))
                        st.write(f"🕒 Created: {created}")
                    if h.get("system_prompt"):
                        st.markdown("**System Prompt**")
                        st.text_area("System Prompt", value=h.get("system_prompt", ""), height=80, key=f"sp_{h['hotel_id']}", disabled=True, label_visibility="collapsed")

    with tab2:
        st.markdown("#### Register a New Hotel")
        with st.form("register_hotel_form"):
            c1, c2 = st.columns(2)
            with c1:
                hotel_name    = st.text_input("Hotel Name *", placeholder="Grand Royal Hotel")
                hotel_id_inp  = st.text_input("Hotel ID (unique slug) *", placeholder="grand_royal_001")
                hotel_number  = st.text_input("Hotel Phone Number *", placeholder="+911234567890")
                manager_contact = st.text_input("Manager Contact *", placeholder="+919876543210")
            with c2:
                hotel_address = st.text_input("Hotel Address *", placeholder="123 Main St, Indore")
                hotel_email   = st.text_input("Hotel Email *", placeholder="info@grandroyal.com")
                password      = st.text_input("Dashboard Password *", type="password")
                dialed_number = st.text_input("Twilio Number *", placeholder="+17479665797")

            system_prompt = st.text_area(
                "System Prompt (leave blank for auto-generated)",
                placeholder="You are a professional AI concierge for...",
                height=100,
            )

            submitted = st.form_submit_button("🏨 Register Hotel", use_container_width=True)
            if submitted:
                if not all([hotel_name, hotel_id_inp, hotel_number, manager_contact, hotel_address, hotel_email, password, dialed_number]):
                    st.error("Please fill all required (*) fields.")
                else:
                    payload = {
                        "hotel_name": hotel_name,
                        "hotel_id": hotel_id_inp,
                        "hotel_number": hotel_number,
                        "manager_contact": manager_contact,
                        "hotel_address": hotel_address,
                        "hotel_email": hotel_email,
                        "password": password,
                        "dialed_number": dialed_number,
                        "system_prompt": system_prompt,
                    }
                    resp = api("post", "/admin/hotels", json=payload)
                    if resp and "error" not in resp:
                        st.success(f"✅ Hotel '{hotel_name}' registered successfully! ID: `{hotel_id_inp}`")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(f"❌ {resp.get('error', 'Registration failed')}")

    with tab3:
        if not st.session_state.selected_hotel:
            st.warning("Select a hotel from the sidebar first.")
        else:
            hotel_detail = get_hotel_detail(st.session_state.selected_hotel)
            if hotel_detail:
                st.markdown(f"#### Update: {hotel_detail.get('hotel_name', '')}")
                with st.form("update_hotel_form"):
                    c1, c2 = st.columns(2)
                    with c1:
                        upd_name    = st.text_input("Hotel Name", value=hotel_detail.get("hotel_name", ""))
                        upd_number  = st.text_input("Hotel Number", value=hotel_detail.get("hotel_number", ""))
                        upd_manager = st.text_input("Manager Contact", value=hotel_detail.get("manager_contact", ""))
                    with c2:
                        upd_address = st.text_input("Address", value=hotel_detail.get("hotel_address", ""))
                        upd_email   = st.text_input("Email", value=hotel_detail.get("hotel_email", ""))
                    upd_prompt = st.text_area("System Prompt", value=hotel_detail.get("system_prompt", ""), height=120)

                    if st.form_submit_button("✅ Update Hotel", use_container_width=True):
                        payload = {
                            "hotel_name": upd_name,
                            "hotel_number": upd_number,
                            "manager_contact": upd_manager,
                            "hotel_address": upd_address,
                            "hotel_email": upd_email,
                            "system_prompt": upd_prompt,
                        }
                        resp = api("put", f"/admin/hotels/{st.session_state.selected_hotel}", json=payload)
                        if resp and "error" not in resp:
                            st.success("✅ Hotel updated successfully!")
                            st.rerun()
                        else:
                            st.error(f"❌ {resp.get('error', 'Update failed')}")


# ═══════════════════════════════════════════════════════
# PAGE: KNOWLEDGE BASE
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "📄 Knowledge Base":
    st.markdown('<div class="section-header">📄 Hotel Knowledge Base (PDF)</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("⚠️ Select a hotel from the sidebar to manage its knowledge base.")
    else:
        hotel_id = st.session_state.selected_hotel
        hotel_detail = get_hotel_detail(hotel_id)
        st.info(f"Managing knowledge base for: **{hotel_detail.get('hotel_name', hotel_id)}**")

        # View existing PDFs
        st.markdown("#### 📚 Uploaded PDFs")
        pdfs_resp = api("get", f"/admin/hotels/{hotel_id}/pdfs")
        if pdfs_resp and "pdfs" in pdfs_resp:
            pdfs_list = pdfs_resp.get("pdfs", [])
            if pdfs_list:
                total_chunks = sum(p.get("chunk_count", 0) for p in pdfs_list)
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.markdown(metric_card("PDFs Uploaded", str(len(pdfs_list)), f"Max 5", "📄"), unsafe_allow_html=True)
                with c2:
                    st.markdown(metric_card("Total Chunks", str(total_chunks), "In Qdrant vector DB", "🧩"), unsafe_allow_html=True)
                with c3:
                    remaining = max(0, 5 - len(pdfs_list))
                    st.markdown(metric_card("Slots Remaining", str(remaining), "PDFs can still be added", "🔓"), unsafe_allow_html=True)

                st.markdown("---")
                for i, pdf in enumerate(pdfs_list, 1):
                    c1, c2, c3 = st.columns([4, 2, 1])
                    with c1:
                        st.markdown(f"**{i}. {pdf.get('filename', '—')}**")
                        st.caption(f"Path: `{pdf.get('filepath', '—')}`")
                    with c2:
                        st.markdown(badge(f"{pdf.get('chunk_count', 0)} chunks", "blue"), unsafe_allow_html=True)
                        st.caption(f"Uploaded: {fmt_time(pdf.get('uploaded_at'))}")
                    with c3:
                        st.markdown(badge("Active", "green"), unsafe_allow_html=True)
                    st.markdown('<hr style="border-color:#2e3650;margin:6px 0;">', unsafe_allow_html=True)
            else:
                st.warning("No PDFs uploaded yet. Upload PDFs below to power the AI knowledge base.")
        else:
            st.warning("No PDF data found for this hotel.")

        st.markdown("---")

        # Upload new PDFs
        st.markdown("#### ⬆️ Upload New PDFs")
        st.markdown("""
        <div class="data-card">
            <div style="color:#94a3b8;font-size:13px;">
                📌 <b>Requirements:</b> PDF files only &nbsp;|&nbsp; Max 5 PDFs per hotel &nbsp;|&nbsp; 
                Content is automatically chunked → embedded → stored in Qdrant vector DB.<br>
                💡 <b>Tip:</b> Upload hotel menu, spa services, policies, amenities guide for best AI responses.
            </div>
        </div>
        """, unsafe_allow_html=True)

        uploaded_files = st.file_uploader(
            "Choose PDF files",
            type=["pdf"],
            accept_multiple_files=True,
            key="pdf_uploader",
        )

        if uploaded_files:
            st.write(f"Selected {len(uploaded_files)} file(s)")
            for f in uploaded_files:
                st.caption(f"📄 {f.name} — {f.size:,} bytes")

        if st.button("📤 Upload to Knowledge Base", use_container_width=True) and uploaded_files:
            with st.spinner("Uploading and processing PDFs... This may take a moment."):
                files_payload = [("files", (f.name, f.read(), "application/pdf")) for f in uploaded_files]
                try:
                    url = f"{st.session_state.api_base}/admin/hotels/{hotel_id}/upload-pdf"
                    r = requests.post(url, files=files_payload, timeout=120)
                    if r.status_code in (200, 201):
                        resp = r.json()
                        results = resp.get("results", [])
                        for res in results:
                            if res.get("status") == "ingested":
                                st.success(f"✅ {res['filename']} — {res['chunks']} chunks ingested")
                            else:
                                st.error(f"❌ {res['filename']} — {res.get('reason', 'Failed')}")
                        st.rerun()
                    else:
                        st.error(f"Upload failed: {r.text[:200]}")
                except Exception as e:
                    st.error(f"Upload error: {e}")


# ═══════════════════════════════════════════════════════
# PAGE: CALL MANAGEMENT
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "📞 Call Management":
    st.markdown('<div class="section-header">📞 Call Management</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar.")
    else:
        hotel_id = st.session_state.selected_hotel
        hotel_detail = get_hotel_detail(hotel_id)

        calls_resp = api("get", f"/admin/hotels/{hotel_id}/calls")
        guests_data = calls_resp.get("guests", {}) if calls_resp and "guests" in calls_resp else {}

        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(metric_card("Total Calls", str(len(guests_data)), "All time", "📞"), unsafe_allow_html=True)
        with c2:
            total_msgs = sum(len(g.get("conversation", [])) for g in guests_data.values())
            st.markdown(metric_card("Total Messages", str(total_msgs), "Across all calls", "💬"), unsafe_allow_html=True)
        with c3:
            active = sum(1 for g in guests_data.values() if g.get("call_sid"))
            st.markdown(metric_card("With Call SIDs", str(active), "Tracked calls", "🔗"), unsafe_allow_html=True)

        st.markdown("---")

        if not guests_data:
            st.info("No call logs yet. Make the first call using 🚀 Outbound Calls.")
        else:
            for guest_key, guest in guests_data.items():
                phone    = guest.get("guest_phone_number", "Unknown")
                room     = guest.get("guest_room_number", "?")
                call_sid = guest.get("call_sid", "")
                conv     = guest.get("conversation", [])
                started  = fmt_time(guest.get("started_at"))

                with st.expander(f"📞 {phone} | Room {room} | {len(conv)} messages"):
                    c1, c2, c3, c4 = st.columns(4)
                    with c1:
                        st.metric("Phone", phone)
                    with c2:
                        st.metric("Room", room or "Not set")
                    with c3:
                        st.metric("Messages", len(conv))
                    with c4:
                        st.metric("Started", started)
                    if call_sid:
                        st.markdown(f"**Call SID:** `{call_sid}`")
                    st.markdown("**Conversation:**")
                    if conv:
                        st.markdown('<div class="chat-container">', unsafe_allow_html=True)
                        for msg in conv:
                            ts = fmt_time(msg.get("timestamp"))
                            if "agent" in msg:
                                st.markdown(f"""
                                <div class="msg-agent">
                                    <div class="msg-role">🤖 Agent</div>
                                    {msg['agent']}
                                    <div class="msg-time">{ts}</div>
                                </div>""", unsafe_allow_html=True)
                            elif "guest" in msg:
                                st.markdown(f"""
                                <div class="msg-guest">
                                    <div class="msg-role">👤 Guest</div>
                                    {msg['guest']}
                                    <div class="msg-time">{ts}</div>
                                </div>""", unsafe_allow_html=True)
                        st.markdown('</div>', unsafe_allow_html=True)
                    else:
                        st.caption("No conversation turns recorded yet.")


# ═══════════════════════════════════════════════════════
# PAGE: FOOD ORDERS
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "🍽️ Food Orders":
    st.markdown('<div class="section-header">🍽️ Food Orders Tracker</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar.")
    else:
        hotel_id = st.session_state.selected_hotel
        resp = api("get", f"/admin/hotels/{hotel_id}/food-orders")

        if resp and "guests" in resp:
            guests = resp["guests"]
            all_items = []
            for g in guests.values():
                all_items.extend(g.get("food_order", []))

            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown(metric_card("Guests Who Ordered", str(len(guests)), "Unique guests", "👥"), unsafe_allow_html=True)
            with c2:
                st.markdown(metric_card("Total Items Ordered", str(len(all_items)), "All items", "🍽️"), unsafe_allow_html=True)
            with c3:
                from collections import Counter
                item_counts = Counter(all_items)
                top_item = item_counts.most_common(1)[0][0] if item_counts else "—"
                st.markdown(metric_card("Top Item", top_item[:20], "Most ordered", "⭐"), unsafe_allow_html=True)

            st.markdown("---")
            st.markdown("#### 📋 Order Details")

            for guest_key, guest in guests.items():
                phone = guest.get("guest_number", "Unknown")
                room  = guest.get("guest_room_number", "?")
                items = guest.get("food_order", [])

                st.markdown(f"""
                <div class="data-card">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                        <div>
                            <span style="color:#f8c471;font-weight:600;font-size:15px;">📱 {phone}</span>
                            &nbsp;&nbsp;
                            <span class="badge-blue">Room {room}</span>
                        </div>
                        <span class="badge-green">{len(items)} items</span>
                    </div>
                """, unsafe_allow_html=True)
                for item in items:
                    st.markdown(f'<div class="order-item"><div class="order-dot"></div>{item}</div>', unsafe_allow_html=True)
                st.markdown('</div>', unsafe_allow_html=True)

            # Item frequency chart
            if item_counts:
                st.markdown("---")
                st.markdown("#### 📊 Item Popularity")
                import pandas as pd
                df_items = pd.DataFrame(item_counts.most_common(10), columns=["Item", "Count"])
                st.bar_chart(df_items.set_index("Item"), color="#f8c471")
        else:
            st.info("No food orders yet for this hotel.")


# ═══════════════════════════════════════════════════════
# PAGE: ROOM CLEANING
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "🧹 Room Cleaning":
    st.markdown('<div class="section-header">🧹 Room Cleaning Requests</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar.")
    else:
        hotel_id = st.session_state.selected_hotel
        resp = api("get", f"/admin/hotels/{hotel_id}/cleaning")

        if resp and "guests" in resp:
            guests = resp["guests"]
            all_requests = []
            for g in guests.values():
                all_requests.extend(g.get("room_cleaning", []))

            c1, c2 = st.columns(2)
            with c1:
                st.markdown(metric_card("Requests From", str(len(guests)), "Unique guests", "👥"), unsafe_allow_html=True)
            with c2:
                st.markdown(metric_card("Total Requests", str(len(all_requests)), "Cleaning tasks", "🧹"), unsafe_allow_html=True)

            st.markdown("---")
            for guest_key, guest in guests.items():
                phone = guest.get("guest_number", "Unknown")
                room  = guest.get("guest_room_number", "?")
                reqs  = guest.get("room_cleaning", [])

                st.markdown(f"""
                <div class="data-card">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                        <div>
                            <span style="color:#4ade80;font-weight:600;font-size:15px;">📱 {phone}</span>
                            &nbsp;&nbsp;<span class="badge-blue">Room {room}</span>
                        </div>
                        <span class="badge-yellow">{len(reqs)} tasks</span>
                    </div>
                """, unsafe_allow_html=True)
                for r in reqs:
                    st.markdown(f'<div class="order-item"><div class="order-dot" style="background:#4ade80;"></div>{r}</div>', unsafe_allow_html=True)
                st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.info("No room cleaning requests yet.")


# ═══════════════════════════════════════════════════════
# PAGE: SPA SERVICES
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "💆 Spa Services":
    st.markdown('<div class="section-header">💆 Spa & Wellness Bookings</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar.")
    else:
        hotel_id = st.session_state.selected_hotel
        resp = api("get", f"/admin/hotels/{hotel_id}/spa")

        if resp and "guests" in resp:
            guests = resp["guests"]
            all_services = []
            for g in guests.values():
                all_services.extend(g.get("spa_services", []))

            c1, c2 = st.columns(2)
            with c1:
                st.markdown(metric_card("Guests Booked", str(len(guests)), "Unique guests", "👥"), unsafe_allow_html=True)
            with c2:
                st.markdown(metric_card("Total Services", str(len(all_services)), "Spa bookings", "💆"), unsafe_allow_html=True)

            st.markdown("---")
            for guest_key, guest in guests.items():
                phone    = guest.get("guest_number", "Unknown")
                room     = guest.get("guest_room_number", "?")
                services = guest.get("spa_services", [])

                st.markdown(f"""
                <div class="data-card">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                        <div>
                            <span style="color:#c084fc;font-weight:600;font-size:15px;">📱 {phone}</span>
                            &nbsp;&nbsp;<span class="badge-blue">Room {room}</span>
                        </div>
                        <span class="badge-purple">{len(services)} services</span>
                    </div>
                """, unsafe_allow_html=True)
                for s in services:
                    st.markdown(f'<div class="order-item"><div class="order-dot" style="background:#c084fc;"></div>{s}</div>', unsafe_allow_html=True)
                st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.info("No spa bookings yet.")


# ═══════════════════════════════════════════════════════
# PAGE: ESSENTIAL NEEDS
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "🪥 Essential Needs":
    st.markdown('<div class="section-header">🪥 Essential Needs & Amenities</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar.")
    else:
        hotel_id = st.session_state.selected_hotel
        resp = api("get", f"/admin/hotels/{hotel_id}/essentials")

        if resp and "guests" in resp:
            guests = resp["guests"]
            all_needs = []
            for g in guests.values():
                all_needs.extend(g.get("essential_needs", []))

            c1, c2 = st.columns(2)
            with c1:
                st.markdown(metric_card("Guests With Needs", str(len(guests)), "Unique requests", "👥"), unsafe_allow_html=True)
            with c2:
                st.markdown(metric_card("Total Items", str(len(all_needs)), "Amenity requests", "🪥"), unsafe_allow_html=True)

            st.markdown("---")
            for guest_key, guest in guests.items():
                phone = guest.get("guest_number", "Unknown")
                room  = guest.get("guest_room_number", "?")
                needs = guest.get("essential_needs", [])

                st.markdown(f"""
                <div class="data-card">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                        <div>
                            <span style="color:#fbbf24;font-weight:600;font-size:15px;">📱 {phone}</span>
                            &nbsp;&nbsp;<span class="badge-blue">Room {room}</span>
                        </div>
                        <span class="badge-yellow">{len(needs)} items</span>
                    </div>
                """, unsafe_allow_html=True)
                for n in needs:
                    st.markdown(f'<div class="order-item"><div class="order-dot" style="background:#fbbf24;"></div>{n}</div>', unsafe_allow_html=True)
                st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.info("No essential needs requests yet.")


# ═══════════════════════════════════════════════════════
# PAGE: INQUIRIES
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "❓ Inquiries":
    st.markdown('<div class="section-header">❓ Guest Inquiries</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar.")
    else:
        hotel_id = st.session_state.selected_hotel
        resp = api("get", f"/admin/hotels/{hotel_id}/inquiries")

        if resp and "guests" in resp:
            guests = resp["guests"]
            all_questions = []
            for g in guests.values():
                all_questions.extend([q.get("question", "") for q in g.get("inquiry", [])])

            c1, c2 = st.columns(2)
            with c1:
                st.markdown(metric_card("Guests Inquired", str(len(guests)), "Unique guests", "👥"), unsafe_allow_html=True)
            with c2:
                st.markdown(metric_card("Total Questions", str(len(all_questions)), "Questions asked", "❓"), unsafe_allow_html=True)

            st.markdown("---")
            for guest_key, guest in guests.items():
                phone     = guest.get("guest_number", "Unknown")
                room      = guest.get("guest_room_number", "?")
                inquiries = guest.get("inquiry", [])

                with st.expander(f"❓ {phone} — {len(inquiries)} question(s)"):
                    st.markdown(f"📱 **{phone}** | Room **{room}**")
                    for i, inq in enumerate(inquiries, 1):
                        q  = inq.get("question", "—")
                        ts = fmt_time(inq.get("timestamp"))

                        # Classify inquiry type
                        q_lower = q.lower()
                        if "[escalation]" in q_lower:
                            b = badge("Escalation", "red")
                        elif "[event_inquiry]" in q_lower:
                            b = badge("Event", "purple")
                        elif any(w in q_lower for w in ["food", "menu", "breakfast", "lunch", "dinner"]):
                            b = badge("Food/Menu", "yellow")
                        elif any(w in q_lower for w in ["spa", "massage", "wellness"]):
                            b = badge("Spa", "purple")
                        elif any(w in q_lower for w in ["wifi", "pool", "gym", "amenity"]):
                            b = badge("Amenities", "blue")
                        else:
                            b = badge("General", "blue")

                        st.markdown(f"""
                        <div class="data-card">
                            <div style="display:flex;justify-content:space-between;align-items:center;">
                                <div style="color:#e2e8f0;font-size:14px;">{i}. {q}</div>
                                {b}
                            </div>
                            <div style="color:#6b7280;font-size:11px;margin-top:4px;">{ts}</div>
                        </div>
                        """, unsafe_allow_html=True)
        else:
            st.info("No inquiries recorded yet.")


# ═══════════════════════════════════════════════════════
# PAGE: CONVERSATION LOGS
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "💬 Conversation Logs":
    st.markdown('<div class="section-header">💬 Full Conversation Logs</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar.")
    else:
        hotel_id = st.session_state.selected_hotel
        hotel_detail = get_hotel_detail(hotel_id)

        calls_resp = api("get", f"/admin/hotels/{hotel_id}/calls")
        guests_data = calls_resp.get("guests", {}) if calls_resp and "guests" in calls_resp else {}

        if not guests_data:
            st.info("No conversations recorded yet.")
        else:
            # Guest selector
            guest_options = {}
            for gk, gv in guests_data.items():
                phone = gv.get("guest_phone_number", "Unknown")
                room  = gv.get("guest_room_number", "?")
                key   = f"{phone} (Room {room})"
                guest_options[key] = gk

            selected_guest_label = st.selectbox("Select Guest", list(guest_options.keys()), key="conv_guest_select")
            selected_guest_key   = guest_options.get(selected_guest_label)
            guest_data           = guests_data.get(selected_guest_key, {})

            if guest_data:
                phone    = guest_data.get("guest_phone_number", "Unknown")
                room     = guest_data.get("guest_room_number", "?")
                call_sid = guest_data.get("call_sid", "")
                conv     = guest_data.get("conversation", [])

                # Info bar
                cols = st.columns(4)
                cols[0].metric("📱 Phone", phone)
                cols[1].metric("🚪 Room", room or "—")
                cols[2].metric("💬 Messages", len(conv))
                cols[3].metric("🔗 Call SID", (call_sid[:12] + "...") if len(call_sid) > 12 else call_sid or "—")

                # Intent stats from conversation
                agent_msgs = [m.get("agent", "") for m in conv if "agent" in m]
                guest_msgs = [m.get("guest", "") for m in conv if "guest" in m]

                st.markdown("---")

                # Search
                search_q = st.text_input("🔍 Search conversation", placeholder="Search messages...", key="conv_search")

                st.markdown('<div class="chat-container">', unsafe_allow_html=True)
                for msg in conv:
                    ts = fmt_time(msg.get("timestamp"))
                    if "agent" in msg:
                        content = msg["agent"]
                        if search_q and search_q.lower() not in content.lower():
                            continue
                        st.markdown(f"""
                        <div class="msg-agent">
                            <div class="msg-role">🤖 AI Concierge</div>
                            {content}
                            <div class="msg-time">{ts}</div>
                        </div>""", unsafe_allow_html=True)
                    elif "guest" in msg:
                        content = msg["guest"]
                        if search_q and search_q.lower() not in content.lower():
                            continue
                        st.markdown(f"""
                        <div class="msg-guest">
                            <div class="msg-role">👤 Guest</div>
                            {content}
                            <div class="msg-time">{ts}</div>
                        </div>""", unsafe_allow_html=True)
                st.markdown('</div>', unsafe_allow_html=True)

                if st.button("📥 Export Conversation"):
                    lines = []
                    for msg in conv:
                        ts = fmt_time(msg.get("timestamp"))
                        if "agent" in msg:
                            lines.append(f"[{ts}] AGENT: {msg['agent']}")
                        elif "guest" in msg:
                            lines.append(f"[{ts}] GUEST: {msg['guest']}")
                    txt = "\n".join(lines)
                    st.download_button("⬇️ Download .txt", txt, f"conversation_{phone}.txt", "text/plain")


# ═══════════════════════════════════════════════════════
# PAGE: WHATSAPP
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "📲 WhatsApp":
    st.markdown('<div class="section-header">📲 WhatsApp Integration</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="data-card">
        <div style="color:#4ade80;font-size:14px;font-weight:600;margin-bottom:8px;">✅ WhatsApp Features</div>
        <div style="color:#94a3b8;font-size:13px;">
            • Post-call summaries sent automatically after call ends<br>
            • Inbound WhatsApp messages handled via LLM (hotel-only scope)<br>
            • "Call me" / "Call karo" → triggers outbound Twilio call automatically<br>
            • Final order confirmation sent when guest replies after summary<br>
            • All sessions stored in Redis (7-day TTL)
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Webhook config info
    if st.session_state.selected_hotel:
        hotel_detail = get_hotel_detail(st.session_state.selected_hotel)
        ngrok_url = st.text_input(
            "Your Ngrok URL (for Twilio webhook config)",
            placeholder="https://abc123.ngrok.io",
            key="ngrok_url_input",
        )
        if ngrok_url:
            webhook_url = f"{ngrok_url}/whatsapp/incoming"
            st.markdown(f"""
            <div class="data-card">
                <div style="color:#fbbf24;font-size:13px;font-weight:600;">📌 Configure in Twilio Console:</div>
                <div style="color:#e2e8f0;font-family:monospace;margin-top:8px;background:#0f1117;padding:10px;border-radius:6px;">
                    Webhook URL: {webhook_url}<br>
                    Method: POST
                </div>
            </div>
            """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("#### 📊 WhatsApp Session Info")
    st.markdown("""
    <div class="data-card">
        <div style="color:#94a3b8;font-size:13px;">
            WhatsApp sessions are managed in Redis. Each guest's WA conversation is stored with:<br><br>
            <b style="color:#e2e8f0;">Key format:</b> <code>wa_session:{hotel_id}:{guest_number}</code><br>
            <b style="color:#e2e8f0;">TTL:</b> 7 days<br>
            <b style="color:#e2e8f0;">Fields:</b> hotel_id, hotel_name, manager_contact, summary_sent, order_items, history[]<br><br>
            <b style="color:#fbbf24;">⚠️ Note:</b> WhatsApp session data is stored in Redis, not MongoDB. 
            Use Redis CLI to inspect: <code>KEYS wa_session:*</code>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("#### 🔧 Call-Request Trigger Patterns")
    patterns = [
        "call me", "call kar", "call karo", "phone karo", "call back", "firse call",
        "agent se baat", "manager se baat", "connect karo", "baat karni hai",
        "please call", "mujhe call", "speak to someone", "human se baat"
    ]
    cols = st.columns(3)
    for i, p in enumerate(patterns):
        with cols[i % 3]:
            st.markdown(badge(p, "blue"), unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
# PAGE: OUTBOUND CALLS
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "🚀 Outbound Calls":
    st.markdown('<div class="section-header">🚀 Outbound Call Trigger</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="data-card">
        <div style="color:#94a3b8;font-size:13px;">
            Trigger an AI-powered outbound call to a guest. The call goes through Twilio → 
            your webhook → WebSocket pipeline → AI agent speaks with the guest.
        </div>
    </div>
    """, unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar first.")
    else:
        hotel_detail = get_hotel_detail(st.session_state.selected_hotel)
        twilio_num   = hotel_detail.get("dialed_number", "—")

        st.markdown(f"**Calling From:** `{twilio_num}` ({hotel_detail.get('hotel_name', '')})")
        st.markdown("---")

        with st.form("outbound_call_form"):
            to_number = st.text_input(
                "Guest Phone Number (E.164 format) *",
                placeholder="+919876543210",
                help="Must include country code e.g. +91 for India"
            )
            ngrok_url = st.text_input(
                "Ngrok URL *",
                placeholder="https://abc123.ngrok.io",
                help="Your currently running ngrok tunnel URL"
            )
            st.markdown("""
            <div style="color:#6b7280;font-size:12px;margin-top:8px;">
                ℹ️ This uses your backend's Twilio credentials to initiate the call.
                Make sure your ngrok tunnel and server are both running.
            </div>
            """, unsafe_allow_html=True)

            submitted = st.form_submit_button("📞 Trigger AI Call", use_container_width=True)
            if submitted:
                if not to_number or not ngrok_url:
                    st.error("Both fields are required.")
                elif not to_number.startswith("+"):
                    st.error("Phone number must start with + and country code (e.g. +919876543210)")
                else:
                    # Make the Twilio API call via your backend (using Twilio REST through backend env)
                    try:
                        from twilio.rest import Client
                        st.warning("⚠️ Direct Twilio call requires running the test.py script on the server with proper credentials.")
                        st.code(f"python test.py {to_number}", language="bash")
                        st.info(f"OR: Run from your terminal → `python test.py {to_number}`")
                    except ImportError:
                        # Show the CLI command instead
                        st.info(f"Run this command from your project root:")
                        st.code(f"python test.py {to_number}", language="bash")
                        st.markdown(f"""
                        <div class="data-card">
                            <div style="color:#4ade80;font-weight:600;">📋 Call Details</div>
                            <div style="color:#94a3b8;margin-top:8px;">
                                To: <b style="color:#e2e8f0;">{to_number}</b><br>
                                From: <b style="color:#e2e8f0;">{twilio_num}</b><br>
                                Webhook: <b style="color:#e2e8f0;">{ngrok_url}/incoming-call</b>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("#### 📋 Recent Callers")
        calls_resp = api("get", f"/admin/hotels/{st.session_state.selected_hotel}/calls")
        if calls_resp and "guests" in calls_resp:
            for gk, gv in calls_resp["guests"].items():
                phone = gv.get("guest_phone_number", "?")
                room  = gv.get("guest_room_number", "?")
                msgs  = len(gv.get("conversation", []))
                c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
                c1.write(f"📱 `{phone}`")
                c2.write(f"🚪 Room {room}")
                c3.write(f"💬 {msgs} messages")
                with c4:
                    if st.button("📞 Call Again", key=f"call_again_{gk}"):
                        st.info(f"Run: `python test.py {phone}`")
        else:
            st.caption("No past calls found.")


# ═══════════════════════════════════════════════════════
# PAGE: ANALYTICS
# ═══════════════════════════════════════════════════════

elif st.session_state.page == "📊 Analytics":
    import pandas as pd
    from collections import Counter, defaultdict

    st.markdown('<div class="section-header">📊 Analytics & Insights</div>', unsafe_allow_html=True)

    if not st.session_state.selected_hotel:
        st.warning("Select a hotel from the sidebar.")
    else:
        hotel_id     = st.session_state.selected_hotel
        hotel_detail = get_hotel_detail(hotel_id)
        hotel_name   = hotel_detail.get("hotel_name", hotel_id)

        st.markdown(f"#### 📈 Analytics for: **{hotel_name}**")

        # Fetch all data in parallel
        fo   = api("get", f"/admin/hotels/{hotel_id}/food-orders")
        rc   = api("get", f"/admin/hotels/{hotel_id}/cleaning")
        spa  = api("get", f"/admin/hotels/{hotel_id}/spa")
        ess  = api("get", f"/admin/hotels/{hotel_id}/essentials")
        inq  = api("get", f"/admin/hotels/{hotel_id}/inquiries")
        calls_data = api("get", f"/admin/hotels/{hotel_id}/calls")
        pdfs_data  = api("get", f"/admin/hotels/{hotel_id}/pdfs")

        # Summary metrics
        fo_guests  = len(fo.get("guests", {})) if fo and "guests" in fo else 0
        rc_guests  = len(rc.get("guests", {})) if rc and "guests" in rc else 0
        spa_guests = len(spa.get("guests", {})) if spa and "guests" in spa else 0
        ess_guests = len(ess.get("guests", {})) if ess and "guests" in ess else 0
        inq_guests = len(inq.get("guests", {})) if inq and "guests" in inq else 0
        call_count = len(calls_data.get("guests", {})) if calls_data and "guests" in calls_data else 0
        pdf_count  = len(pdfs_data.get("pdfs", [])) if pdfs_data and "pdfs" in pdfs_data else 0

        all_fo_items = []
        if fo and "guests" in fo:
            for g in fo["guests"].values():
                all_fo_items.extend(g.get("food_order", []))

        all_inquiries = []
        if inq and "guests" in inq:
            for g in inq["guests"].values():
                for iq in g.get("inquiry", []):
                    all_inquiries.append(iq.get("question", ""))

        # Engagement overview chart
        st.markdown("#### 🎯 Service Engagement Overview")
        engagement_data = {
            "Food Orders": fo_guests,
            "Room Cleaning": rc_guests,
            "Spa Services": spa_guests,
            "Essential Needs": ess_guests,
            "Inquiries": inq_guests,
            "Total Calls": call_count,
        }
        df_eng = pd.DataFrame(list(engagement_data.items()), columns=["Service", "Count"])
        st.bar_chart(df_eng.set_index("Service"), color="#f8c471")

        st.markdown("---")
        c1, c2 = st.columns(2)

        with c1:
            # Top food items
            st.markdown("#### 🍽️ Most Ordered Food Items")
            if all_fo_items:
                fc = Counter(all_fo_items)
                df_food = pd.DataFrame(fc.most_common(8), columns=["Item", "Orders"])
                st.bar_chart(df_food.set_index("Item"), color="#f8c471")
            else:
                st.caption("No food order data yet.")

        with c2:
            # Inquiry categories
            st.markdown("#### ❓ Inquiry Categories")
            categories = defaultdict(int)
            for q in all_inquiries:
                q_lower = q.lower()
                if "[escalation]" in q_lower:
                    categories["Escalation"] += 1
                elif "[event_inquiry]" in q_lower:
                    categories["Event Inquiry"] += 1
                elif any(w in q_lower for w in ["food", "menu", "breakfast", "lunch", "dinner", "drink"]):
                    categories["Food/Menu"] += 1
                elif any(w in q_lower for w in ["spa", "massage", "wellness", "gym"]):
                    categories["Spa/Wellness"] += 1
                elif any(w in q_lower for w in ["wifi", "pool", "parking", "amenity"]):
                    categories["Amenities"] += 1
                elif any(w in q_lower for w in ["price", "rate", "cost", "charge"]):
                    categories["Pricing"] += 1
                elif any(w in q_lower for w in ["time", "hour", "open", "close", "timing"]):
                    categories["Timings"] += 1
                else:
                    categories["General"] += 1

            if categories:
                df_cat = pd.DataFrame(list(categories.items()), columns=["Category", "Count"])
                st.bar_chart(df_cat.set_index("Category"), color="#c084fc")
            else:
                st.caption("No inquiry data yet.")

        st.markdown("---")

        # Guest activity table
        st.markdown("#### 👥 Guest Activity Summary")
        guest_activity = {}

        for src_name, src_data, field in [
            ("Food", fo, "food_order"),
            ("Cleaning", rc, "room_cleaning"),
            ("Spa", spa, "spa_services"),
            ("Essentials", ess, "essential_needs"),
        ]:
            if src_data and "guests" in src_data:
                for gv in src_data["guests"].values():
                    phone = gv.get("guest_number", "Unknown")
                    room  = gv.get("guest_room_number", "?")
                    if phone not in guest_activity:
                        guest_activity[phone] = {"Room": room, "Food": 0, "Cleaning": 0, "Spa": 0, "Essentials": 0}
                    guest_activity[phone][src_name] += len(gv.get(field, []))

        if guest_activity:
            df_guests = pd.DataFrame.from_dict(guest_activity, orient="index")
            df_guests.index.name = "Phone"
            df_guests = df_guests.reset_index()
            st.dataframe(df_guests, use_container_width=True, hide_index=True)
        else:
            st.caption("No guest activity data yet.")

        st.markdown("---")

        # PDF knowledge base stats
        st.markdown("#### 📚 Knowledge Base Stats")
        if pdfs_data and "pdfs" in pdfs_data:
            pdfs_list = pdfs_data["pdfs"]
            df_pdfs = pd.DataFrame([{
                "Filename": p.get("filename", "—").split("_", 2)[-1] if "_" in p.get("filename", "") else p.get("filename"),
                "Chunks": p.get("chunk_count", 0),
                "Uploaded": fmt_time(p.get("uploaded_at"))
            } for p in pdfs_list])
            st.dataframe(df_pdfs, use_container_width=True, hide_index=True)
            total_chunks = sum(p.get("chunk_count", 0) for p in pdfs_list)
            st.metric("Total Vector Embeddings in Qdrant", total_chunks)
        else:
            st.caption("No PDFs uploaded yet.")


# ─────────────────────────────────────────────
# Footer (always shown)
# ─────────────────────────────────────────────

st.markdown("""
<div class="footer">
    🏨 Hotel AI Voice Assistant Dashboard &nbsp;|&nbsp; v2.3.0 &nbsp;|&nbsp;
    Built with FastAPI · Twilio · Deepgram · Qwen · LangGraph · Qdrant · MongoDB · Redis
</div>
""", unsafe_allow_html=True)