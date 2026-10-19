"""Serve the existing dashboard and its public liveness route on one port."""

from pathlib import Path

import streamlit as st

from vendor_dashboard.web.application import health_routes

# Streamlit's CLI discovers the ASGI constructor statically in this launcher.
app = st.App(Path(__file__).with_name("app.py"), routes=health_routes())
