#!/usr/bin/env python3
"""Parse a BRS purse export (copy/pasted, tab separated) for Supabase.

Usage:
    python brs_import.py export.txt                 # checks + writes brs_inserts.sql
    python brs_import.py export.txt -o other.sql
    python brs_import.py export.txt --report        # checks only, no SQL written
    python brs_import.py export.txt --force         # write SQL even if checks fail

Run the generated .sql in the Supabase SQL editor. It upserts on row_hash, so
you can re-run it, or load a newer export that overlaps the old one, without
creating duplicates.

parse_export() is also used by brs_pl_tab.py for in-app uploads.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

TABLE = "brs_purse_transactions"

# Pay methods seen in the export. A new one would be mistaken for a
# competition name, so add it here if BRS ever introduces another.
PAY_METHODS = {"realex", "voucher", "cash", "online"}

# Your own name as BRS shows it (plus the "Guest with ..." variant).
PLAYER_RE = re.compile(r"^(guest with )?(eddie|edmund) mcburney$", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

DATE_ALONE = re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4}\s*$")
TIME_START = re.compile(r"^\d{2}:\d{2}\t")
DATE_JOIN = re.compile(r"(\d{2}-[A-Za-z]{3}-\d{4})\n(\d{2}:\d{2})")
DATE_TOKEN = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})(?: (\d{2}):(\d{2}))?$")
MONEY = re.compile(r"^-?\d[\d,]*\.\d{2}$")

CATEGORIES = ("entry_fee", "guest_fee", "refund", "winnings",
              "topup", "other_spend", "other_credit")


@dataclass
class Txn:
    txn_at: datetime
    username: str | None
    tee_date: date | None
    tee_time: time | None
    competition: str | None
    player: str | None
    pay_method: str | None
    notes: str | None
    debit: Decimal
    credit: Decimal
    balance: Decimal
    category: str
    needs_review: bool
    source: str | None = None
    line_items: str | None = None

    @property
    def row_hash(self) -> str:
        # Only fields that never change, so improving the text parsing later
        # still matches (and updates) rows that are already stored.
        key = (f"{self.txn_at.isoformat()}|{self.debit:.2f}|"
               f"{self.credit:.2f}|{self.balance:.2f}")
        return hashlib.md5(key.encode()).hexdigest()

    def to_db(self) -> dict:
        """JSON-safe dict for supabase-py .upsert()."""
        return {
            "row_hash": self.row_hash,
            "txn_at": self.txn_at.isoformat(),
            "username": self.username,
            "tee_date": self.tee_date.isoformat() if self.tee_date else None,
            "tee_time": self.tee_time.isoformat() if self.tee_time else None,
            "competition": self.competition,
            "player": self.player,
            "pay_method": self.pay_method,
            "notes": self.notes,
            "debit": float(self.debit),
            "credit": float(self.credit),
            "balance": float(self.balance),
            "category": self.category,
            "needs_review": self.needs_review,
            "source": self.source,
            "line_items": self.line_items,
        }


@dataclass
class Parsed:
    rows: list[Txn] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # block the import
    review: list[str] = field(default_factory=list)     # worth a look


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def _money(s: str) -> Decimal:
    s = s.replace(",", "").strip()
    if not s:
        return Decimal("0.00")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"bad amount {s!r}")


def _date_token(s: str):
    """'29-Apr-2018 07:40' or '29-Apr-2018' -> (datetime, has_time) or None."""
    m = DATE_TOKEN.match(s)
    if not m:
        return None
    d, mon, y, hh, mm = m.groups()
    month = MONTHS.get(mon.lower())
    if not month:
        return None
    return datetime(int(y), month, int(d), int(hh or 0), int(mm or 0)), hh is not None


def _split_records(text: str) -> list[str]:
    """A record starts with a date alone on a line, then a line beginning
    'HH:MM<tab>'. (Tee dates sit at the END of a line, never alone.)"""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    starts = [i for i in range(len(lines) - 1)
              if DATE_ALONE.match(lines[i]) and TIME_START.match(lines[i + 1])]
    chunks = []
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunks.append("\n".join(lines[s:e]))
    return chunks


def _categorise(debit, credit, pay, comp, player, notes):
    """Returns (category, needs_review)."""
    pay_l = (pay or "").lower()
    text = f"{comp or ''} {notes or ''}".lower()
    if credit:
        if pay_l in ("realex", "cash"):
            return "topup", False
        if pay_l == "voucher":
            return "winnings", False
        if any(w in text for w in ("refund", "cancel", "amend")):
            return "refund", False
        if pay_l == "online":
            return "other_credit", False
        return "other_credit", True
    if "swing room" in text:
        return "other_spend", False
    if player and player.lower().startswith("guest with"):
        return "guest_fee", False
    if comp:
        return "entry_fee", False
    return "other_spend", True


def _parse_record(chunk: str) -> Txn:
    chunk = chunk.rstrip("\n").replace("\\'", "'")
    chunk = DATE_JOIN.sub(r"\1 \2", chunk)       # "date\ntime" -> "date time"
    chunk = chunk.replace("\n", "\t")            # any other newline = cell break
    f = [x.strip() for x in chunk.split("\t")]

    dt = _date_token(f[0])
    if not dt or not dt[1]:
        raise ValueError("no transaction date/time")
    txn_at = dt[0]

    # Anchor on the right: debit | credit | balance (exactly one of debit or
    # credit is filled), followed by Source and Line Items.
    bal_i = None
    for i in range(len(f) - 1, 2, -1):
        a, b = f[i - 2], f[i - 1]
        if MONEY.match(f[i]) and ((MONEY.match(a) and b == "") or (a == "" and MONEY.match(b))):
            bal_i = i
            break
    if bal_i is None:
        raise ValueError("could not find debit/credit/balance columns")
    debit, credit, balance = _money(f[bal_i - 2]), _money(f[bal_i - 1]), _money(f[bal_i])
    tail = f[bal_i + 1:]
    source = tail[0] if tail and tail[0] else None
    line_items = " ".join(t for t in tail[1:] if t) or None

    # Left-hand text cells. Some rows omit the empty cells, so classify each
    # remaining token by what it looks like instead of by position.
    toks = [t for t in f[1:bal_i - 2] if t]
    username = tee_date = tee_time = pay = comp = player = notes = None
    if toks and not _date_token(toks[0]) and toks[0].lower() not in PAY_METHODS:
        username = toks.pop(0)
    if toks and _date_token(toks[0]):
        tdt, has_time = _date_token(toks.pop(0))
        tee_date, tee_time = tdt.date(), (tdt.time() if has_time else None)
    for k, t in enumerate(toks):
        if t.lower() in PAY_METHODS:
            pay = toks.pop(k)
            break
    for k, t in enumerate(toks):
        if PLAYER_RE.match(t):
            player = toks.pop(k)
            break
    if len(toks) >= 2:
        comp, notes = toks[0], " | ".join(toks[1:])
    elif len(toks) == 1:
        if pay is None and player is not None:
            comp = toks[0]          # competition row: name, then player
        else:
            notes = toks[0]         # voucher/top-up/swing room text

    category, review = _categorise(debit, credit, pay, comp, player, notes)
    return Txn(txn_at, username, tee_date, tee_time, comp, player,
               pay.title() if pay else None, notes, debit, credit, balance,
               category, review, source, line_items)


def chain_problems(rows: list[Txn]) -> list[str]:
    """Each row's balance must equal previous balance - debit + credit."""
    out = []
    for prev, cur in zip(rows, rows[1:]):
        expected = prev.balance - cur.debit + cur.credit
        if expected != cur.balance:
            out.append(
                f"Balance mismatch at {cur.txn_at:%d-%b-%Y %H:%M}: expected "
                f"{expected}, export says {cur.balance} (rows missing or "
                f"misread just before this one?)")
    return out


def parse_export(text: str) -> Parsed:
    result = Parsed()
    chunks = _split_records(text)
    if not chunks:
        result.problems.append(
            "No transactions found. Expected the tab-separated text copied "
            "from the BRS purse page, with each row starting with a date on "
            "its own line.")
        return result
    for n, chunk in enumerate(chunks, 1):
        try:
            result.rows.append(_parse_record(chunk))
        except Exception as e:  # keep going so every bad row is reported
            first = chunk.split("\n", 1)[0]
            result.problems.append(f"Record {n} ({first}): {e}")
    rows = result.rows
    if len(rows) > 1 and rows[0].txn_at > rows[-1].txn_at:
        rows.reverse()                       # newest-first export
    seen = set()
    for r in rows:
        if r.row_hash in seen:
            result.problems.append(
                f"Two rows look identical at {r.txn_at:%d-%b-%Y %H:%M} "
                f"(same amount and balance).")
        seen.add(r.row_hash)
    result.problems.extend(chain_problems(rows))
    for r in rows:
        if r.needs_review:
            result.review.append(
                f"{r.txn_at:%d-%b-%Y %H:%M} {r.category}: "
                f"comp={r.competition!r} notes={r.notes!r} "
                f"pay={r.pay_method!r} debit={r.debit} credit={r.credit}")
    return result


# --------------------------------------------------------------------------
# summary + SQL output
# --------------------------------------------------------------------------
def totals(rows: list[Txn]) -> dict:
    def s(cat, col):
        return sum((getattr(r, col) for r in rows if r.category == cat), Decimal("0"))
    t = {
        "entry_fees": s("entry_fee", "debit"),
        "guest_fees": s("guest_fee", "debit"),
        "refunds": s("refund", "credit"),
        "winnings": s("winnings", "credit"),
        "other_spend": s("other_spend", "debit"),
        "topups": s("topup", "credit"),
    }
    t["net_competition"] = (t["winnings"] + t["refunds"]
                            - t["entry_fees"] - t["guest_fees"])
    return t


def summary(rows: list[Txn]) -> str:
    lines = [f"Rows: {len(rows)}   {rows[0].txn_at:%d-%b-%Y} -> {rows[-1].txn_at:%d-%b-%Y}"]
    opening = rows[0].balance - rows[0].credit + rows[0].debit
    lines.append(f"Opening balance (implied): {opening}   Closing balance: {rows[-1].balance}")
    lines.append("")
    lines.append(f"{'category':<14}{'rows':>6}{'debit':>12}{'credit':>12}")
    for cat in CATEGORIES:
        sel = [r for r in rows if r.category == cat]
        if sel:
            lines.append(f"{cat:<14}{len(sel):>6}{sum(r.debit for r in sel):>12.2f}"
                         f"{sum(r.credit for r in sel):>12.2f}")
    t = totals(rows)
    lines.append("")
    lines.append("Net competition P/L (winnings + refunds - entry & guest fees): "
                 f"{t['net_competition']:.2f}")
    return "\n".join(lines)


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return f"{v:.2f}"
    if isinstance(v, datetime):
        return f"'{v:%Y-%m-%d %H:%M:%S}'"
    if isinstance(v, date):
        return f"'{v:%Y-%m-%d}'"
    if isinstance(v, time):
        return f"'{v:%H:%M:%S}'"
    return "'" + str(v).replace("'", "''") + "'"


COLS = ["row_hash", "txn_at", "username", "tee_date", "tee_time", "competition",
        "player", "pay_method", "notes", "debit", "credit", "balance",
        "category", "needs_review", "source", "line_items"]
UPDATE_COLS = ["username", "tee_date", "tee_time", "competition", "player",
               "pay_method", "notes", "category", "needs_review", "source",
               "line_items"]


def build_sql(rows: list[Txn], chunk: int = 200) -> str:
    parts = [
        "-- Generated by brs_import.py. Safe to run more than once.",
        f"-- {len(rows)} rows, {rows[0].txn_at:%d-%b-%Y} to {rows[-1].txn_at:%d-%b-%Y}",
        "",
    ]
    for s in range(0, len(rows), chunk):
        values = []
        for r in rows[s:s + chunk]:
            vals = [r.row_hash, r.txn_at, r.username, r.tee_date, r.tee_time,
                    r.competition, r.player, r.pay_method, r.notes, r.debit,
                    r.credit, r.balance, r.category, r.needs_review, r.source,
                    r.line_items]
            values.append("(" + ", ".join(_lit(v) for v in vals) + ")")
        parts.append(f"insert into public.{TABLE} ({', '.join(COLS)}) values")
        parts.append(",\n".join(values))
        parts.append("on conflict (row_hash) do update set")
        parts.append(",\n".join(f"  {c} = excluded.{c}" for c in UPDATE_COLS) + ";")
        parts.append("")
    parts += [
        "-- Check against the BRS purse page after running:",
        f"--   expected latest balance: {rows[-1].balance}",
        f"-- select count(*) as rows, max(txn_at) as latest,",
        f"--   (select balance from public.{TABLE} order by txn_at desc, id desc limit 1) as latest_balance",
        f"-- from public.{TABLE};",
    ]
    return "\n".join(parts) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("export", help="BRS purse export text file")
    ap.add_argument("-o", "--out", default="brs_inserts.sql")
    ap.add_argument("--report", action="store_true", help="checks only, write no SQL")
    ap.add_argument("--force", action="store_true", help="write SQL even if checks fail")
    args = ap.parse_args(argv)

    with open(args.export, encoding="utf-8-sig", errors="replace") as fh:
        parsed = parse_export(fh.read())

    if parsed.rows:
        print(summary(parsed.rows))
    if parsed.review:
        print("\nRows worth a look (categorised by best guess):")
        for line in parsed.review:
            print("  -", line)
    if parsed.problems:
        print(f"\n{len(parsed.problems)} problem(s):")
        for p in parsed.problems[:25]:
            print("  !", p)
        if len(parsed.problems) > 25:
            print(f"  ... and {len(parsed.problems) - 25} more")
    else:
        print("\nChecks passed: every balance follows from the one before it.")

    if args.report or not parsed.rows:
        return 1 if parsed.problems else 0
    if parsed.problems and not args.force:
        print("\nNo SQL written (use --force to override).")
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(build_sql(parsed.rows))
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
