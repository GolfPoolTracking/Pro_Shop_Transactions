"""BRS purse tab for the Pro Shop Streamlit app.

Drop brs_import.py and this file next to proshop_app.py, then in the app:

    from brs_pl_tab import render_brs_tab
    ...
    with tab_brs:                      # or however your tabs/pages are set up
        render_brs_tab(supabase)       # the same supabase client the app already uses

It does two things:
  1. Upload / paste a BRS purse export, check it, and import it (safe to repeat).
  2. Show competition profit/loss by year.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from brs_import import TABLE, parse_export, summary, totals

PAGE = 1000          # Supabase returns at most 1000 rows per request
UPSERT_CHUNK = 200


# --------------------------------------------------------------------------
# database helpers (no Streamlit calls in here, so they can be tested)
# --------------------------------------------------------------------------
def fetch_all(supabase, columns: str, order: str | None = None) -> list[dict]:
    out, start = [], 0
    while True:
        q = supabase.table(TABLE).select(columns)
        if order:
            q = q.order(order)
        batch = q.range(start, start + PAGE - 1).execute().data or []
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        start += PAGE


def stored_hashes(supabase) -> set[str]:
    return {r["row_hash"] for r in fetch_all(supabase, "row_hash", order="id")}


def import_rows(supabase, rows) -> int:
    """Upsert in the order given. Returns how many rows were sent."""
    payload = [r.to_db() for r in rows]
    for i in range(0, len(payload), UPSERT_CHUNK):
        supabase.table(TABLE).upsert(payload[i:i + UPSERT_CHUNK],
                                     on_conflict="row_hash").execute()
    return len(payload)


def pl_by_year(transactions: list[dict]) -> pd.DataFrame:
    """Same maths as the brs_pl_by_year view, computed from stored rows so the
    tab works even before the view exists."""
    if not transactions:
        return pd.DataFrame()
    df = pd.DataFrame(transactions)
    df["year"] = pd.to_datetime(df["txn_at"]).dt.year
    for c in ("debit", "credit"):
        df[c] = df[c].astype(float)

    def agg(cat, col):
        return (df[df["category"] == cat].groupby("year")[col].sum())

    out = pd.DataFrame({
        "Entries": df[df["category"] == "entry_fee"].groupby("year").size(),
        "Entry fees": agg("entry_fee", "debit"),
        "Guest fees": agg("guest_fee", "debit"),
        "Refunds": agg("refund", "credit"),
        "Winnings": agg("winnings", "credit"),
        "Other spend": agg("other_spend", "debit"),
    }).reindex(sorted(df["year"].unique())).fillna(0)
    out["Net (own play)"] = out["Winnings"] + out["Refunds"] - out["Entry fees"]
    out["Net (incl. guests)"] = out["Net (own play)"] - out["Guest fees"]
    out["Entries"] = out["Entries"].astype(int)
    out.index.name = "Year"
    return out


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def _eur(x: float) -> str:
    return f"-€{abs(x):,.2f}" if x < 0 else f"€{x:,.2f}"


def _show_pl(supabase):
    st.subheader("Competition profit / loss")
    cols = "txn_at,category,debit,credit,balance,competition,notes,needs_review"
    data = fetch_all(supabase, cols, order="id")
    if not data:
        st.info("No BRS purse transactions stored yet. Import an export below.")
        return
    df = pl_by_year(data)
    tot = df.sum(numeric_only=True)

    a, b = st.columns(2)
    a.metric("Net, your own play", _eur(tot["Net (own play)"]))
    b.metric("Net, incl. guest fees", _eur(tot["Net (incl. guests)"]))
    c, d = st.columns(2)
    c.metric("Entry fees paid", _eur(tot["Entry fees"]))
    d.metric("Winnings + refunds", _eur(tot["Winnings"] + tot["Refunds"]))

    latest = sorted(data, key=lambda r: r["txn_at"])[-1]
    st.caption(f"Purse balance after the latest stored row "
               f"({latest['txn_at'][:10]}): {_eur(float(latest['balance']))}. "
               "This should match the balance on BRS.")

    st.bar_chart(df[["Net (own play)"]], height=220)

    show = df.copy()
    show.index = show.index.astype(str)
    st.dataframe(show.style.format(
        {c: "{:,.2f}" for c in show.columns if c != "Entries"}),
        use_container_width=True)
    st.caption("Years use the date BRS charged the purse. Top-ups are not "
               "counted as income. Guest entries and Swing Room spend are "
               "shown separately.")

    review = [r for r in data if r.get("needs_review")]
    if review:
        with st.expander(f"{len(review)} row(s) categorised by best guess"):
            st.dataframe(pd.DataFrame(review)[
                ["txn_at", "category", "debit", "credit", "competition", "notes"]],
                use_container_width=True)


def _import_panel(supabase):
    st.subheader("Import BRS purse export")
    st.caption("Copy the whole purse transaction list from BRS (oldest to "
               "newest or the reverse) and paste it here, or upload it as a "
               "text file. Importing the full list every time is fine: rows "
               "already stored are not duplicated.")

    up = st.file_uploader("Export file", type=["txt", "tsv", "csv"])
    pasted = st.text_area("...or paste it here", height=120)
    text = up.getvalue().decode("utf-8-sig", errors="replace") if up else pasted
    if not text.strip():
        return

    parsed = parse_export(text)
    if parsed.rows:
        existing = stored_hashes(supabase)
        new = [r for r in parsed.rows if r.row_hash not in existing]
        a, b, c = st.columns(3)
        a.metric("Rows read", len(parsed.rows))
        b.metric("New", len(new))
        c.metric("Already stored", len(parsed.rows) - len(new))
        with st.expander("Category totals in this export"):
            st.code(summary(parsed.rows))
    if parsed.review:
        with st.expander(f"{len(parsed.review)} row(s) need a look"):
            for line in parsed.review:
                st.write("- " + line)
    if parsed.problems:
        st.error(f"{len(parsed.problems)} problem(s). Nothing was imported.")
        for p in parsed.problems[:15]:
            st.write("- " + p)
        return

    st.success("Checks passed: every balance follows from the one before it.")
    if not new:
        st.info("Nothing new to import.")
        return
    if st.button(f"Import {len(new)} new row(s)", type="primary"):
        try:
            # Send everything (not just new) so improved categories also
            # update rows already stored.
            import_rows(supabase, parsed.rows)
        except Exception as e:
            st.error(f"Import failed: {e}")
            return
        st.success(f"Imported. Latest balance in this export: "
                   f"{_eur(float(parsed.rows[-1].balance))}")
        st.rerun()


def render_brs_tab(supabase):
    _show_pl(supabase)
    st.divider()
    _import_panel(supabase)
