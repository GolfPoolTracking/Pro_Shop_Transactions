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

# --- Helper Functions ---
def fetch_transactions():
    response = supabase.table("proshop_transactions").select("*").order("transaction_date", desc=True).execute()
    if response.data:
        df = pd.DataFrame(response.data)
        # Convert date strings to datetime objects for proper sorting/display
        df['transaction_date'] = pd.to_datetime(df['transaction_date']).dt.date
        return df
    return pd.DataFrame(columns=['id', 'transaction_date', 'description', 'amount', 'created_at'])

def add_transaction(t_date, desc, amt):
    data = {
        "transaction_date": t_date.isoformat(),
        "description": desc,
        "amount": float(amt)
    }
    supabase.table("proshop_transactions").insert(data).execute()

# --- Main App UI ---
st.title("⛳ Pro Shop Balance")

df_transactions = fetch_transactions()

# Calculate totals
calculated_balance = df_transactions['amount'].sum() if not df_transactions.empty else 0.0

# --- Reconciliation Metrics ---
st.subheader("Account Status")
col1, col2, col3 = st.columns(3)

with col1:
    st.metric("Calculated Balance", f"€{calculated_balance:.2f}")

with col2:
    shop_record = st.number_input("Shop System Record (€)", value=float(calculated_balance), step=1.0, format="%.2f")

with col3:
    difference = calculated_balance - shop_record
    st.metric("Difference", f"€{difference:.2f}", delta=float(difference), delta_color="inverse")

st.divider()

# --- New Transaction Entry ---
st.subheader("Log New Transaction")
with st.form("transaction_form", clear_on_submit=True):
    col_date, col_desc, col_amt = st.columns([1, 2, 1])
    
    with col_date:
        entry_date = st.date_input("Date", date.today())
    with col_desc:
        entry_desc = st.text_input("Description (e.g., 2's Entry, SM11 Wedge)")
    with col_amt:
        entry_amount = st.number_input("Amount (€)", value=0.00, step=1.00, format="%.2f")
        
    submitted = st.form_submit_button("Save Transaction")
    if submitted:
        if entry_desc:
            add_transaction(entry_date, entry_desc, entry_amount)
            st.success("Transaction saved!")
            st.rerun()
        else:
            st.error("Please enter a description.")

# --- Transaction History ---
st.subheader("Transaction History")
if not df_transactions.empty:
    # Display the dataframe cleanly without the UUIDs and timestamps
    display_df = df_transactions[['transaction_date', 'description', 'amount']].copy()
    display_df.columns = ['Date', 'Description', 'Amount (€)']
    
    # Format amount column
    st.dataframe(
        display_df, 
        use_container_width=True, 
        hide_index=True,
        column_config={
            "Amount (€)": st.column_config.NumberColumn(format="€%.2f")
        }
    )
else:
    st.info("No transactions found. Add one above to get started.")
