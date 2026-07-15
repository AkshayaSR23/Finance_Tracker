"""
Simple Finance Tracker — Streamlit App
Run with:  streamlit run finance_tracker.py
"""

import streamlit as st
from streamlit.errors import StreamlitAPIException
import json
import os
import uuid
import calendar
from datetime import datetime, date
import psycopg2
import psycopg2.extras
import psycopg2.pool

# ----------------------------------------------------------------------
# CONFIG / CONSTANTS
# ----------------------------------------------------------------------
# Connection string comes from Neon (see secrets.toml), never a local file.
DATABASE_URL = st.secrets["neon"]["database_url"]


@st.cache_resource
def get_pool():
    """A small pool of already-open connections to Neon, created once and
    reused for the lifetime of the app. Without this, every get_connection()
    call would pay the full cost of a fresh network handshake to Neon's
    server (in Singapore) — the pool avoids that by keeping connections
    open and handing them out/back as needed.

    Kept small (a handful, not dozens) since this app is used by one
    person — but NOT set to exactly 1: Streamlit can briefly run two
    script executions back-to-back (e.g. a quick second click before the
    first rerun finishes), and with zero spare capacity that instantly
    exhausts the pool. A small buffer costs nothing extra — Neon bills
    for active query time, not for how many idle connections are held —
    so there's no downside to a bit of headroom here."""
    return psycopg2.pool.ThreadedConnectionPool(
        1, 5, DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
    )


def get_connection():
    """Borrow a connection from the pool. Before handing it back, do a
    cheap health check (SELECT 1). If the app has been idle long enough
    that Neon's compute went to sleep and later closed out old sessions,
    the pooled connection may be dead even though it looks fine to us —
    this catches that and transparently swaps in a fresh one (a new
    handshake), rather than surfacing a confusing error deep inside
    whatever query happens to run first after waking up."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        probe = conn.cursor()
        probe.execute("SELECT 1")
        probe.close()
    except Exception:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    return conn


def release_connection(conn):
    """Return a connection to the pool instead of closing it outright,
    so it can be reused by the next query."""
    get_pool().putconn(conn)


@st.cache_resource
def initialize_database():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users(
            username TEXT PRIMARY KEY,
            email TEXT,
            budget REAL DEFAULT 5000,
            google_id TEXT,
            name TEXT,
            picture TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses(
            id TEXT PRIMARY KEY,
            category TEXT,
            "desc" TEXT,
            amount REAL,
            date TEXT,
            count INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history(
            id TEXT PRIMARY KEY,
            expense_id TEXT,
            category TEXT,
            "desc" TEXT,
            paid_amount REAL,
            date TEXT
        )
    """)

    # Per-user data isolation: each expense/history row now belongs to a
    # user. One-time cleanup: old rows predate this and were test data,
    # so we clear them out instead of migrating (clean slate, not tied
    # to any account).
    cursor.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'expenses'
    """)
    existing_cols = [r["column_name"] for r in cursor.fetchall()]
    if "username" not in existing_cols:
        cursor.execute("DELETE FROM history")
        cursor.execute("DELETE FROM expenses")
        cursor.execute("ALTER TABLE expenses ADD COLUMN username TEXT")
        cursor.execute("ALTER TABLE history ADD COLUMN username TEXT")

    # NEW: indexes so History lookups (by expense, date, category)
    # stay fast as the table grows — no schema/behavior change, just
    # faster reads.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_expense_id ON history(expense_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_date ON history(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_category ON history(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_username ON history(username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_expenses_username ON expenses(username)")

    conn.commit()
    release_connection(conn)


initialize_database()

CATEGORIES = ["Food", "Studies", "Miscellaneous"]
CATEGORY_ICONS = {"Food": "🍔", "Studies": "📚", "Miscellaneous": "📦"}

# Per-category accent palette (accent line + icon chip tint) so each
# block reads as its own color, like the reference fintech UIs.
CATEGORY_COLORS = {
    "Food":          {"accent": "#F59E0B", "tint": "#FFFBF2", "border": "#FCE1B0", "grad": ("#FBBF24", "#F59E0B")},
    "Studies":       {"accent": "#6366F1", "tint": "#F6F7FF", "border": "#CBD1FB", "grad": ("#818CF8", "#6366F1")},
    "Miscellaneous": {"accent": "#06B6D4", "tint": "#F1FCFE", "border": "#AEEAF3", "grad": ("#22D3EE", "#06B6D4")},
}
DEFAULT_ACCENT = "#0D9488"

# Cheerful cycling palette for month cards (indexed by month number).
MONTH_PALETTE = ["#6366F1", "#EC4899", "#F59E0B", "#10B981", "#06B6D4", "#8B5CF6",
                 "#EF4444", "#14B8A6", "#F97316", "#3B82F6", "#A855F7", "#22C55E"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

st.set_page_config(page_title="Finance Tracker", page_icon="💰", layout="centered")

# ----------------------------------------------------------------------
# SESSION STATE DEFAULTS
# ----------------------------------------------------------------------
defaults = {
    "logged_in": False,
    "username": None,
    "page": "Home",
    "selected_category": None,
    "selected_year": None,
    "expanded_month": None,
    "edit_history": None,
    "previous_page": "Home",
    "show_add_expense": False,
    "add_expense_category_lock": None,
    # NEW: consolidated UI state instead of many per-row session keys
    # (show_edit_<id>, show_delete_<id>, confirm_delete_hist_<id>, ...).
    # Only one row-level action can be open at a time.
    "category_action": {"type": None, "id": None},
    "history_delete_confirm_id": None,
    # Toast queued across a rerun. Toasts float over the UI instead of a
    # banner that pushes content down, so nothing "twitches" on confirm.
    "pending_toast": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


def queue_toast(msg, icon="✅"):
    """Queue a toast to show after the next rerun (full or fragment)."""
    st.session_state.pending_toast = (msg, icon)


def flush_toast():
    if st.session_state.pending_toast:
        msg, icon = st.session_state.pending_toast
        st.toast(msg, icon=icon)
        st.session_state.pending_toast = None


def _frag_rerun():
    """Rerun ONLY the current fragment so the rest of the page doesn't
    repaint (no flicker/twitch). Falls back to a normal rerun in the rare
    context where a fragment-scoped rerun isn't valid."""
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()


# ----------------------------------------------------------------------
# THEME
# ----------------------------------------------------------------------
def apply_theme():
    base_css = """
    <style>

    /* ---------- Main App ---------- */
    .stApp{
        background:#F4F7F8;
        color:#0F1B2A;
    }
    html, body, [class*="css"]{ color:#0F1B2A; }

    /* ---------- Type scale (secondary text uses solid, higher-contrast slate) ---------- */
    .app-page-title{ font-size:23px; font-weight:800; margin-bottom:2px; letter-spacing:-0.01em; color:#0F172A; }
    .app-section-title{ font-size:17px; font-weight:700; margin:6px 0 12px 0; color:#1E293B; }
    .stat-label{ font-size:14px; color:#475569; font-weight:600; }
    .stat-value{ font-size:32px; font-weight:800; line-height:1.15; margin-top:2px; letter-spacing:-0.02em; color:#0F172A; }
    .stat-sub{ font-size:14px; color:#475569; margin-top:6px; }
    .cat-name{ font-size:17px; font-weight:700; color:#0F172A; }
    .cat-sub{ font-size:14px; color:#475569; margin-top:3px; }
    .cell-value{ font-size:15px; font-weight:600; color:#334155; white-space:nowrap; text-align:right; }
    .row-title{ font-size:16px; font-weight:700; color:#0F172A; }
    .row-sub{ font-size:14px; color:#475569; margin-top:2px; }
    .table-cell{ font-size:15px; color:#1E293B; }
    .table-header{ font-size:13px; font-weight:700; color:#475569; text-transform:uppercase; letter-spacing:0.04em; }
    .budget-label{ font-size:16px; font-weight:700; }
    .budget-sub{ font-size:14px; color:#475569; margin-top:6px; }

    /* ---------- White cards everywhere (native bordered containers) ---------- */
    div[data-testid="stVerticalBlockBorderWrapper"]{
        background:#FFFFFF;
        border-radius:20px !important;
        border:1px solid #E9EEF1 !important;
        box-shadow:0 1px 2px rgba(15,27,42,0.04), 0 12px 30px -18px rgba(15,27,42,0.12);
    }

    /* ---------- Monthly Budget card: the ONE colored card, like the reference ---------- */
    .st-key-budget_card{
        background:linear-gradient(135deg,#2AC4D4 0%,#12A6BA 55%,#0E93A6 100%) !important;
        border:none !important;
        box-shadow:0 18px 40px -18px rgba(18,166,186,0.6) !important;
    }
    .st-key-budget_card .app-section-title{ color:#FFFFFF; }

    /* ---------- Reusable stat / hero card ---------- */
    .stat-card{
        border-radius:20px;
        padding:22px 24px;
        margin-bottom:6px;
        background:#FFFFFF;
        border:1px solid #E9EEF1;
        box-shadow:0 1px 2px rgba(15,27,42,0.04), 0 12px 30px -18px rgba(15,27,42,0.12);
    }
    /* Plain by default now — only the Monthly Budget card carries color */
    .stat-card.hero{
        border:1px solid #E9EEF1;
        color:#0F172A;
        background:#FFFFFF !important;
        box-shadow:0 1px 2px rgba(15,27,42,0.04), 0 12px 30px -18px rgba(15,27,42,0.12);
    }
    .stat-card.hero .stat-label{ color:#475569; opacity:1; }
    .stat-card.hero .stat-value{ color:#0F172A; }
    .stat-card.hero .stat-sub{ color:#475569; }

    /* Plain, card-less stat block (e.g. home page balance) */
    .stat-plain{
        border:none;
        background:transparent !important;
        box-shadow:none;
        padding:2px 2px 4px 2px;
        margin-bottom:0;
    }
    .stat-plain .stat-label{ color:#64748B; }
    .stat-plain .stat-value{ color:#0F172A; }

    /* ---------- Icon chip (neutral rounded square, like the reference) ---------- */
    .cell-lead{ display:flex; align-items:center; gap:12px; min-width:0; }
    .cell-lead .cat-name{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .chip{
        display:inline-flex; align-items:center; justify-content:center;
        width:48px; height:48px; border-radius:14px; font-size:23px; flex:0 0 auto;
        background:#EEF2F5 !important; color:#0F1B2A;
    }

    /* ---------- Buttons ---------- */
    .stButton>button{
        border-radius:14px;
        font-weight:600;
        transition:all .15s ease;
    }
    .stButton>button[kind="secondary"],
    button[kind="secondaryFormSubmit"]{
        background:#FFFFFF;
        color:#1E2A33;
        border:1px solid #DCE3E8;
    }
    .stButton>button[kind="secondary"]:hover,
    button[kind="secondaryFormSubmit"]:hover{
        background:#F4F7F8;
        color:#0F1B2A;
        border:1px solid #12A6BA;
    }
    /* Primary = near-black pill (the reference "Save" button) */
    .stButton>button[kind="primary"],
    button[kind="primaryFormSubmit"]{
        background:#1E2A33;
        color:white;
        border:none;
        box-shadow:0 10px 22px -10px rgba(30,42,51,0.55);
    }
    .stButton>button[kind="primary"]:hover,
    button[kind="primaryFormSubmit"]:hover{
        background:#0F1720;
        color:white;
    }

    /* ---------- Destructive ("danger") buttons ---------- */
    .st-key-danger_btn button{
        background:#EF4444 !important;
        color:white !important;
        border:none !important;
        border-radius:14px !important;
        box-shadow:0 10px 22px -10px rgba(239,68,68,0.55) !important;
    }
    .st-key-danger_btn button:hover{ background:#DC2626 !important; }

    /* ---------- Pill "Back" buttons ---------- */
    .st-key-back_to_months button,
    .st-key-back_to_categories button,
    .st-key-profile_back button{
        background:#FFFFFF !important;
        color:#1E2A33 !important;
        border:1px solid #DCE3E8 !important;
        border-radius:999px !important;
        padding:6px 18px !important;
        font-weight:700 !important;
        box-shadow:0 4px 12px -8px rgba(15,27,42,0.25) !important;
    }
    .st-key-back_to_months button:hover,
    .st-key-back_to_categories button:hover,
    .st-key-profile_back button:hover{
        background:#F4F7F8 !important;
        border-color:#12A6BA !important;
        color:#0E93A6 !important;
        transform:translateX(-3px);
    }

    /* ---------- Chevron "open" buttons on category + month cards ---------- */
    [class*="st-key-open_"] button{
        background:#EEF2F5 !important;
        color:#1E2A33 !important;
        border:1px solid #E2E8EC !important;
        border-radius:12px !important;
        min-height:0 !important;
    }
    [class*="st-key-open_"] button:hover{
        background:#12A6BA !important;
        color:#FFFFFF !important;
        border-color:#12A6BA !important;
        transform:translateX(3px);
    }

    /* ---------- Top Header (plain — only Monthly Budget carries color) ---------- */
    .st-key-app_header{
        background:transparent;
        border:none;
        box-shadow:none;
        padding:6px 4px 2px 4px;
        margin-bottom:14px;
        min-height:0;
        overflow:visible;
    }
    .st-key-app_header [data-testid="stHorizontalBlock"]{ width:100%; align-items:center; }
    .st-key-app_header [data-testid="stMarkdownContainer"]{
        min-width:0; overflow:visible; white-space:normal;
    }
    .st-key-app_header .app-page-title{ font-size:24px; margin-bottom:1px; }
    .st-key-app_header .row-sub{ white-space:nowrap; font-size:14px; color:#64748B; }
    .st-key-profile_icon_btn button{
        border-radius:50% !important;
        width:46px !important; height:46px !important;
        padding:0 !important;
        min-height:0 !important;
        background:#EEF2F5 !important;
        color:#1E2A33 !important;
        border:1px solid #E2E8EC !important;
    }
    .st-key-profile_icon_btn button:hover{
        background:#12A6BA !important; color:#FFFFFF !important; border-color:#12A6BA !important;
    }

    /* ---------- Bottom Nav Bar ---------- */
    .st-key-bottom_nav{
        position:fixed;
        bottom:0;
        left:0;
        right:0;
        background:rgba(255,255,255,0.92);
        backdrop-filter:blur(10px);
        border-top:1px solid #E2E8F0;
        padding:8px 16px 12px 16px;
        z-index:999;
        box-shadow:0 -6px 20px -10px rgba(16,24,40,0.18);
    }
    .st-key-bottom_nav [data-testid="stHorizontalBlock"]{
        max-width:520px;
        margin:0 auto;
        width:100%;
        align-items:center;
        gap:0 !important;
    }
    .st-key-bottom_nav [data-testid="stColumn"]{
        display:flex;
        align-items:center;
        justify-content:center;
    }
    .bottom-nav-spacer{ height:82px; }
    .st-key-bottom_nav .stButton>button{
        background:transparent;
        border:none;
        color:#64748B;
        font-weight:600;
        box-shadow:none;
        padding:4px 10px;
    }
    .st-key-bottom_nav .stButton>button[kind="primary"]{
        background:transparent !important;
        color:#12A6BA !important;
    }
    .st-key-home_nav_container, .st-key-history_nav_container{
        display:flex;
        justify-content:center;
        align-items:center;
        width:100%;
    }

    /* ---------- Center FAB ("+" button) ---------- */
    .st-key-fab_container{
        display:flex;
        justify-content:center;
        align-items:center;
        width:100%;
    }
    .st-key-fab_container button{
        background:#22C55E !important;
        color:white !important;
        font-size:26px !important;
        font-weight:bold;
        width:58px;
        height:58px;
        border-radius:50% !important;
        padding:0 !important;
        line-height:1;
        margin-top:-34px;
        box-shadow:0 12px 24px -8px rgba(34,197,94,0.55);
        border:none !important;
    }
    .st-key-fab_container button:hover{
        background:#16A34A !important;
        transform:translateY(-2px);
    }

    /* ---------- Login Card ---------- */
    .st-key-login_card{
        max-width:420px;
        margin:0 auto;
        background:#FFFFFF;
        border-radius:22px;
        padding:22px 24px 6px 24px;
        border:1px solid #E9EEF1;
        box-shadow:0 20px 46px -22px rgba(15,27,42,0.24);
    }

    /* ---------- Month detail table (screenshot-style) ---------- */
    .st-key-month_table [data-testid="stHorizontalBlock"]{ align-items:center; }
    .tbl-cell{ font-size:15px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .tbl-amount{ font-size:15px; font-weight:700; text-align:right; }
    .tbl-total-label{ font-size:15px; font-weight:800; letter-spacing:0.03em; }
    .tbl-total-amount{ font-size:16px; font-weight:800; text-align:right; color:#12A6BA; }
    .tbl-divider{ border:none; border-top:2px dashed #D5DEE4; margin:6px 0; }
    .st-key-month_table .stButton>button{
        padding:4px !important;
        min-height:0 !important;
        border:none !important;
        background:transparent !important;
        color:#64748B !important;
        box-shadow:none !important;
    }
    .st-key-month_table .stButton>button:hover{ color:#12A6BA !important; }

    /* ===================================================================
       UNIFORM SIZING SYSTEM — taller, consistent controls & cards
       =================================================================== */

    /* Form controls: one consistent, comfortable height everywhere */
    .stTextInput input,
    .stNumberInput input,
    .stDateInput input{
        min-height:48px !important;
        font-size:15px !important;
        padding:10px 14px !important;
    }
    .stSelectbox div[data-baseweb="select"] > div{
        min-height:48px !important;
        font-size:15px !important;
    }
    .stDateInput div[data-baseweb="input"]{ min-height:48px !important; }
    /* Date field: make day/month/year fully visible, never clipped */
    .stDateInput div[data-baseweb="input"]{ width:100% !important; }
    .stDateInput input{ white-space:nowrap; text-overflow:clip; letter-spacing:0.3px; }
    .stNumberInput button{ height:48px !important; }
    .stTextInput label, .stNumberInput label, .stDateInput label,
    .stSelectbox label, .stRadio label, .stCheckbox label{
        font-weight:600 !important; color:#334155 !important; font-size:14px !important;
    }
    /* Cyan focus accent on inputs (matches reference accent) */
    .stTextInput input:focus,
    .stNumberInput input:focus,
    .stDateInput input:focus{
        border-color:#12A6BA !important;
        box-shadow:0 0 0 2px rgba(23,185,204,0.25) !important;
    }
    .stCheckbox [data-baseweb="checkbox"] [data-checked="true"]{ background-color:#12A6BA !important; }
    /* Tabs (login) + selection controls use the cyan accent, not the default red */
    [data-baseweb="tab-highlight"]{ background-color:#12A6BA !important; }
    button[data-baseweb="tab"][aria-selected="true"]{ color:#0E93A6 !important; }
    [data-baseweb="radio"] div[data-checked="true"]{ background-color:#12A6BA !important; border-color:#12A6BA !important; }
    [data-testid="stRadio"] [aria-checked="true"]{ border-color:#12A6BA !important; }

    /* Buttons: comfortable minimum height across all button types */
    .stButton>button,
    .stDownloadButton>button,
    .stFormSubmitButton>button{
        min-height:48px;
        padding:8px 18px;
    }

    /* Cards: comfortable padding + taller, vertically-centered rows */
    div[data-testid="stVerticalBlockBorderWrapper"]{ padding:10px 14px; }
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stHorizontalBlock"]{
        min-height:60px;
        align-items:center;
    }

    /* Center the app as a phone-inspired column; don't stretch edge-to-edge */
    .block-container,
    [data-testid="stMainBlockContainer"]{
        padding-top:1.1rem !important;
        padding-bottom:0.6rem !important;
        max-width:620px !important;
    }
    /* trim the default gap Streamlit puts between vertical elements */
    [data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"]{ gap:1.15rem; }

    /* ---------- Accordion (month) header + toggle ---------- */
    [class*="st-key-toggle_month_"] button{
        background:transparent !important;
        border:none !important;
        color:#334155 !important;
        box-shadow:none !important;
        font-size:20px !important;
        min-height:44px !important;
        padding:4px 8px !important;
    }
    [class*="st-key-toggle_month_"] button:hover{
        color:#12A6BA !important;
        background:#EAFBFD !important;
        border-radius:12px !important;
    }

    /* ---------- Admin / Profile: larger centered card ---------- */
    .st-key-profile_card{
        max-width:560px;
        margin:0 auto;
        background:#FFFFFF;
        border-radius:22px;
        padding:26px 30px;
        border:1px solid #E6EDEC;
        box-shadow:0 20px 46px -22px rgba(16,24,40,0.26);
    }
    .st-key-profile_card [data-testid="stElementContainer"]{ text-align:center; }
    .st-key-profile_card .app-page-title,
    .st-key-profile_card .app-section-title,
    .st-key-profile_card .row-sub{ text-align:center; }

    /* ---------- History: centered year selector card + summary ---------- */
    .st-key-year_card{
        max-width:300px;
        margin:0 auto 10px auto;
        background:#FFFFFF;
        border-radius:16px;
        padding:12px 18px 14px;
        border:1px solid #E6EDEC;
        box-shadow:0 10px 26px -18px rgba(16,24,40,0.18);
        text-align:center;
    }
    .year-card-label{
        font-size:13px; font-weight:700; color:#475569;
        text-transform:uppercase; letter-spacing:0.05em; margin-bottom:6px;
    }
    .summary-item{ text-align:center; }
    .summary-label{ font-size:12.5px; color:#475569; font-weight:600; }
    .summary-value{ font-size:20px; font-weight:800; color:#0F172A; margin-top:2px; white-space:nowrap; }

    /* ===================== RESPONSIVE ===================== */
    /* Desktop / tablet: centered phone-inspired column with side whitespace */
    @media (min-width:900px){
        .block-container, [data-testid="stMainBlockContainer"]{ max-width:620px !important; }
    }
    @media (min-width:641px) and (max-width:899px){
        .block-container, [data-testid="stMainBlockContainer"]{ max-width:560px !important; }
    }
    /* Phone: full-width column, comfortable touch spacing */
    @media (max-width:640px){
        .block-container, [data-testid="stMainBlockContainer"]{
            max-width:460px !important;
            padding-left:1rem !important; padding-right:1rem !important;
        }
        .st-key-app_header{ padding:6px 2px 2px 2px; }
        .app-page-title{ font-size:21px; }
        .st-key-app_header .app-page-title{ font-size:22px; }
        .app-section-title{ font-size:16px; }
        .stat-value{ font-size:29px; }
        .cat-name{ font-size:16px; }
        .chip{ width:44px; height:44px; font-size:21px; }
        .cell-value{ font-size:13px; }
        .tbl-cell, .tbl-amount{ font-size:13px; }
        .table-header{ font-size:11px; }
        .st-key-bottom_nav{ padding:6px 8px 10px 8px; }
        .st-key-bottom_nav [data-testid="stHorizontalBlock"]{ max-width:460px; }
        .st-key-profile_card{ padding:20px 16px; }
    }

    /* ---------- old left sidebar no longer used ---------- */
    section[data-testid="stSidebar"]{ display:none; }
    [data-testid="collapsedControl"]{ display:none; }

    /* ---------- Hide native Streamlit chrome ----------
       The default header/toolbar is a fixed, transparent overlay that sits
       on top of the page. With our small custom top padding it was
       overlapping and clipping the first line of content (the greeting).
       Hiding it removes the overlap entirely. */
    header[data-testid="stHeader"]{ display:none !important; }
    div[data-testid="stToolbar"]{ display:none !important; }
    #MainMenu{ visibility:hidden; }
    footer{ visibility:hidden; }

    </style>
    """
    st.markdown(base_css, unsafe_allow_html=True)

    # Cards are kept uniform white (clean, like the reference). Category and
    # month identity now come from the neutral chip + labels rather than
    # colored tints/stripes, so no per-block color CSS is generated.


apply_theme()


# ----------------------------------------------------------------------
# HELPERS — USERS / BUDGET
# ----------------------------------------------------------------------
def get_user(username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cursor.fetchone()
    release_connection(conn)
    return user


def get_or_create_google_user(google_user):
    """Ensure a local `users` row exists for this Google account, and
    return the `username` string the rest of the app should use.
    Using the Google email as the username means every existing
    function (get_expenses, add_expense, budgets, etc.) needs zero
    changes — they only ever cared about a username string."""
    email = google_user["email"].strip().lower()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username=%s", (email,))
    user = cursor.fetchone()
    if user is None:
        cursor.execute(
            "INSERT INTO users(username, email, google_id, name, picture) "
            "VALUES(%s, %s, %s, %s, %s)",
            (email, email, google_user.get("sub"),
             google_user.get("name"), google_user.get("picture")),
        )
    else:
        # Keep name/avatar fresh in case they change on Google's side.
        cursor.execute(
            "UPDATE users SET google_id=%s, name=%s, picture=%s WHERE username=%s",
            (google_user.get("sub"), google_user.get("name"),
             google_user.get("picture"), email),
        )
    conn.commit()
    release_connection(conn)
    return email


def get_user_budget(username):
    user = get_user(username)
    if user is None or user["budget"] is None:
        return 5000.0
    return float(user["budget"])


def set_user_budget(username, new_budget):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET budget=%s WHERE username=%s", (new_budget, username))
    conn.commit()
    release_connection(conn)


# ----------------------------------------------------------------------
# HELPERS — EXPENSES / HISTORY
# ----------------------------------------------------------------------
def get_expenses(year=None, month=None, category=None):

    conn = get_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM expenses WHERE username=%s"
    params = [st.session_state.username]

    if category:
        query += " AND category=%s"
        params.append(category)

    if year:
        query += " AND TO_CHAR(date::date, 'YYYY')=%s"
        params.append(str(year))

    if month:
        query += " AND TO_CHAR(date::date, 'MM')=%s"
        params.append(f"{month:02d}")

    query += " ORDER BY date DESC"

    cursor.execute(query, params)

    expenses = cursor.fetchall()

    release_connection(conn)

    return expenses


def total_of(history):
    return sum(h["paid_amount"] for h in history)


def get_month_history(year, month):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM history
        WHERE username=%s
        AND TO_CHAR(date::date, 'YYYY')=%s
        AND TO_CHAR(date::date, 'MM')=%s
        ORDER BY date DESC
    """,
    (
        st.session_state.username,
        str(year),
        f"{month:02d}"
    ))

    rows = cursor.fetchall()

    release_connection(conn)

    return rows


def month_total(year, month):

    history = get_month_history(year, month)

    return total_of(history)


def get_year_total(year=None):
    if year is None:
        year = date.today().year

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT SUM(paid_amount) AS total FROM history WHERE username=%s AND TO_CHAR(date::date, 'YYYY')=%s",
        (st.session_state.username, str(year))
    )

    row = cursor.fetchone()
    total = (row["total"] if row else None) or 0

    release_connection(conn)

    return total


def get_current_month_total():
    today_ = date.today()
    return month_total(today_.year, today_.month)


def get_category_month_total(category, year=None, month=None):
    today_ = date.today()
    year = year or today_.year
    month = month or today_.month

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT SUM(paid_amount) AS total FROM history
        WHERE username=%s
        AND category=%s
        AND TO_CHAR(date::date, 'YYYY')=%s
        AND TO_CHAR(date::date, 'MM')=%s
    """, (st.session_state.username, category, str(year), f"{month:02d}"))

    row = cursor.fetchone()
    total = (row["total"] if row else None) or 0

    release_connection(conn)

    return total


def get_all_category_month_totals(year=None, month=None):
    """Same result as calling get_category_month_total() once per category,
    but as a single grouped query — one connection/round-trip instead of
    one per category. Returns a dict: {category: total}."""
    today_ = date.today()
    year = year or today_.year
    month = month or today_.month

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT category, SUM(paid_amount) AS total FROM history
        WHERE username=%s
        AND TO_CHAR(date::date, 'YYYY')=%s
        AND TO_CHAR(date::date, 'MM')=%s
        GROUP BY category
    """, (st.session_state.username, str(year), f"{month:02d}"))

    totals = {row["category"]: row["total"] for row in cursor.fetchall()}

    release_connection(conn)

    return totals


def get_expense_count_this_month(expense_id):
    today_ = date.today()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*) AS total FROM history
        WHERE expense_id=%s
        AND username=%s
        AND TO_CHAR(date::date, 'YYYY')=%s
        AND TO_CHAR(date::date, 'MM')=%s
    """, (expense_id, st.session_state.username, str(today_.year), f"{today_.month:02d}"))

    row = cursor.fetchone()
    count = row["total"] if row else 0

    release_connection(conn)

    return count


def find_expense_in_category(category, desc):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM expenses WHERE username=%s AND category=%s AND LOWER(TRIM(\"desc\"))=%s",
        (st.session_state.username, category, desc.strip().lower())
    )

    row = cursor.fetchone()

    release_connection(conn)

    return row


def add_expense(category, desc, amount, exp_date, count=1):
    conn = get_connection()
    cursor = conn.cursor()

    expense_id = str(uuid.uuid4())

    cursor.execute("""
        INSERT INTO expenses
        VALUES(%s,%s,%s,%s,%s,%s,%s)
            """,(
            expense_id,
            category,
            desc,
            amount,
            exp_date.strftime("%Y-%m-%d"),
            count,
            st.session_state.username
))

    conn.commit()
    release_connection(conn)

    return expense_id


def add_history(expense_id, category, desc, paid_amount, purchase_date):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""INSERT INTO history VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                   (str(uuid.uuid4()),expense_id,category,desc,paid_amount,purchase_date.strftime("%Y-%m-%d"),st.session_state.username))

    conn.commit()
    release_connection(conn)


def delete_template_only(exp_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM expenses WHERE id=%s AND username=%s",
        (exp_id, st.session_state.username)
    )

    conn.commit()
    release_connection(conn)


def delete_template_with_history(exp_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM history WHERE expense_id=%s AND username=%s",
        (exp_id, st.session_state.username)
    )

    cursor.execute(
        "DELETE FROM expenses WHERE id=%s AND username=%s",
        (exp_id, st.session_state.username)
    )

    conn.commit()
    release_connection(conn)


def delete_history_transaction(history_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM history WHERE id=%s AND username=%s",
        (history_id, st.session_state.username)
    )

    conn.commit()
    release_connection(conn)


def edit_history_transaction(history_id, new_desc, new_category, new_amount, new_date):
    """
    Only ever touches the History record. The predefined expense
    template is never modified when editing a past transaction.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
            UPDATE history
            SET
                "desc"=%s,
                category=%s,
                paid_amount=%s,
                date=%s
            WHERE id=%s AND username=%s
        """,
        (
            new_desc,
            new_category,
            new_amount,
            new_date.strftime("%Y-%m-%d"),
            history_id,
            st.session_state.username
        ))

    conn.commit()
    release_connection(conn)


def all_years():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT DISTINCT TO_CHAR(date::date, 'YYYY') AS yr
    FROM expenses
    WHERE username=%s
    """, (st.session_state.username,))

    years = [int(r["yr"]) for r in cursor.fetchall()]

    release_connection(conn)

    current = date.today().year

    if current not in years:
        years.append(current)

    return sorted(years, reverse=True)


# ----------------------------------------------------------------------
# BUDGET BAR — simplified to 3 clear, labeled states
# ----------------------------------------------------------------------
def get_budget_status(spent, budget):
    """
    Pace-aware: compares % of budget spent against % of the month
    already elapsed. Collapsed to exactly 3 states (not a 5-shade
    gradient) so the meaning is unambiguous, with a plain-language
    label alongside the color.
    """
    today_ = date.today()
    days_in_month = calendar.monthrange(today_.year, today_.month)[1]
    pace = today_.day / days_in_month

    if budget <= 0:
        return "on_track", "#22C55E", "On track", 0.0

    ratio = spent / budget

    if ratio > 1.0:
        return "over", "#EF4444", "Over budget", ratio
    elif ratio > pace:
        return "cutting_close", "#F5B301", "Cutting it close", ratio
    else:
        return "on_track", "#22C55E", "On track", ratio


def render_budget_bar(spent, budget, on_dark=False):
    status, color, label, ratio = get_budget_status(spent, budget)
    width_pct = max(0.0, min(ratio, 1.0)) * 100

    today_ = date.today()
    days_in_month = calendar.monthrange(today_.year, today_.month)[1]
    days_left = days_in_month - today_.day

    track_bg = "rgba(255,255,255,0.35)" if on_dark else "#E2E8F0"
    fill_color = color
    sub_color = "rgba(255,255,255,0.92)" if on_dark else "#475569"

    st.markdown(f"""
    <div style='background:{track_bg};border-radius:10px;height:14px;overflow:hidden;margin-top:2px;'>
      <div style='width:{width_pct:.1f}%;background:{fill_color};height:100%;border-radius:10px;'></div>
    </div>
    """, unsafe_allow_html=True)

    over_text = ""
    if spent > budget:
        over_text = f" — over by ₹{spent - budget:,.2f}"

    st.markdown(
        f"<div class='budget-sub' style='color:{sub_color};'>₹{spent:,.0f} / ₹{budget:,.0f}{over_text} · "
        f"{days_left} day{'s' if days_left != 1 else ''} left in {today_.strftime('%B')}</div>",
        unsafe_allow_html=True
    )


def render_stat_card(label, value, subtitle=None, gradient=None, bordered=True):
    """Single reusable stat card. Pass `gradient` (a CSS gradient string)
    to render it as a colored hero card with white text. Pass
    bordered=False for a plain, card-less label+value block (matches the
    reference 'Current Balance' style used on the home page)."""
    if gradient:
        cls = "stat-card hero"
    elif bordered:
        cls = "stat-card"
    else:
        cls = "stat-plain"
    style = f"background:{gradient};" if gradient else ""
    sub_html = f"<div class='stat-sub'>{subtitle}</div>" if subtitle else ""
    st.markdown(
        f"<div class='{cls}' style='{style}'>"
        f"<div class='stat-label'>{label}</div>"
        f"<div class='stat-value'>{value}</div>"
        f"{sub_html}</div>",
        unsafe_allow_html=True
    )


# ----------------------------------------------------------------------
# LOGIN SCREEN
# ----------------------------------------------------------------------
def login_screen():
    st.write("")
    st.markdown(
        "<h1 style='text-align:center;'>💰 Finance Tracker</h1>"
        "<p style='text-align:center;opacity:0.8;'>    </p>",
        unsafe_allow_html=True
    )
    st.write("")

    with st.container(key="login_card"):
        st.write("")
        if st.button("Continue with Google", icon=":material/login:",
                     width="stretch", type="primary"):
            st.login("google")


# ----------------------------------------------------------------------
# TOP HEADER
# ----------------------------------------------------------------------
def render_header():
    with st.container(key="app_header"):
        col_text, col_btn = st.columns([8, 1.4], vertical_alignment="center")
        with col_text:
            display_name = st.user.get("name") or st.session_state.username.title()
            st.markdown(
                f"<div class='app-page-title'>{display_name}</div>"
                f"<div class='row-sub'>{date.today().strftime('%A, %d %B %Y')}</div>",
                unsafe_allow_html=True
            )
        with col_btn:
            if st.button("", icon=":material/person:", key="profile_icon_btn", help="Profile"):
                if st.session_state.page != "Profile":
                    st.session_state.previous_page = st.session_state.page
                st.session_state.page = "Profile"
                st.rerun()


# ----------------------------------------------------------------------
# BOTTOM NAV (Home | + | History) — native horizontal flex container,
# so the three items are genuinely evenly distributed instead of
# stretched equal-width columns fighting a fixed-size circle.
# ----------------------------------------------------------------------
def render_bottom_nav():
    st.markdown('<div class="bottom-nav-spacer"></div>', unsafe_allow_html=True)
    with st.container(key="bottom_nav"):
        col_home, col_fab, col_hist = st.columns(3, vertical_alignment="center")

        with col_home:
            with st.container(key="home_nav_container"):
                if st.button(
                    "Home", icon=":material/home:", key="nav_home",
                    type="primary" if st.session_state.page == "Home" else "secondary",
                ):
                    st.session_state.page = "Home"
                    st.session_state.selected_category = None
                    st.session_state.expanded_month = None
                    st.rerun()

        with col_fab:
            with st.container(key="fab_container"):
                if st.button("", icon=":material/add:", key="fab_add_expense"):
                    st.session_state.add_expense_category_lock = (
                        st.session_state.selected_category
                        if st.session_state.page == "Home"
                        else None
                    )
                    st.session_state.show_add_expense = True
                    st.rerun()

        with col_hist:
            with st.container(key="history_nav_container"):
                if st.button(
                    "History", icon=":material/history:", key="nav_history",
                    type="primary" if st.session_state.page == "History" else "secondary",
                ):
                    st.session_state.page = "History"
                    st.session_state.expanded_month = None
                    st.rerun()


# ----------------------------------------------------------------------
# ADD AN EXPENSE (global FAB sheet)
# ----------------------------------------------------------------------
def render_add_expense_view():
    with st.container(horizontal=True, vertical_alignment="center", gap="medium"):
        if st.button("", icon=":material/close:", key="close_add_expense"):
            st.session_state.show_add_expense = False
            st.rerun()
        st.markdown("<div class='app-page-title'>Add an Expense</div>", unsafe_allow_html=True)

    locked_category = st.session_state.add_expense_category_lock

    with st.form("add_expense_form", clear_on_submit=True):

        if locked_category:
            st.text_input("Category", value=locked_category, disabled=True, key="add_expense_category_locked")
            category = locked_category
        else:
            category = st.selectbox("Category", CATEGORIES, key="add_expense_category")

        desc = st.text_input("Expense Name", key="add_expense_desc")
        amount = st.number_input("Amount (₹)", min_value=0.0, step=10.0, key="add_expense_amount")
        exp_date = st.date_input("Date", value=date.today(), key="add_expense_date")

        submitted = st.form_submit_button("Save", width="stretch", type="primary")

        if submitted:
            if desc.strip() == "" or amount <= 0:
                st.error("Please enter a valid name and amount.")
            else:
                existing = find_expense_in_category(category, desc)

                if existing:
                    add_history(existing["id"], category, existing["desc"], amount, exp_date)
                    st.warning(f"'{existing['desc']}' already exists in {category} — this purchase was added to it.")
                else:
                    expense_id = add_expense(category, desc.strip(), amount, exp_date)
                    add_history(expense_id, category, desc.strip(), amount, exp_date)
                    st.success(f"'{desc.strip()}' added to {category}!")

    if st.button("Done", key="add_expense_done", width="stretch"):
        st.session_state.show_add_expense = False
        st.rerun()


# ----------------------------------------------------------------------
# HOME PAGE
# ----------------------------------------------------------------------
def home_page():
    if st.session_state.selected_category is not None:
        category_detail_view(st.session_state.selected_category)
        return

    render_stat_card(
        "Total Spent This Year",
        f"₹{get_year_total():,.2f}",
        bordered=False,
    )

    with st.container(border=True, key="budget_card"):
        st.markdown("<div class='app-section-title'>Monthly Budget:</div>", unsafe_allow_html=True)
        budget = get_user_budget(st.session_state.username)
        spent_this_month = get_current_month_total()
        render_budget_bar(spent_this_month, budget, on_dark=True)

    st.markdown("<div class='app-section-title'>~Categories~</div>", unsafe_allow_html=True)

    all_cat_totals = get_all_category_month_totals()

    for cat in CATEGORIES:
        icon = CATEGORY_ICONS.get(cat, "📦")
        c = CATEGORY_COLORS.get(cat, {"accent": DEFAULT_ACCENT})
        cat_total = all_cat_totals.get(cat, 0)

        with st.container(border=True, key=f"catcard_{cat}"):
            # [icon] Name / "This month"  ....  ₹amount (colored)  [>]
            lead, value, arrow = st.columns([5, 4.5, 1.2], vertical_alignment="center")
            lead.markdown(
                f"<div class='cell-lead'>"
                f"<span class='chip' style='background:{c['accent']}33;'>{icon}</span>"
                f"<div><div class='cat-name'>{cat}</div>"
                f"<div class='cat-sub'>This month</div></div></div>",
                unsafe_allow_html=True
            )
            value.markdown(
                f"<div class='cell-value' style='color:{c['accent']};font-weight:800;font-size:16px;'>"
                f"₹{cat_total:,.2f}</div>",
                unsafe_allow_html=True
            )
            with arrow:
                if st.button("", icon=":material/chevron_right:", key=f"open_{cat}"):
                    st.session_state.selected_category = cat
                    st.rerun()


@st.dialog("Delete expense?")
def _confirm_delete_template(exp_id, desc):
    st.markdown(
        f"<div style='font-size:15px;margin-bottom:6px;'>Delete <b>{desc}</b>?</div>",
        unsafe_allow_html=True
    )
    delete_option = st.radio(
        "What should be removed?",
        ("Delete Template Only", "Delete Template + Entire History"),
        key="tpl_delete_opt",
    )
    col_del, col_cancel = st.columns(2)
    with col_del:
        with st.container(key="danger_btn"):
            if st.button("Delete", key="tpl_delete_yes", width="stretch"):
                if delete_option == "Delete Template Only":
                    delete_template_only(exp_id)
                else:
                    delete_template_with_history(exp_id)
                st.session_state.category_action = {"type": None, "id": None}
                queue_toast("Deleted.", "🗑️")
                st.rerun()
    with col_cancel:
        if st.button("Cancel", key="tpl_delete_no", width="stretch"):
            st.session_state.category_action = {"type": None, "id": None}
            st.rerun()


def category_detail_view(category):
    if st.button("Back to Categories", icon=":material/arrow_back:", key="back_to_categories"):
        st.session_state.selected_category = None
        st.session_state.category_action = {"type": None, "id": None}
        st.rerun()

    st.markdown(f"<div class='app-page-title'>📂 {category}</div>", unsafe_allow_html=True)

    expenses = get_expenses(category=category)
    category_total = get_category_month_total(category)

    c = CATEGORY_COLORS.get(category, {"grad": (DEFAULT_ACCENT, DEFAULT_ACCENT)})
    g0, g1 = c["grad"]
    render_stat_card(
        f"{category} — This Month",
        f"₹{category_total:,.2f}",
        gradient=f"linear-gradient(135deg,{g0} 0%,{g1} 100%)",
    )

    st.markdown("<div class='app-section-title'>Expenses</div>", unsafe_allow_html=True)

    if not expenses:
        st.info("No expenses recorded yet in this category. Tap the ➕ below to add one.")
        return

    action = st.session_state.category_action

    for e in expenses:
        with st.container(border=True):
            tx_count = get_expense_count_this_month(e["id"])

            # Single horizontal line: Name | Default: ₹X | Count: N | [+][Custom][🗑]
            name_c, default_c, count_c, actions_c = st.columns(
                [3.2, 2.4, 1.8, 3.0], vertical_alignment="center"
            )
            name_c.markdown(
                f"<div class='row-title' style='white-space:nowrap;overflow:hidden;"
                f"text-overflow:ellipsis;'>{e['desc']}</div>",
                unsafe_allow_html=True
            )
            default_c.markdown(
                f"<div class='cell-value' style='text-align:left;'>Default: ₹{e['amount']:,.0f}</div>",
                unsafe_allow_html=True
            )
            count_c.markdown(
                f"<div class='cell-value' style='text-align:left;'>Count: {tx_count}</div>",
                unsafe_allow_html=True
            )
            with actions_c:
                b1, b2, b3 = st.columns(3, gap="small", vertical_alignment="center")
                with b1:
                    if st.button("", icon=":material/add:", key=f"plus_{e['id']}", help="Add at default amount"):
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE expenses SET date=%s WHERE id=%s AND username=%s",
                            (date.today().strftime("%Y-%m-%d"), e["id"], st.session_state.username)
                        )
                        conn.commit()
                        release_connection(conn)
                        add_history(e["id"], e["category"], e["desc"], e["amount"], date.today())
                        queue_toast("Added at default amount.")
                        st.rerun()
                with b2:
                    if st.button("", icon=":material/shopping_cart:", key=f"custom_{e['id']}", help="Custom amount"):
                        st.session_state.category_action = {"type": "edit", "id": e["id"]}
                        st.rerun()
                with b3:
                    if st.button("", icon=":material/delete:", key=f"delete_{e['id']}", help="Delete"):
                        st.session_state.category_action = {"type": "delete", "id": e["id"]}
                        st.rerun()

            # Inline "custom purchase" form directly beneath THIS expense
            if action["type"] == "edit" and action["id"] == e["id"]:
                with st.form(f"edit_form_{e['id']}"):
                    st.markdown("**🛒 Record Custom Purchase**")
                    st.text_input("Description", value=e["desc"], disabled=True)
                    new_amount = st.number_input("Purchase Amount (₹)", min_value=0.0, value=float(e["amount"]), step=10.0)
                    new_date = st.date_input("Purchase Date", value=date.today())
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.form_submit_button("Save", width="stretch", type="primary"):
                            conn = get_connection()
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE expenses SET date=%s WHERE id=%s AND username=%s",
                                (new_date.strftime("%Y-%m-%d"), e["id"], st.session_state.username)
                            )
                            conn.commit()
                            release_connection(conn)
                            add_history(e["id"], e["category"], e["desc"], new_amount, new_date)
                            st.session_state.category_action = {"type": None, "id": None}
                            queue_toast("Custom purchase recorded.")
                            st.rerun()
                    with c2:
                        if st.form_submit_button("Cancel", width="stretch"):
                            st.session_state.category_action = {"type": None, "id": None}
                            st.rerun()

    # Delete confirmation modal (opens when a trash icon was clicked)
    if action["type"] == "delete":
        target = next((e for e in expenses if e["id"] == action["id"]), None)
        if target:
            _confirm_delete_template(target["id"], target["desc"])


# ----------------------------------------------------------------------
# HISTORY PAGE  (year summary + inline accordion months)
# ----------------------------------------------------------------------
_TXN_COLS = [2.0, 3.0, 2.4, 1.8, 0.8, 0.8]


@st.dialog("Delete transaction?")
def _confirm_delete_history(history_id, desc):
    st.markdown(
        f"<div style='font-size:15px;'>Delete <b>{desc}</b>?</div>",
        unsafe_allow_html=True
    )
    st.write("")
    col_del, col_cancel = st.columns(2)
    with col_del:
        with st.container(key="danger_btn"):
            if st.button("Delete", key="hist_delete_yes", width="stretch"):
                delete_history_transaction(history_id)
                st.session_state.history_delete_confirm_id = None
                queue_toast("Transaction deleted.", "🗑️")
                st.rerun()
    with col_cancel:
        if st.button("Cancel", key="hist_delete_no", width="stretch"):
            st.session_state.history_delete_confirm_id = None
            st.rerun()


def _find_history(year, history_id):
    for m in range(1, 13):
        for r in get_month_history(year, m):
            if r["id"] == history_id:
                return r
    return None


def history_page():
    st.markdown("<div class='app-page-title'> History</div>", unsafe_allow_html=True)

    years = all_years()
    if st.session_state.selected_year not in years:
        st.session_state.selected_year = years[0]

    # Year selector — centered inside its own comfortable card
    with st.container(key="year_card"):
        st.markdown("<div class='year-card-label'>Viewing year</div>", unsafe_allow_html=True)
        selected_year = st.selectbox(
            "Year", years,
            index=years.index(st.session_state.selected_year),
            label_visibility="collapsed",
        )
    st.session_state.selected_year = selected_year

    _history_fragment(selected_year)

    # Delete confirmation modal (opened from inside the accordion)
    if st.session_state.history_delete_confirm_id:
        target = _find_history(selected_year, st.session_state.history_delete_confirm_id)
        if target:
            _confirm_delete_history(target["id"], target["desc"])
        else:
            st.session_state.history_delete_confirm_id = None


@st.fragment
def _history_fragment(year):
    flush_toast()

    active_months = [m for m in range(1, 13) if get_month_history(year, m)]
    if not active_months:
        st.info("No transactions recorded yet this year.")
        return

    _render_year_summary(year, active_months)

    st.markdown("<div class='app-section-title'>Months</div>", unsafe_allow_html=True)
    for m in sorted(active_months, reverse=True):
        _render_month_accordion(year, m)


def _render_year_summary(year, active_months):
    year_total = get_year_total(year)
    avg = year_total / len(active_months) if active_months else 0
    busiest = max(active_months, key=lambda mm: month_total(year, mm))

    cat_totals = {c: 0.0 for c in CATEGORIES}
    for m in active_months:
        for r in get_month_history(year, m):
            cat_totals[r["category"]] = cat_totals.get(r["category"], 0.0) + r["paid_amount"]
    top_cat = max(cat_totals, key=cat_totals.get) if any(cat_totals.values()) else "—"
    top_color = "#12A6BA"

    with st.container(border=True, key="year_summary"):
        a, b, c = st.columns(3, vertical_alignment="center")
        a.markdown(
            f"<div class='summary-item'><div class='summary-label'>This year</div>"
            f"<div class='summary-value'>₹{year_total:,.0f}</div></div>",
            unsafe_allow_html=True)
        b.markdown(
            f"<div class='summary-item'><div class='summary-label'>Avg / active month</div>"
            f"<div class='summary-value'>₹{avg:,.0f}</div></div>",
            unsafe_allow_html=True)
        c.markdown(
            f"<div class='summary-item'><div class='summary-label'>Busiest month</div>"
            f"<div class='summary-value'>{MONTH_NAMES[busiest - 1][:3]}</div></div>",
            unsafe_allow_html=True)

    st.markdown(
        f"<div class='row-sub' style='margin:2px 2px 6px;'>Most spent category this year: "
        f"<b style='color:{top_color};'>{top_cat}</b> · ₹{cat_totals.get(top_cat, 0):,.0f}</div>",
        unsafe_allow_html=True)


def _render_month_accordion(year, month):
    color = MONTH_PALETTE[(month - 1) % len(MONTH_PALETTE)]
    m_total = month_total(year, month)
    is_open = st.session_state.expanded_month == month

    with st.container(border=True, key=f"monthcard_{month}"):
        lead, total_c, arrow_c = st.columns([5, 4, 1.2], vertical_alignment="center")
        lead.markdown(
            f"<div class='cell-lead'>"
            f"<span class='chip' style='background:{color}33;color:{color};'>🗓️</span>"
            f"<span class='cat-name'>{MONTH_NAMES[month - 1]} {year}</span></div>",
            unsafe_allow_html=True)
        total_c.markdown(
            f"<div class='cell-value'>Total:  ₹{m_total:,.0f}</div>",
            unsafe_allow_html=True)
        with arrow_c:
            arrow = "▲" if is_open else "▼"
            if st.button(arrow, key=f"toggle_month_{month}"):
                st.session_state.expanded_month = None if is_open else month
                st.session_state.edit_history = None
                _frag_rerun()

        # Inline expand/collapse — the list appears beneath this header,
        # inside the same card. No navigation, no separate page.
        if is_open:
            _render_month_transactions(year, month)


def _render_month_transactions(year, month):
    history = get_month_history(year, month)
    if not history:
        st.info("No transactions in this month.")
        return

    st.markdown("<hr class='tbl-divider'>", unsafe_allow_html=True)
    with st.container(key="month_table"):
        h = st.columns(_TXN_COLS, vertical_alignment="center")
        h[0].markdown("<div class='table-header'>Date</div>", unsafe_allow_html=True)
        h[1].markdown("<div class='table-header'>Description</div>", unsafe_allow_html=True)
        h[2].markdown("<div class='table-header'>Category</div>", unsafe_allow_html=True)
        h[3].markdown("<div class='table-header' style='text-align:right;'>Amount</div>", unsafe_allow_html=True)

        total_amount = 0.0
        for h_row in history:
            total_amount += h_row["paid_amount"]
            date_str = datetime.strptime(h_row["date"], "%Y-%m-%d").strftime("%d %b %Y")

            c = st.columns(_TXN_COLS, vertical_alignment="center")
            c[0].markdown(f"<div class='tbl-cell'>{date_str}</div>", unsafe_allow_html=True)
            c[1].markdown(f"<div class='tbl-cell' title='{h_row['desc']}'>{h_row['desc']}</div>", unsafe_allow_html=True)
            cat_color = CATEGORY_COLORS.get(h_row["category"], {"accent": DEFAULT_ACCENT})["accent"]
            c[2].markdown(f"<div class='tbl-cell' style='color:{cat_color};font-weight:700;'>{h_row['category']}</div>", unsafe_allow_html=True)
            c[3].markdown(f"<div class='tbl-amount'>₹{h_row['paid_amount']:,.0f}</div>", unsafe_allow_html=True)
            if c[4].button("", icon=":material/edit:", key=f"edit_hist_{h_row['id']}", help="Edit"):
                st.session_state.edit_history = h_row["id"]
                st.session_state.history_delete_confirm_id = None
                _frag_rerun()
            if c[5].button("", icon=":material/delete:", key=f"delete_hist_{h_row['id']}", help="Delete"):
                st.session_state.history_delete_confirm_id = h_row["id"]
                st.session_state.edit_history = None
                st.rerun()  # full rerun so the confirmation modal opens

            # Inline edit form immediately beneath THIS transaction
            if st.session_state.edit_history == h_row["id"]:
                _render_inline_edit(h_row)

        st.markdown("<hr class='tbl-divider'>", unsafe_allow_html=True)
        t = st.columns(_TXN_COLS, vertical_alignment="center")
        t[0].markdown("<div class='tbl-total-label'>TOTAL</div>", unsafe_allow_html=True)
        t[3].markdown(f"<div class='tbl-total-amount'>₹{total_amount:,.0f}</div>", unsafe_allow_html=True)


def _render_inline_edit(row):
    with st.form(f"edit_history_form_{row['id']}"):
        st.markdown("**✏️ Edit Transaction**")
        new_desc = st.text_input("Description", value=row["desc"])
        new_category = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index(row["category"]))
        new_amount = st.number_input("Amount", value=float(row["paid_amount"]), min_value=0.0, step=10.0)
        new_date = st.date_input("Date", value=datetime.strptime(row["date"], "%Y-%m-%d").date())
        c1, c2 = st.columns(2)
        with c1:
            if st.form_submit_button("Save", width="stretch", type="primary"):
                edit_history_transaction(row["id"], new_desc, new_category, new_amount, new_date)
                st.session_state.edit_history = None
                queue_toast("Transaction updated.")
                _frag_rerun()
        with c2:
            if st.form_submit_button("Cancel", width="stretch"):
                st.session_state.edit_history = None
                _frag_rerun()

# ----------------------------------------------------------------------
# PROFILE PAGE
# ----------------------------------------------------------------------
def profile_page():
    if st.button("Back", icon=":material/arrow_back:", key="profile_back"):
        st.session_state.page = st.session_state.previous_page
        st.rerun()

    with st.container(key="profile_card"):
        st.markdown("<div class='app-page-title'>👤 Profile</div>", unsafe_allow_html=True)
        display_name = st.user.get("name") or st.session_state.username.title()
        st.markdown(f"<div class='row-sub'>{display_name} · {st.session_state.username}</div>", unsafe_allow_html=True)
        st.divider()

        st.markdown("<div class='app-section-title'>Monthly Budget</div>", unsafe_allow_html=True)
        current_budget = get_user_budget(st.session_state.username)
        new_budget = st.number_input(
            "Set your monthly budget (₹)",
            min_value=0.0, step=100.0, value=current_budget
        )
        if st.button("Save Budget", width="stretch", type="primary"):
            set_user_budget(st.session_state.username, new_budget)
            queue_toast("Budget updated.")
            st.rerun()

        st.divider()

        st.markdown("<div class='app-section-title'>Download Data</div>", unsafe_allow_html=True)
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM expenses WHERE username=%s", (st.session_state.username,))
        expenses = [dict(row) for row in cursor.fetchall()]
        release_connection(conn)

        json_str = json.dumps(expenses, indent=2)
        st.download_button(
            "Download as JSON", icon=":material/download:",
            data=json_str,
            file_name="finance_data.json",
            mime="application/json",
            width="stretch",
        )

        if expenses:
            import csv
            import io
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=["id", "category", "desc", "amount", "count", "date", "username"])
            writer.writeheader()
            writer.writerows(expenses)
            st.download_button(
                "Download as CSV", icon=":material/download:",
                data=buf.getvalue(),
                file_name="finance_data.csv",
                mime="text/csv",
                width="stretch",
            )

        st.divider()
        st.markdown("<div class='app-section-title'>Reset Data</div>", unsafe_allow_html=True)
        confirm = st.checkbox("I understand this will permanently delete all my expense data.")
        with st.container(key="danger_btn"):
            if st.button("Reset All Data", icon=":material/delete_forever:", disabled=not confirm, width="stretch"):
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM expenses WHERE username=%s", (st.session_state.username,))
                cursor.execute("DELETE FROM history WHERE username=%s", (st.session_state.username,))
                conn.commit()
                release_connection(conn)
                queue_toast("All data reset.", "🗑️")
                st.rerun()

        st.divider()
        if st.button("Log Out", icon=":material/logout:", width="stretch"):
            st.session_state.page = "Home"
            st.session_state.selected_category = None
            st.session_state.selected_year = None
            st.session_state.expanded_month = None
            st.session_state.logged_in = False
            st.session_state.username = None
            st.logout()


# ----------------------------------------------------------------------
# MAIN APP FLOW
# ----------------------------------------------------------------------
def main():
    if not st.user.is_logged_in:
        login_screen()
        return

    if not st.session_state.logged_in:
        st.session_state.username = get_or_create_google_user(st.user)
        st.session_state.logged_in = True

    if st.session_state.show_add_expense:
        flush_toast()
        render_add_expense_view()
        return

    render_header()
    flush_toast()

    if st.session_state.page == "Home":
        home_page()
    elif st.session_state.page == "History":
        history_page()
    elif st.session_state.page == "Profile":
        profile_page()

    render_bottom_nav()


if __name__ == "__main__":
    main()