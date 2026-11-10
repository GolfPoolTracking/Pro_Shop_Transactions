import html

import streamlit as st
import pandas as pd
from supabase import create_client, Client
from datetime import date, datetime

# --- Page Config ---
st.set_page_config(page_title="Pro Shop Balance", page_icon="⛳", layout="centered")

PAGE_SIZE = 25

# --- Database Connection ---
@st.cache_resource
def init_connection() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_connection()

# --- Keep-Alive Ping Handler (/?view=ping) ---
if st.query_params.get("view") == "ping":
    try:
        # Lightweight query to keep both Streamlit and Supabase active
        supabase.table("proshop_transactions").select("id").limit(1).execute()
        st.write(f"🟢 PONG - Pro Shop App & Supabase Awake ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    except Exception as e:
        st.write(f"🟡 PONG - App Awake, DB Ping Error: {e}")
    st.stop()

# --- Database Functions ---
@st.cache_data(ttl=60)
def fetch_transactions() -> pd.DataFrame:
    response = (
        supabase.table("proshop_transactions")
        .select("*")
        .order("transaction_date", desc=True)
        .order("created_at", desc=True)
        .execute()
    )
    if response.data:
        df = pd.DataFrame(response.data)
        df["transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.date
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
        return df
    return pd.DataFrame(columns=["id", "transaction_date", "description", "amount", "created_at"])

@st.cache_data(ttl=60)
def fetch_shop_record(fallback: float) -> float:
    try:
        res = supabase.table("proshop_settings").select("value").eq("key", "shop_record").execute()
        if res.data:
            return float(res.data[0]["value"])
    except Exception:
        pass
    return fallback

def update_shop_record(new_val: float):
    supabase.table("proshop_settings").upsert({
        "key": "shop_record",
        "value": float(new_val)
    }).execute()
    fetch_shop_record.clear()

def add_transaction(t_date: date, desc: str, amt: float):
    supabase.table("proshop_transactions").insert({
        "transaction_date": t_date.isoformat(),
        "description": desc.strip(),
        "amount": float(amt)
    }).execute()
    fetch_transactions.clear()

def bulk_insert_transactions(records: list):
    supabase.table("proshop_transactions").insert(records).execute()
    fetch_transactions.clear()

def _plain(value):
    """Convert numpy scalars to plain Python values for the Supabase client."""
    return value.item() if hasattr(value, "item") else value

def apply_editor_changes(original_df: pd.DataFrame, editor_state: dict):
    """original_df must be exactly the frame that was shown in the editor."""
    has_changes = False

    # 1. Deletions (single call)
    deleted_ids = [_plain(original_df.iloc[i]["id"]) for i in editor_state.get("deleted_rows", [])]
    if deleted_ids:
        supabase.table("proshop_transactions").delete().in_("id", deleted_ids).execute()
        has_changes = True

    # 2. Edits (differ per row, so one call each)
    for row_idx, edits in editor_state.get("edited_rows", {}).items():
        row_id = _plain(original_df.iloc[int(row_idx)]["id"])
        if row_id in deleted_ids:
            continue
        payload = {}
        if "Date" in edits:
            payload["transaction_date"] = str(edits["Date"])
        if "Description" in edits:
            payload["description"] = str(edits["Description"]).strip()
        if "Amount (€)" in edits:
            payload["amount"] = float(edits["Amount (€)"])
        if payload:
            supabase.table("proshop_transactions").update(payload).eq("id", row_id).execute()
            has_changes = True

    if has_changes:
        fetch_transactions.clear()
        st.toast("Changes saved to Supabase!", icon="✅")
        st.rerun()

# --- Main App UI ---
st.title("⛳ Pro Shop Account Tracker")

df_transactions = fetch_transactions()

# --- Calculations ---
if not df_transactions.empty:
    calculated_balance = float(df_transactions["amount"].sum())
    total_winnings = float(df_transactions.loc[df_transactions["amount"] > 0, "amount"].sum())
    total_spent = float(df_transactions.loc[df_transactions["amount"] < 0, "amount"].abs().sum())
else:
    calculated_balance, total_winnings, total_spent = 0.0, 0.0, 0.0

saved_shop_record = fetch_shop_record(fallback=calculated_balance)
difference = saved_shop_record - calculated_balance

# --- Account Status & Reconciliation ---
st.subheader("Account Reconciliation")

m1, m2, m3 = st.columns(3)
m1.metric("My Calculated Balance", f"€{calculated_balance:,.2f}")
m2.metric("Shop System Record", f"€{saved_shop_record:,.2f}")
m3.metric(
    "Difference (Shop vs Mine)",
    f"€{difference:,.2f}",
    delta=f"{difference:+.2f} €",
    delta_color="normal" if difference == 0 else "inverse"
)

s1, s2 = st.columns(2)
s1.caption(f"🟢 **Total Winnings / Credits:** €{total_winnings:,.2f}")
s2.caption(f"🔴 **Total Spend / Entries:** €{total_spent:,.2f}")

with st.expander("Update Shop System Record"):
    u_col1, u_col2 = st.columns([3, 1])
    with u_col1:
        new_shop_val = st.number_input(
            "Latest balance reported by Pro Shop (€)",
            value=float(saved_shop_record),
            step=1.0,
            format="%.2f",
            label_visibility="collapsed"
        )
    with u_col2:
        if st.button("Save Balance", use_container_width=True):
            update_shop_record(new_shop_val)
            st.toast("Shop record updated!", icon="⛳")
            st.rerun()

st.divider()

# --- New Transaction Entry ---
st.subheader("Log Transaction")
with st.form("transaction_form", clear_on_submit=True):
    f_col1, f_col2 = st.columns([1, 1])
    with f_col1:
        entry_date = st.date_input("Date", date.today())
    with f_col2:
        tx_type = st.radio(
            "Type",
            ["Credit / Winnings (+)", "Spend / Entry (-)"],
            horizontal=True
        )

    f_col3, f_col4 = st.columns([2, 1])
    with f_col3:
        entry_desc = st.text_input("Description", placeholder="e.g., 2's Saturday, SM11 Wedge, 2's Entry")
    with f_col4:
        raw_amount = st.number_input("Amount (€)", min_value=0.00, value=2.00, step=1.00, format="%.2f")

    submitted = st.form_submit_button("Add Transaction", type="primary", use_container_width=True)
    if submitted:
        if not entry_desc.strip():
            st.error("Please enter a description.")
        elif raw_amount == 0:
            st.error("Amount must be greater than €0.00.")
        else:
            final_amount = raw_amount if "Credit" in tx_type else -abs(raw_amount)
            add_transaction(entry_date, entry_desc, final_amount)
            st.toast("Transaction logged!", icon="✅")
            st.rerun()

st.divider()

# --- Transaction History ---
st.subheader("Transaction History")


def render_list(df: pd.DataFrame):
    """Lightweight HTML list: scrolls with the page, grouped by date."""
    rows_html, last_date = [], None
    for r in df.itertuples():
        if r.transaction_date != last_date:
            last_date = r.transaction_date
            rows_html.append(
                f"<div style='margin:14px 0 4px;font-size:0.8rem;opacity:.6;"
                f"font-weight:600'>{last_date:%a %d %b %Y}</div>"
            )
        colour = "#16a34a" if r.amount > 0 else "#dc2626"
        rows_html.append(
            "<div style='display:flex;justify-content:space-between;align-items:center;"
            "padding:8px 0;border-bottom:1px solid rgba(128,128,128,.2)'>"
            f"<div><div>{html.escape(str(r.description))}</div>"
            f"<div style='font-size:.75rem;opacity:.55'>Balance €{r.running_balance:,.2f}</div></div>"
            f"<div style='color:{colour};font-weight:600;white-space:nowrap'>"
            f"{r.amount:+,.2f} €</div></div>"
        )
    st.markdown("".join(rows_html), unsafe_allow_html=True)


if not df_transactions.empty:
    f_search, f_filter = st.columns([2, 1])
    with f_search:
        search_term = st.text_input("Search", placeholder="Filter by 2's, Wedge, Draw...",
                                    label_visibility="collapsed")
    with f_filter:
        filter_type = st.selectbox("Filter", ["All Transactions", "Credits (+)", "Spends (-)"],
                                   label_visibility="collapsed")

    # Running balance computed chronologically *before* filtering
    df_chrono = df_transactions.sort_values(["transaction_date", "created_at"]).copy()
    df_chrono["running_balance"] = df_chrono["amount"].cumsum()
    df_view = df_chrono.sort_values(["transaction_date", "created_at"], ascending=False)

    if search_term:
        df_view = df_view[df_view["description"].str.contains(search_term, case=False, na=False, regex=False)]
    if filter_type == "Credits (+)":
        df_view = df_view[df_view["amount"] > 0]
    elif filter_type == "Spends (-)":
        df_view = df_view[df_view["amount"] < 0]
    df_view = df_view.reset_index(drop=True)

    # Reset paging whenever the filter changes
    sig = (search_term, filter_type)
    if st.session_state.get("_filter_sig") != sig:
        st.session_state["_filter_sig"] = sig
        st.session_state["visible_rows"] = PAGE_SIZE

    visible = st.session_state.get("visible_rows", PAGE_SIZE)
    df_page = df_view.head(visible).reset_index(drop=True)

    edit_mode = st.toggle("✏️ Edit mode")

    if df_view.empty:
        st.info("No transactions match your filter.")
    elif not edit_mode:
        render_list(df_page)
    else:
        st.caption("Double-click a cell to edit, or select a row and press `Delete`, then save.")
        display_df = pd.DataFrame({
            "Date": df_page["transaction_date"],
            "Description": df_page["description"],
            "Amount (€)": df_page["amount"],
        })
        # Key includes filter + page size so stale edits can never map to the wrong rows
        editor_key = f"tx_editor_{hash(sig)}_{visible}"
        st.data_editor(
            display_df,
            key=editor_key,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",      # no phantom "add row"; use the form above
            height=450,
            column_config={
                "Date": st.column_config.DateColumn("Date", required=True),
                "Description": st.column_config.TextColumn("Description", required=True),
                "Amount (€)": st.column_config.NumberColumn("Amount (€)", format="€%.2f", required=True),
            },
        )
        editor_state = st.session_state.get(editor_key, {})
        if editor_state.get("edited_rows") or editor_state.get("deleted_rows"):
            if st.button("💾 Save Table Edits", type="primary", use_container_width=True):
                apply_editor_changes(df_page, editor_state)

    # Paging
    remaining = len(df_view) - visible
    st.caption(f"Showing {min(visible, len(df_view))} of {len(df_view)}")
    if remaining > 0:
        if st.button(f"Show {min(PAGE_SIZE, remaining)} more", use_container_width=True):
            st.session_state["visible_rows"] = visible + PAGE_SIZE
            st.rerun()
else:
    st.info("No transactions in the database yet. Log one above or import your Excel file in the sidebar.")

# --- Sidebar: One-Click Excel Importer & Export ---
with st.sidebar:
    st.header("⚙️ Data Management")
    st.subheader("Import ProShop Balance.xlsx")
    uploaded_file = st.file_uploader("Upload Excel sheet", type=["xlsx"])

    if uploaded_file is not None:
        raw_excel = pd.read_excel(uploaded_file, skiprows=3, names=["Amount", "Date", "Description"])
        raw_excel = raw_excel.dropna(subset=["Amount", "Date", "Description"])
        raw_excel["Date"] = pd.to_datetime(raw_excel["Date"]).dt.date

        # Duplicate guard: skip rows already in the database (same date, description, amount)
        existing_keys = set()
        if not df_transactions.empty:
            existing_keys = {
                (d, str(desc).strip(), round(float(a), 2))
                for d, desc, a in zip(
                    df_transactions["transaction_date"],
                    df_transactions["description"],
                    df_transactions["amount"],
                )
            }

        records_to_insert, skipped = [], 0
        for _, row in raw_excel.iterrows():
            desc = str(row["Description"]).strip()
            amt = round(float(row["Amount"]), 2)
            if (row["Date"], desc, amt) in existing_keys:
                skipped += 1
                continue
            records_to_insert.append({
                "transaction_date": row["Date"].isoformat(),
                "description": desc,
                "amount": amt,
            })

        st.write(f"Ready to import **{len(records_to_insert)}** new transactions.")
        if skipped:
            st.caption(f"Skipping {skipped} that already exist.")

        if records_to_insert and st.button("🚀 Bulk Import to Supabase", type="primary", use_container_width=True):
            bulk_insert_transactions(records_to_insert)
            st.success(f"Imported {len(records_to_insert)} rows!")
            st.rerun()

    if not df_transactions.empty:
        st.divider()
        csv_data = df_transactions[["transaction_date", "description", "amount"]].to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Export Backup CSV",
            data=csv_data,
            file_name=f"proshop_balance_{date.today().isoformat()}.csv",
            mime="text/csv",
            use_container_width=True
        )