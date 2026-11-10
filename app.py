import streamlit as st
import pandas as pd
from supabase import create_client, Client
from datetime import date

# --- Page Config ---
st.set_page_config(page_title="Pro Shop Balance", page_icon="⛳", layout="centered")

# --- Database Connection ---
@st.cache_resource
def init_connection() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_connection()

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

def apply_editor_changes(original_df: pd.DataFrame, editor_state: dict):
    has_changes = False

    # 1. Handle Deletions
    for row_idx in editor_state.get("deleted_rows", []):
        row_id = original_df.iloc[row_idx]["id"]
        supabase.table("proshop_transactions").delete().eq("id", row_id).execute()
        has_changes = True

    # 2. Handle Edits
    for row_idx, edits in editor_state.get("edited_rows", {}).items():
        row_id = original_df.iloc[int(row_idx)]["id"]
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
difference = shop_record_diff = saved_shop_record - calculated_balance

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

# --- Transaction History & Inline Editor ---
st.subheader("Transaction History")

if not df_transactions.empty:
    f_search, f_filter = st.columns([2, 1])
    with f_search:
        search_term = st.text_input("Search descriptions...", placeholder="Filter by 2's, Wedge, Draw...", label_visibility="collapsed")
    with f_filter:
        filter_type = st.selectbox("Filter", ["All Transactions", "Credits (+)", "Spends (-)"], label_visibility="collapsed")

    # Compute chronological running balance before filtering
    df_chrono = df_transactions.sort_values(["transaction_date", "created_at"], ascending=[True, True]).copy()
    df_chrono["running_balance"] = df_chrono["amount"].cumsum()
    df_view = df_chrono.sort_values(["transaction_date", "created_at"], ascending=[False, False]).copy()

    if search_term:
        df_view = df_view[df_view["description"].str.contains(search_term, case=False, na=False)]
    if filter_type == "Credits (+)":
        df_view = df_view[df_view["amount"] > 0]
    elif filter_type == "Spends (-)":
        df_view = df_view[df_view["amount"] < 0]

    df_view = df_view.reset_index(drop=True)

    display_df = pd.DataFrame({
        "Date": df_view["transaction_date"],
        "Description": df_view["description"],
        "Amount (€)": df_view["amount"],
        "Running Bal (€)": df_view["running_balance"]
    })

    st.caption("💡 Double-click any cell to edit, or select a row on the left and press `Delete` to remove it, then click **Save Table Edits**.")

    edited_df = st.data_editor(
        display_df,
        key="tx_editor",
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        disabled=["Running Bal (€)"],
        column_config={
            "Date": st.column_config.DateColumn("Date", required=True),
            "Description": st.column_config.TextColumn("Description", required=True),
            "Amount (€)": st.column_config.NumberColumn("Amount (€)", format="€%.2f", required=True),
            "Running Bal (€)": st.column_config.NumberColumn("Running Bal (€)", format="€%.2f")
        }
    )

    editor_state = st.session_state.get("tx_editor", {})
    has_pending_edits = bool(editor_state.get("edited_rows") or editor_state.get("deleted_rows"))

    if has_pending_edits:
        if st.button("💾 Save Table Edits to Database", type="primary"):
            apply_editor_changes(df_view, editor_state)
else:
    st.info("No transactions in the database yet. Log one above or import your Excel file in the sidebar.")

# --- Sidebar: One-Click Excel Importer & Export ---
with st.sidebar:
    st.header("⚙️ Data Management")
    st.subheader("Import ProShop Balance.xlsx")
    uploaded_file = st.file_uploader("Upload Excel sheet", type=["xlsx"])

    if uploaded_file is not None:
        # Read starting from row 4 (index 3) to skip the summary block at the top
        raw_excel = pd.read_excel(uploaded_file, skiprows=3, names=["Amount", "Date", "Description"])
        raw_excel = raw_excel.dropna(subset=["Amount", "Date", "Description"])
        raw_excel["Date"] = pd.to_datetime(raw_excel["Date"]).dt.date

        st.write(f"Ready to import **{len(raw_excel)}** transactions.")
        if st.button("🚀 Bulk Import to Supabase", type="primary", use_container_width=True):
            records_to_insert = [
                {
                    "transaction_date": row["Date"].isoformat(),
                    "description": str(row["Description"]).strip(),
                    "amount": float(row["Amount"])
                }
                for _, row in raw_excel.iterrows()
            ]
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
