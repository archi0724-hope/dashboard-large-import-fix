"""Shared catalog screens for the admin dashboard and restricted sales workspace."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from catalog_search import (CATALOG_TEMPLATE, catalog_documents, comparison_rows, find_offers,
                            load_catalog, parse_requirements, refresh_catalog)
from exports import csv_bytes


def refresh_search(store, documents):
    with st.status("Reading saved catalogs…", expanded=True) as status:
        result = refresh_catalog(store, documents,
                                 lambda i, n, filename: status.update(label=f"Reading catalog {i} of {n}: {filename}"))
        status.update(label=f"Search updated: {result['updated']} catalogs processed", state="complete", expanded=False)


def render_catalog_setup(store, documents):
    st.subheader("Prepare catalogs for your sales team")
    st.write("Upload vendor catalogs and price lists through **Upload documents**, including your existing Google Drive ZIP import. Keep each vendor’s files in its own company folder.")
    st.caption("Only available files assigned to a vendor and classified exclusively as Catalogue / Price appear in sales search. Separate mixed catalog/KYC files first. Use Review files to correct the vendor or document type.")
    st.download_button("Download price-list template", CATALOG_TEMPLATE.encode("utf-8-sig"),
                       "Vendor_Price_List_Template.csv", "text/csv", key="catalog_template")
    st.caption("The template contains fictional examples. Replace them with vendor data. Use one row per product; Price is per stated Unit, Stock and MOQ use that same unit. Add pack size for boxes/packs and use YYYY-MM-DD for Valid until.")
    with st.expander("Supported catalogs and price comparison"):
        st.write("CSV and Excel (.xlsx) price lists support Product, Specifications, Category, Brand, Model, Price, Currency, Unit, MOQ, Stock, Contact, Valid until and Terms. These fields provide the most reliable comparisons.")
        st.write("Text PDFs, Word (.docx) and TXT catalogs provide searchable excerpts with source references. Explicit single-line prices with a currency and unit can be compared; ambiguous price tables require a quote. Scanned images and image-only PDFs need OCR or a structured price list.")
        st.write("Lowest-price labels compare matching descriptions, specifications, brands/models, currencies, selling units and price terms across vendors. Check the original catalog and request a final quote, including tax and delivery.")
    if st.button("Refresh catalog search", key="refresh_catalog", type="primary"):
        refresh_search(store, documents)
    entries, reports, pending = load_catalog(store, documents)
    cols = st.columns(3)
    cols[0].metric("Catalog files", len(reports))
    cols[1].metric("Search entries", len(entries))
    cols[2].metric("Pending files", pending)
    if reports:
        st.dataframe(pd.DataFrame(reports), hide_index=True, width="stretch")
    else:
        st.info("No assigned catalogs or price lists yet. Upload a catalog to get started.")
    st.subheader("Sales team access")
    st.write("Share your hosted dashboard address with `?view=sales` at the end. Configure a separate SALES_PASSWORD in your hosting secrets; this login can search catalogs and download comparisons, with no access to document administration.")
    st.link_button("Open sales view", "?view=sales")


def render_sales_assistant(store, documents, is_admin=False):
    st.title("Sales catalog assistant")
    st.write("Describe what your customer needs. Find vendors, compare listed prices and open the supporting catalog.")
    entries, reports, pending = load_catalog(store, documents)
    product_count = sum(entry["kind"] == "product" for entry in entries)
    cols = st.columns(3)
    cols[0].metric("Vendors with catalogs", catalog_documents(documents).company_key.nunique())
    cols[1].metric("Catalogs", len(reports))
    cols[2].metric("Priced products", sum(entry["price"] is not None for entry in entries))
    st.caption("Search saved catalogs · Prices are vendor listings, not live quotes · Currency, unit and specifications stay visible")
    if not reports:
        st.info("Your catalog library is empty. An administrator can upload vendor catalogs and price lists from Upload documents.")
    elif pending:
        st.info(f"{pending} catalogs need indexing. They will be read when you send your next requirement.")
    elif not entries:
        st.warning("The saved catalogs have no readable search entries. Ask an administrator to check Catalog setup for files needing OCR or conversion.")
    if entries and not product_count:
        st.info("Catalog text is searchable. Upload structured CSV/Excel price lists to compare product prices.")
    with st.expander("How to describe a requirement"):
        st.write("Use a product name and specifications, with quantity first. Put different products on separate lines or separate them with a semicolon. Include the currency in a budget, for example: **20 office chairs under INR 5000 each; 500 5ml syringes**.")
        st.write("Quantity defaults to 1 piece. For packs, specify the pack size, e.g. **10 packs of 100 syringes**. Each message is a new search; repeat the product name when refining a requirement.")
        st.write("Results show catalog matches. Unknown prices need a quote. Expired prices, insufficient listed stock and minimum-order conflicts are flagged. Lowest-price labels apply only to comparable listings; confirm stock, specifications, tax and delivery with the vendor.")
    st.session_state.setdefault("sales_messages", [])
    if st.button("New search", key="clear_sales_chat"):
        st.session_state["sales_messages"] = []
    prompt = st.chat_input("E.g. 20 office chairs under INR 5000 each; 500 5ml syringes", key="sales_prompt", max_chars=4000)
    if prompt:
        try:
            parse_requirements(prompt)
        except ValueError as error:
            st.error(str(error))
        else:
            indexed_during_search = bool(pending)
            if pending:
                refresh_search(store, documents)
                entries, reports, pending = load_catalog(store, documents)
            st.session_state["sales_messages"] = (st.session_state["sales_messages"] + [prompt])[-6:]
            if indexed_during_search:
                st.rerun()
    if not st.session_state["sales_messages"]:
        with st.chat_message("assistant"):
            st.write("What does your customer need? Try **20 office chairs**, **500 5ml syringes**, or a list of products and quantities.")
    for message_index, text in enumerate(st.session_state["sales_messages"]):
        with st.chat_message("user"):
            st.text(text)
        with st.chat_message("assistant"):
            rows, all_offers = [], []
            for req_index, requirement in enumerate(parse_requirements(text)):
                st.markdown(f"**Item {req_index + 1}**")
                st.text(requirement.text)
                st.caption(f"Quantity: {requirement.quantity:,} · Selling unit: {requirement.unit}" +
                           (f" · Budget per unit: {requirement.currency or 'currency needed'} {requirement.budget:,.2f}" if requirement.budget is not None else ""))
                offers = find_offers(entries, requirement)
                if not offers:
                    st.info("No catalog match for this requirement and budget. Try a broader product name or ask the administrator to add the vendor’s catalog/price list.")
                    continue
                st.write(f"Found {len(offers)} matching listings from {len({offer['vendor'] for offer in offers})} vendors.")
                comparison = comparison_rows(requirement, offers)
                rows.extend(comparison)
                all_offers.extend(offers)
                st.dataframe(pd.DataFrame(comparison).drop(columns=["Requirement"]), hide_index=True, width="stretch")
                for offer in [offer for offer in offers if offer["lowest"]][:3]:
                    st.success(f"Lowest comparable listed price: {offer['vendor']} · {offer['product']} — {offer['currency']} {offer['price']:,.2f} / {offer['unit']}. Estimated line total: {offer['currency']} {offer['total']:,.2f}.")
                if not any(offer["eligible"] for offer in offers):
                    st.caption("No confirmed price basis for this quantity. Review the Status column and request vendor quotes.")
            if rows:
                st.download_button("Download vendor comparison", csv_bytes(pd.DataFrame(rows)), "Vendor_Comparison.csv", "text/csv",
                                   key=f"sales_comparison_{message_index}")
                st.caption("Line totals multiply listed unit price by requested quantity. Tax and delivery follow the stated terms; unstated charges are not included. Sources and upload dates are included in the download.")
            if all_offers:
                with st.expander("View catalog evidence and download source"):
                    options = list(range(len(all_offers)))
                    selected = st.selectbox("Matching listing", options,
                                            format_func=lambda i: f"{all_offers[i]['vendor']} · {all_offers[i]['product'][:65]} · {all_offers[i]['location']}",
                                            key=f"sales_source_{message_index}")
                    offer = all_offers[selected]
                    st.text(offer["evidence"])
                    st.caption(f"{offer['filename']} · {offer['location']} · Uploaded {offer['uploaded_at'][:10]}")
                    if st.button("Prepare catalog download", key=f"sales_prepare_{message_index}"):
                        # Validate against the current sales-visible register before reading bytes.
                        visible = catalog_documents(store.documents())
                        if offer["document_id"] in set(visible.id):
                            payload = store.read_bytes(offer["document_id"])
                            if payload:
                                st.download_button("Download source catalog", payload, offer["filename"],
                                                   key=f"sales_download_{message_index}")
                            else:
                                st.warning("Source file is no longer available.")
    if pending and st.session_state["sales_messages"]:
        st.warning(f"{pending} catalogs could not be indexed. These results do not include those files; ask the administrator to check Catalog setup.")
