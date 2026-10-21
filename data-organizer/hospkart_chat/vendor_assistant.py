"""Embed the original QuoteSarthi interface in Vendor AI Assistant."""
import streamlit as st


def render_assistant(store=None):
    st.subheader("Vendor AI Assistant")
    st.caption("QuoteSarthi AI · Searches your uploaded catalogue and readable document contents.")
    if store is not None:
        documents = store.documents()
        count = store.product_record_count()
        files, products = st.columns(2)
        files.metric("Uploaded documents", len(documents))
        products.metric("Searchable catalogue records", count)
        if count:
            st.success("Your uploaded catalogue is connected. New uploads and corrections are checked when you ask a question.")
        else:
            st.info(
                "Your readable uploaded documents can answer content questions and checklist questions. "
                "Upload a catalogue or price list to enable product search and quotations."
            )
        st.caption(
            "Uploaded text is searched live; uploading does not fine-tune model weights. "
            "Test it by asking about payment terms, certificates, or another fact written in an uploaded file. "
            "QuoteSarthi returns matching excerpts with source filenames; missing prices are not guessed."
        )
    st.iframe("/chatbot/ui/", height=1100, width="stretch")
