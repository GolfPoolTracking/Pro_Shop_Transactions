"""Golf profit/loss view for the Pro Shop Streamlit app.

Keep brs_import.py and this file next to app.py. app.py calls:

    from brs_pl_tab import render_brs_tab
    render_brs_tab(supabase)

Three sub-tabs:
  Lifetime    BRS purse + Pro Shop account combined: costs, winnings, net, by year
  BRS purse   BRS-only breakdown (entry fees, guests, refunds, winnings, ...)
  Import      upload / paste a BRS purse export (safe to repeat)
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from brs_import import TABLE, parse_export, summary

PROSHOP_TABLE = "proshop_transactions"
PAGE = 1000          # Supabase returns at most 1000 rows per request
UPSERT_CHUNK = 200
STALE_DAYS = 14      # warn if the newest stored BRS row is older than this


# --------------------------------------------------------------------------
# database helpers (no Streamlit calls, so they can be tested)
# --------------------------------------------------------------------------
def fetch_all(supabase, columns: str, order: str | None = None,
              table: str = TABLE) -> list[dict]:
    out, start = [], 0
    while True:
        q = supabase.table(table).select(columns)
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


# --------------------------------------------------------------------------
# maths (pure functions)
# --------------------------------------------------------------------------
EVENT_COLS = ["date", "kind", "amount", "source", "label", "entry"]


def build_events(brs: list[dict], proshop: list[dict] | None,
                 include_guest: bool = True,
                 include_swing: bool = False) -> pd.DataFrame:
    """One row per cost or winning. amount is signed within its kind:
    refunds are negative costs. Costs come only from the BRS purse.
    Pro Shop credits are winnings; Pro Shop debits are winnings being spent
    in the shop, so they are NOT costs. BRS top-ups and unclassified credits
    are ignored because moving money around is neither a cost nor a win."""
    ev = []
    for r in brs:
        cat, d = r["category"], str(r["txn_at"])[:10]
        deb, cred = float(r["debit"]), float(r["credit"])
        label = r.get("competition") or r.get("notes") or cat
        if cat == "entry_fee":
            ev.append((d, "cost", deb, "BRS purse", label, 1))
        elif cat == "guest_fee" and include_guest:
            ev.append((d, "cost", deb, "BRS purse", f"Guest: {label}", 0))
        elif cat == "other_spend" and include_swing:
            ev.append((d, "cost", deb, "BRS purse", label, 0))
        elif cat == "refund":
            ev.append((d, "cost", -cred, "BRS purse", f"Refund: {label}", 0))
        elif cat == "winnings":
            ev.append((d, "win", cred, "BRS purse", label, 0))
    for r in proshop or []:
        amt = float(r["amount"])
        if amt > 0:
            desc = str(r.get("description") or "")
            ev.append((str(r["transaction_date"])[:10], "win", amt,
                       "Pro Shop", desc, 0))
    df = pd.DataFrame(ev, columns=EVENT_COLS)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df["year"] = df["date"].dt.year
    return df


def yearly(ev: pd.DataFrame) -> pd.DataFrame:
    if ev.empty:
        return pd.DataFrame()
    years = range(int(ev["year"].min()), int(ev["year"].max()) + 1)
    out = pd.DataFrame({
        "Entries": ev.groupby("year")["entry"].sum(),
        "Costs": ev[ev["kind"] == "cost"].groupby("year")["amount"].sum(),
        "Winnings": ev[ev["kind"] == "win"].groupby("year")["amount"].sum(),
    }).reindex(years).fillna(0)
    out["Net"] = out["Winnings"] - out["Costs"]
    out["Return %"] = (out["Winnings"] / out["Costs"] * 100).where(out["Costs"] > 0)
    out["Entries"] = out["Entries"].astype(int)
    out.index.name = "Year"
    return out


def headline(ev: pd.DataFrame, yr: pd.DataFrame) -> dict:
    costs = float(ev.loc[ev["kind"] == "cost", "amount"].sum())
    wins = float(ev.loc[ev["kind"] == "win", "amount"].sum())
    entries = int(ev["entry"].sum())
    brs_entry_cost = float(ev.loc[ev["entry"] == 1, "amount"].sum())
    return {
        "costs": costs, "wins": wins, "net": wins - costs,
        "return_pct": (wins / costs * 100) if costs > 0 else None,
        "entries": entries,
        "avg_entry": (brs_entry_cost / entries) if entries else None,
        "avg_year_cost": costs / len(yr) if len(yr) else None,
        "best_year": int(yr["Net"].idxmax()) if len(yr) else None,
        "worst_year": int(yr["Net"].idxmin()) if len(yr) else None,
    }


def cumulative_net(ev: pd.DataFrame) -> pd.Series:
    signed = ev["amount"].where(ev["kind"] == "win", -ev["amount"])
    month = ev["date"].dt.to_period("M").astype(str)
    return signed.groupby(month).sum().sort_index().cumsum()


def pl_by_year(transactions: list[dict]) -> pd.DataFrame:
    """BRS-only yearly breakdown by category."""
    if not transactions:
        return pd.DataFrame()
    df = pd.DataFrame(transactions)
    df["year"] = pd.to_datetime(df["txn_at"]).dt.year
    for c in ("debit", "credit"):
        df[c] = df[c].astype(float)

    def agg(cat, col):
        return df[df["category"] == cat].groupby("year")[col].sum()

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
# cached loaders (underscore arg = not hashed)
# --------------------------------------------------------------------------
@st.cache_data(ttl=60, show_spinner=False)
def _load_brs(_sb):
    return fetch_all(_sb, "txn_at,category,debit,credit,balance,competition,"
                          "notes,needs_review", order="id")


@st.cache_data(ttl=60, show_spinner=False)
def _load_proshop(_sb):
    try:
        return fetch_all(_sb, "transaction_date,description,amount",
                         order="id", table=PROSHOP_TABLE)
    except Exception:
        return None


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def _eur(x: float) -> str:
    return f"-€{abs(x):,.2f}" if x < 0 else f"€{x:,.2f}"


def _freshness(brs: list[dict]):
    latest = max(str(r["txn_at"])[:10] for r in brs)
    days = (date.today() - date.fromisoformat(latest)).days
    msg = (f"Latest BRS purse row stored: {latest} ({days} days ago). "
           "Import a fresh export in the Import tab to bring it up to date.")
    (st.warning if days > STALE_DAYS else st.caption)(msg)


def _lifetime(supabase):
    brs = _load_brs(supabase)
    if not brs:
        st.info("No BRS purse transactions stored yet. Use the Import tab.")
        return
    proshop = _load_proshop(supabase)
    _freshness(brs)
    if proshop is None:
        st.warning("Couldn't read the Pro Shop account, so this shows the "
                   "BRS purse only.")

    with st.expander("What counts as a cost or a win"):
        inc_guest = st.checkbox("Count guest fees as costs", value=True)
        inc_swing = st.checkbox("Count Swing Room spend as a cost", value=False)
        st.caption("Costs are what BRS charges you (entry fees, minus refunds). "
                   "Pro Shop credits count as winnings. Pro Shop spending is "
                   "you spending those winnings in the shop, so it is not "
                   "counted as a cost. BRS top-ups are never winnings.")

    ev = build_events(brs, proshop, inc_guest, inc_swing)
    if ev.empty:
        st.info("Nothing to show yet.")
        return
    yr = yearly(ev)
    h = headline(ev, yr)

    a, b = st.columns(2)
    a.metric("Lifetime net", _eur(h["net"]))
    b.metric("Return on spend",
             f"{h['return_pct']:.0f}%" if h["return_pct"] is not None else "n/a")
    c, d = st.columns(2)
    c.metric("Total costs", _eur(h["costs"]))
    d.metric("Total winnings", _eur(h["wins"]))
    e, f = st.columns(2)
    e.metric("Competition entries", f"{h['entries']:,}")
    f.metric("Avg entry fee",
             _eur(h["avg_entry"]) if h["avg_entry"] is not None else "n/a")
    st.caption(f"Average cost per year: {_eur(h['avg_year_cost'])}.")

    st.markdown("**Costs vs winnings by year**")
    chart = yr[["Costs", "Winnings"]].copy()
    chart.index = chart.index.astype(str)
    st.bar_chart(chart, height=240)

    st.markdown("**Running net since the start**")
    st.line_chart(cumulative_net(ev), height=200)

    st.markdown("**By year**")
    show = yr.copy()
    show.index = show.index.astype(str)
    st.dataframe(show.style.format({
        "Costs": "{:,.2f}", "Winnings": "{:,.2f}", "Net": "{:,.2f}",
        "Return %": "{:,.0f}"}, na_rep="-"), use_container_width=True)
    st.download_button("📥 Download yearly table (CSV)",
                       yr.to_csv().encode("utf-8"),
                       file_name=f"golf_pl_by_year_{date.today().isoformat()}.csv",
                       mime="text/csv", use_container_width=True)

    split = ev.assign(
        Costs=ev["amount"].where(ev["kind"] == "cost", 0.0),
        Winnings=ev["amount"].where(ev["kind"] == "win", 0.0),
    ).groupby("source")[["Costs", "Winnings"]].sum()
    split["Net"] = split["Winnings"] - split["Costs"]
    st.markdown("**By account**")
    st.dataframe(split.style.format("{:,.2f}"), use_container_width=True)

    with st.expander("Winnings log (check these are all real wins)"):
        wins = ev[ev["kind"] == "win"].sort_values("date", ascending=False)
        log = pd.DataFrame({
            "Date": wins["date"].dt.date, "Source": wins["source"],
            "Description": wins["label"], "€": wins["amount"]})
        st.dataframe(log, use_container_width=True, hide_index=True)


def _brs_only(supabase):
    data = _load_brs(supabase)
    if not data:
        st.info("No BRS purse transactions stored yet. Use the Import tab.")
        return
    df = pl_by_year(data)
    tot = df.sum(numeric_only=True)
    a, b = st.columns(2)
    a.metric("Net, your own play", _eur(tot["Net (own play)"]))
    b.metric("Net, incl. guest fees", _eur(tot["Net (incl. guests)"]))
    c, d = st.columns(2)
    c.metric("Entry fees paid", _eur(tot["Entry fees"]))
    d.metric("Winnings + refunds", _eur(tot["Winnings"] + tot["Refunds"]))

    latest = sorted(data, key=lambda r: str(r["txn_at"]))[-1]
    st.caption(f"Purse balance after the latest stored row "
               f"({str(latest['txn_at'])[:10]}): {_eur(float(latest['balance']))}. "
               "This should match the balance on BRS.")
    show = df.copy()
    show.index = show.index.astype(str)
    st.dataframe(show.style.format(
        {c: "{:,.2f}" for c in show.columns if c != "Entries"}),
        use_container_width=True)
    st.caption("Years use the date BRS charged the purse. Top-ups are not "
               "counted as income.")
    review = [r for r in data if r.get("needs_review")]
    if review:
        with st.expander(f"{len(review)} row(s) categorised by best guess"):
            st.dataframe(pd.DataFrame(review)[
                ["txn_at", "category", "debit", "credit", "competition", "notes"]],
                use_container_width=True)


def _import_panel(supabase):
    st.caption("Copy the whole purse transaction list from BRS and paste it "
               "here, or upload it as a text file. Importing the full list "
               "every time is fine: rows already stored are not duplicated.")
    up = st.file_uploader("Export file", type=["txt", "tsv", "csv"])
    pasted = st.text_area("...or paste it here", height=120)
    text = up.getvalue().decode("utf-8-sig", errors="replace") if up else pasted
    if not text.strip():
        return

    parsed = parse_export(text)
    new = []
    if parsed.rows:
        existing = stored_hashes(supabase)
        new = [r for r in parsed.rows if r.row_hash not in existing]
        a, b, c = st.columns(3)
        a.metric("Rows read", len(parsed.rows))
        b.metric("New", len(new))
        c.metric("Stored", len(parsed.rows) - len(new))
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
    if st.button(f"Import {len(new)} new row(s)", type="primary",
                 use_container_width=True):
        try:
            # Send everything (not just new) so improved categories also
            # update rows already stored.
            import_rows(supabase, parsed.rows)
        except Exception as e:
            st.error(f"Import failed: {e}")
            return
        st.cache_data.clear()
        st.success(f"Imported. Latest balance in this export: "
                   f"{_eur(float(parsed.rows[-1].balance))}")
        st.rerun()


def render_brs_tab(supabase):
    t_life, t_brs, t_imp = st.tabs(["📊 Lifetime", "🏌️ BRS purse", "⬆️ Import"])
    with t_life:
        _lifetime(supabase)
    with t_brs:
        _brs_only(supabase)
    with t_imp:
        _import_panel(supabase)
