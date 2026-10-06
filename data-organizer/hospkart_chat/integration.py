"""Live catalogue bridge; document imports stay in the Vendor dashboard database."""
from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import importlib.util
import os
from pathlib import Path
import re
import sys
from threading import RLock
import time

import pandas as pd

from .hospkart_agent import HospKartAgent
from .hospkart_rag_setup import HospKartRAG

ROOT = Path(__file__).resolve().parent
VENDOR_ROOT = ROOT.parent / "vendor_dashboard"
session_agents_lock = RLock()


def vendor_setting(name: str, default: str = "") -> str:
    # Use precisely the same configuration precedence as the Vendor dashboard.
    import streamlit as st
    try:
        return str(st.secrets.get(name, os.environ.get(name, default)))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return os.environ.get(name, default)


@lru_cache(maxsize=4)
def open_vendor_store(data_dir: str, database_url: str):
    if str(VENDOR_ROOT) not in sys.path:
        sys.path.insert(0, str(VENDOR_ROOT))
    spec = importlib.util.spec_from_file_location("hospkart_vendor_storage", VENDOR_ROOT / "storage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Store(Path(data_dir), database_url)


def recorded_price(value) -> float:
    """Accept a single recorded amount, never a range or inferred price."""
    text = str(value or "").strip().replace(",", "")
    text = re.sub(r"(?i)^(?:INR|RS\.?|₹)\s*", "", text)
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return 0.0
    return float(text)


def vendor_frame(records: pd.DataFrame) -> pd.DataFrame:
    if records.empty:
        return pd.DataFrame()
    r = records.fillna("")
    out = pd.DataFrame(index=r.index)
    out["id"] = "vendor:" + r["id"].astype(str)
    out["offer_id"] = out["id"]
    out["product_id"] = r["sku"].where(r["sku"] != "", out["id"])
    out["vendor_id"] = r["company_key"]
    out["product_name"] = r["product_name"]
    out["vendor_name"] = r["company_name"]
    out["category"] = r["product_category"]
    out["manufacturer"] = r["brand"]
    out["product_sku"] = r["sku"]
    out["price"] = r["price"].map(recorded_price)
    out["currency"] = r["currency"].astype(str).str.upper().str.strip()
    # The inherited quotation engine calculates INR totals only.
    out.loc[~out["currency"].isin(["", "INR", "RS", "RS.", "₹"]), "price"] = 0.0
    out["regular_price"] = r["mrp"].map(recorded_price)
    out["sale_price"] = 0.0
    out["stock"] = 0
    out["warranty"] = "Not recorded"
    out["vendor_status"] = "Imported document"
    out["product_status"] = r["availability"].where(r["availability"] != "", "Not recorded")
    out["source_file"] = r["source_file"]
    out["source_row"] = r["source_row"].astype(str)
    out["source_sheet"] = r["source_sheet"].astype(str)
    out["source_page"] = r["source_page"].astype(str)
    out["specs"] = r["specification"].astype(str) + " " + r["description"].astype(str)
    out["summary"] = "Source: " + r["source_file"].astype(str) + "; stock quantity not recorded."
    return out[out["product_name"].astype(str).str.strip() != ""].reset_index(drop=True)


class CatalogueSource:
    def __init__(self, store, csv_path: Path):
        self.store, self.csv_path = store, csv_path
        self.signature = None
        self.frame = pd.DataFrame()
        self.vendor_count = self.hospkart_count = 0
        self.loaded_at = 0.0

    def refresh(self):
        csv_stamp = self.csv_path.stat().st_mtime_ns if self.csv_path.exists() else None
        local_stamps = () if self.store.cloud else tuple(
            p.stat().st_mtime_ns if p.exists() else None
            for p in (self.store.db_path, Path(str(self.store.db_path) + "-wal")))
        signature = (self.store.search_index_signature(), csv_stamp, local_stamps)
        if signature == self.signature and (not self.store.cloud or time.monotonic() - self.loaded_at < 30):
            return
        vendor = vendor_frame(self.store.product_records())
        original = pd.read_csv(self.csv_path, low_memory=False).fillna("") if csv_stamp else pd.DataFrame()
        self.vendor_count, self.hospkart_count = len(vendor), len(original)
        frame = pd.concat([vendor, original], ignore_index=True).fillna("")
        for col in ("id", "product_name", "vendor_name", "category", "product_sku", "specs", "summary"):
            if col not in frame:
                frame[col] = ""
        for col in ("price", "regular_price", "sale_price", "stock"):
            if col not in frame:
                frame[col] = 0
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0)
        frame["_names"] = frame["product_name"].astype(str).str.lower()
        frame["_lookup"] = (frame["product_name"].astype(str) + " " + frame["vendor_name"].astype(str)
                            + " " + frame["product_sku"].astype(str) + " " + frame["category"].astype(str)).str.lower()
        self.frame, self.signature = frame, signature
        self.loaded_at = time.monotonic()

    def candidates(self, query: str) -> pd.DataFrame:
        self.refresh()
        stop = {"show", "me", "find", "search", "list", "product", "products", "vendor", "vendors", "supplier",
                "quote", "quotation", "invoice", "create", "generate", "purchase", "tax", "for", "of", "the",
                "and", "with", "units", "unit", "quantity", "qty", "gst", "discount", "price", "pricing", "best",
                "details", "about", "this", "please", "hai", "ki", "ka", "ke", "chahiye", "stock", "compare"}
        tokens = list(dict.fromkeys(t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in stop and not t.isdigit()))[:20]
        frame = self.frame
        if frame.empty or not tokens:
            return frame.iloc[:0].copy()
        score = pd.Series(0, index=frame.index)
        for token in tokens:
            pattern = rf"\b{re.escape(token)}\b"
            score += frame["_lookup"].str.contains(pattern, regex=True).astype(int)
            score += 2 * frame["_names"].str.contains(pattern, regex=True).astype(int)
        hits = pd.Series(0, index=frame.index)
        for token in tokens:
            hits += frame["_lookup"].str.contains(rf"\b{re.escape(token)}\b", regex=True).astype(int)
        relevant = hits >= max(1, (len(tokens) + 1) // 2)
        matches = frame.loc[relevant].copy()
        matches["_rank"] = score[relevant]
        return matches.sort_values("_rank", ascending=False, kind="stable").head(3000).reset_index(drop=True)


@lru_cache(maxsize=4)
def catalogue_source(data_dir: str, database_url: str, csv_path: str):
    return CatalogueSource(open_vendor_store(data_dir, database_url), Path(csv_path))


class LiveCatalogueRAG(HospKartRAG):
    def __init__(self, source: CatalogueSource):
        # Reuse HospKart's query classification and offer comparison, without
        # downloading an embedding model or building a duplicate vector database.
        self.source = source
        self.db_path = source.store.db_path
        self.lightweight_mode = True
        self.embedding_model = self.client = self.collection = None
        self.max_text_field_chars = 1000
        self.products_df = pd.DataFrame()
        self.vendor_name_index = self.product_name_index = []
        self.query = None
        self.query_signature = None
        self.unpriced = pd.DataFrame()

    def prepare(self, query):
        self.source.refresh()
        generation = (self.source.signature, self.source.loaded_at)
        if self.query == query and self.query_signature == generation:
            return
        candidates = self.source.candidates(query)
        self.unpriced = candidates.loc[candidates["price"] <= 0].copy() if not candidates.empty else candidates
        self.vendor_name_index = self.product_name_index = []
        self._prepare_products_df(candidates.loc[candidates["price"] > 0].copy() if not candidates.empty else candidates)
        self.query, self.query_signature = query, (self.source.signature, self.source.loaded_at)

    def search_products(self, query: str, top_k: int = 5):
        self.prepare(query)
        if self.products_df is None or self.products_df.empty:
            return []
        return [{"id": str(row["id"]), "metadata": row.to_dict(), "document": str(row["product_name"]), "distance": 0.0}
                for _, row in self.products_df.head(top_k).iterrows()]

    def query_catalog(self, query: str, **kwargs):
        self.prepare(query)
        return super().query_catalog(query, **kwargs)

    def source_status(self):
        self.source.refresh()
        return {"vendor_records": self.source.vendor_count, "hospkart_records": self.source.hospkart_count,
                "retrieval": "live catalogue keyword search", "candidate_limit": 3000}


class MergedAgent(HospKartAgent):
    def handle_query(self, user_query, options=None):
        result = super().handle_query(user_query, options)
        if self._infer_intent(user_query) == "search":
            self.rag.prepare(user_query)
            missing = self.rag.unpriced.head(10)
            if not missing.empty:
                lines = ["Catalogue matches without a usable INR price (request vendor pricing before quoting):"]
                for _, row in missing.iterrows():
                    lines.append(f"- {row['product_name']} | {row['vendor_name']} | Price not recorded | Source: {row.get('source_file', '')}")
                result += "\n\n" + "\n".join(lines)
        selected = self.state.last_selected_product or {}
        if selected.get("source_file") and self._is_offer_relevant_to_query(selected, user_query):
            result += f"\n\nImported source: {selected['source_file']} (sheet {selected.get('source_sheet', '-')}, row {selected.get('source_row', '-')})."
        return result


_sessions = OrderedDict()


def get_session_agent(chat_id: str):
    with session_agents_lock:
        now = time.monotonic()
        for key in list(_sessions):
            if now - _sessions[key][0] > 3600:
                del _sessions[key]
        cached = _sessions.pop(chat_id, None)
        if cached:
            agent = cached[1]
        else:
            data_dir = vendor_setting("VENDOR_DATA_DIR", str(VENDOR_ROOT / "vendor_data"))
            source = catalogue_source(data_dir, vendor_setting("DATABASE_URL"),
                                      os.getenv("HOSPKART_PRODUCT_CSV", str(ROOT / "data" / "hospkart_products.csv")))
            agent = MergedAgent(rag=LiveCatalogueRAG(source), logs_dir=ROOT / "logs", exports_dir=ROOT / "exports" / chat_id)
            agent.ollama_enabled = os.getenv("HOSPKART_USE_OLLAMA", "false").lower() in {"1", "true", "yes"}
        _sessions[chat_id] = (now, agent)
        while len(_sessions) > 128:
            _sessions.popitem(last=False)
        return agent
