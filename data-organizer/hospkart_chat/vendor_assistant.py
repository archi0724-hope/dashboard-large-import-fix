"""Embed the original QuoteSarthi interface in Vendor AI Assistant."""
import streamlit as st


def render_assistant(store=None):
    st.subheader("Vendor AI Assistant")
    st.caption("QuoteSarthi AI · Answers from the data you upload to this dashboard.")
    if store is not None:
        documents = store.documents()
        count = store.product_record_count()
        files, products = st.columns(2)
        files.metric("Uploaded documents", len(documents))
        products.metric("Searchable catalogue records", count)
        if count:
            st.success("Your uploaded catalogue is connected. New uploads and corrections are checked when you ask a question.")
        else:
            st.info("Your saved documents can answer checklist questions. Upload a catalogue or price list to enable product search and quotations.")
        st.caption("Try: ‘Acme GST documents’, a product name, or a quotation request. Answers include source filenames; missing prices are not guessed.")
    st.iframe("/chatbot/ui/", height=1100, width="stretch")
