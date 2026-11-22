"""Sentry error reporting for the Pro Shop app.

Reads SENTRY_DSN from Streamlit secrets. If it's missing (e.g. running
locally without it), the app runs exactly as before with no reporting.

How errors reach Sentry: Streamlit catches app errors itself and logs them
at ERROR level ("Uncaught app execution"). Sentry's built-in logging
integration picks those log entries up and sends them, full traceback
included, so no try/except wrapping is needed around the app code.
"""
import sentry_sdk
import streamlit as st


@st.cache_resource
def init_sentry() -> bool:
    """Start Sentry once per server process (cached across reruns)."""
    dsn = st.secrets.get("SENTRY_DSN")
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment="production",
        send_default_pii=False,   # don't send IPs / user details
        traces_sample_rate=0.0,   # errors only, no performance tracing
    )
    return True
