"""Embed the original QuoteSarthi interface in Vendor AI Assistant."""
import streamlit as st


def render_assistant():
    st.subheader("Vendor AI Assistant")
    st.caption("QuoteSarthi AI · Connected to your vendor catalogue and HospKart products.")
    st.iframe("/chatbot/ui/", height=1100, width="stretch")
