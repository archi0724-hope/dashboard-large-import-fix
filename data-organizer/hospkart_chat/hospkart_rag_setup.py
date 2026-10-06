from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv


REQUIRED_COLUMNS = {
    "id",
    "product_name",
    "category",
    "manufacturer",
    "vendor_name",
    "price",
    "specs",
    "warranty",
    "stock",
}


@dataclass
class Product:
    offer_id: str
    product_id: str
    vendor_id: str
    id: str
    product_name: str
    category: str
    manufacturer: str
    vendor_name: str
    price: float
    specs: str
    warranty: str
    stock: int
    regular_price: float = 0.0
    sale_price: float = 0.0
    vendor_status: str = "Unknown"
    product_status: str = "Unknown"
    product_code: str = ""
    product_sku: str = ""
    product_slug: str = ""
    summary: str = ""
    created_at: str = ""
    location: str = "-"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "product_id": self.product_id,
            "vendor_id": self.vendor_id,
            "id": self.id,
            "product_name": self.product_name,
            "category": self.category,
            "manufacturer": self.manufacturer,
            "vendor_name": self.vendor_name,
            "price": float(self.price),
            "specs": self.specs,
            "warranty": self.warranty,
            "stock": int(self.stock),
            "regular_price": float(self.regular_price),
            "sale_price": float(self.sale_price),
            "vendor_status": self.vendor_status,
            "product_status": self.product_status,
            "product_code": self.product_code,
            "product_sku": self.product_sku,
            "product_slug": self.product_slug,
            "summary": self.summary,
            "created_at": self.created_at,
            "location": self.location,
        }

    def to_document(self) -> str:
        return (
            f"Product: {self.product_name}. "
            f"Category: {self.category}. "
            f"Manufacturer: {self.manufacturer}. "
            f"Vendor: {self.vendor_name}. "
            f"Price: INR {self.price:.2f}. "
            f"Regular Price: INR {self.regular_price:.2f}. "
            f"Sale Price: INR {self.sale_price:.2f}. "
            f"Specs: {self.specs}. "
            f"Warranty: {self.warranty}. "
            f"Stock: {self.stock}."
            f" Location: {self.location}."
        )


class HospKartRAG:
    """RAG layer for HospKart product retrieval."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        collection_name: str = "hospkart_products",
        embedding_model: str | None = None,
    ) -> None:
        load_dotenv()
        resolved_db_path = db_path or os.getenv("VECTOR_DB_PATH", "data/chroma_db")
        resolved_embedding_model = embedding_model or os.getenv(
            "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        )
        self.lightweight_mode = str(os.getenv("LIGHTWEIGHT_MODE", "")).strip().lower() in {"1", "true", "yes"}
        self.db_path = Path(resolved_db_path)
        self.embedding_model = None
        self.client = None
        self.collection = None
        self.max_text_field_chars = int(os.getenv("MAX_TEXT_FIELD_CHARS", "280") or 280)
        if not self.lightweight_mode:
            import chromadb
            from sentence_transformers import SentenceTransformer

            self.db_path.mkdir(parents=True, exist_ok=True)
            self.embedding_model = SentenceTransformer(resolved_embedding_model)
            self.client = chromadb.PersistentClient(path=str(self.db_path.resolve()))
            self.collection = self.client.get_or_create_collection(name=collection_name)
        self.catalog_csv_path = Path(os.getenv("PRODUCT_CSV_PATH", "data/hospkart_products.csv"))
        self.products_df: pd.DataFrame | None = None
        self.vendor_name_index: list[str] = []
        self.product_name_index: list[str] = []
        self._load_products_df_if_exists()

    def _load_products_df_if_exists(self) -> None:
        if not self.catalog_csv_path.exists():
            self.products_df = None
            return
        base_cols = {
            "offer_id",
            "product_id",
            "vendor_id",
            "id",
            "product_name",
            "category",
            "manufacturer",
            "vendor_name",
            "price",
            "regular_price",
            "sale_price",
            "specs",
            "summary",
            "warranty",
            "stock",
            "vendor_status",
            "product_status",
            "product_code",
            "product_sku",
            "product_slug",
            "location",
        }
        df = pd.read_csv(self.catalog_csv_path, usecols=lambda c: c in base_cols, low_memory=False)
        self._prepare_products_df(df)

    def _prepare_products_df(self, df: pd.DataFrame) -> None:
        if "product_name" not in df.columns:
            self.products_df = None
            return
        df = df.fillna("")

        def _series_or_empty(column: str) -> pd.Series:
            if column in df.columns:
                return df[column].astype(str).str.lower()
            return pd.Series([""] * len(df))

        if "price" in df.columns:
            df["price"] = pd.to_numeric(df["price"], errors="coerce").fillna(0)
        if "regular_price" in df.columns:
            df["regular_price"] = pd.to_numeric(df["regular_price"], errors="coerce").fillna(0)
        if "sale_price" in df.columns:
            df["sale_price"] = pd.to_numeric(df["sale_price"], errors="coerce").fillna(0)
        if "stock" in df.columns:
            df["stock"] = pd.to_numeric(df["stock"], errors="coerce").fillna(0).astype(int)

        # Skip heavy embedding models when LIGHTWEIGHT_MODE is enabled.
        if self.lightweight_mode:
            for col in ("specs", "summary"):
                if col in df.columns:
                    df[col] = df[col].astype(str).str.slice(0, self.max_text_field_chars)

        df["_search_text"] = (
            df["product_name"].astype(str).str.lower()
            + " "
            + _series_or_empty("product_code")
            + " "
            + _series_or_empty("product_sku")
            + " "
            + _series_or_empty("product_slug")
            + " "
            + _series_or_empty("category")
            + " "
            + _series_or_empty("specs")
            + " "
            + _series_or_empty("summary")
        )
        self.vendor_name_index = sorted({str(v).strip() for v in df.get("vendor_name", pd.Series([])).tolist() if str(v).strip()})
        self.product_name_index = sorted(
            {str(v).strip() for v in df.get("product_name", pd.Series([])).tolist() if str(v).strip()}
        )
        self.products_df = df

    def load_product_data(self, csv_path: str | Path) -> list[Product]:
        csv_path = Path(csv_path)
        df = pd.read_csv(csv_path)

        missing_columns = REQUIRED_COLUMNS - set(df.columns)
        if missing_columns:
            raise ValueError(f"Missing required CSV columns: {sorted(missing_columns)}")

        products: list[Product] = []
        for _, row in df.iterrows():
            product = Product(
                offer_id=str(row.get("offer_id", row["id"])),
                product_id=str(row.get("product_id", row["id"])),
                vendor_id=str(row.get("vendor_id", "")),
                id=str(row["id"]),
                product_name=str(row["product_name"]).strip(),
                category=str(row["category"]).strip(),
                manufacturer=str(row["manufacturer"]).strip(),
                vendor_name=str(row["vendor_name"]).strip(),
                price=float(row["price"]),
                specs=str(row["specs"]).strip(),
                warranty=str(row["warranty"]).strip(),
                stock=int(row["stock"]),
                regular_price=float(row.get("regular_price", row["price"])),
                sale_price=float(row.get("sale_price", 0) or 0),
                vendor_status=str(row.get("vendor_status", "Unknown")).strip(),
                product_status=str(row.get("product_status", "Unknown")).strip(),
                product_code=str(row.get("product_code", "")).strip(),
                product_sku=str(row.get("product_sku", "")).strip(),
                product_slug=str(row.get("product_slug", "")).strip(),
                summary=str(row.get("summary", "")).strip(),
                created_at=str(row.get("created_at", "")).strip(),
                location=str(row.get("location", "-")).strip() or "-",
            )
            products.append(product)
        return products

    def ingest_products(self, products: list[Product], reset_collection: bool = False) -> int:
        if self.lightweight_mode or self.client is None or self.collection is None or self.embedding_model is None:
            self._load_products_df_if_exists()
            return len(products)

        if reset_collection:
            self.client.delete_collection(self.collection.name)
            self.collection = self.client.get_or_create_collection(name=self.collection.name)

        ids = [product.id for product in products]
        docs = [product.to_document() for product in products]
        metadatas = [product.to_metadata() for product in products]
        embeddings = self.embedding_model.encode(docs).tolist()

        self.collection.upsert(
            ids=ids,
            documents=docs,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        self._load_products_df_if_exists()
        return len(products)

    def search_products(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if self.lightweight_mode or self.collection is None or self.embedding_model is None:
            return []

        query_embedding = self.embedding_model.encode([query]).tolist()
        response = self.collection.query(
            query_embeddings=query_embedding,
            n_results=top_k,
            include=["metadatas", "documents", "distances"],
        )

        if not response["ids"] or not response["ids"][0]:
            return []

        results: list[dict[str, Any]] = []
        for idx in range(len(response["ids"][0])):
            results.append(
                {
                    "id": response["ids"][0][idx],
                    "metadata": response["metadatas"][0][idx],
                    "document": response["documents"][0][idx],
                    "distance": float(response["distances"][0][idx]),
                }
            )
        return results

    def export_search_results(self, query: str, output_file: str | Path, top_k: int = 5) -> Path:
        output_file = Path(output_file)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        results = self.search_products(query=query, top_k=top_k)
        payload = {"query": query, "top_k": top_k, "results": results}
        output_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return output_file

    def get_vendor_offers(self, query: str, max_products: int = 3, max_offers_per_product: int = 10) -> list[dict[str, Any]]:
        if self.products_df is None or self.products_df.empty:
            return []

        q = query.strip().lower()
        if not q:
            return []
        tokens = [token for token in q.split() if token]

        scored_rows: list[tuple[float, int]] = []
        for idx, row in self.products_df.iterrows():
            search_text = row["_search_text"]
            name = str(row["product_name"]).lower()
            score = SequenceMatcher(None, q, name).ratio()
            if q in search_text:
                score += 1.0
            if tokens:
                token_hits = sum(1 for token in tokens if token in search_text)
                score += token_hits / len(tokens)
            if score > 0.2:
                scored_rows.append((score, idx))

        if not scored_rows:
            return []

        scored_rows.sort(key=lambda item: item[0], reverse=True)
        candidate = self.products_df.loc[[idx for _, idx in scored_rows]].copy()
        product_names = list(candidate["product_name"].drop_duplicates().head(max_products))

        products_with_offers: list[dict[str, Any]] = []
        for product_name in product_names:
            product_name = str(product_name)
            offers_df = self.products_df[self.products_df["product_name"] == product_name].copy()
            if offers_df.empty:
                continue
            offers_df["price"] = pd.to_numeric(offers_df["price"], errors="coerce").fillna(0)
            offers_df = offers_df[offers_df["price"] > 0].sort_values(by=["price", "vendor_name"]).head(
                max_offers_per_product
            )
            offers: list[dict[str, Any]] = []
            for _, offer in offers_df.iterrows():
                offers.append(
                    {
                        "offer_id": str(offer.get("offer_id", "")),
                        "product_id": str(offer.get("product_id", "")),
                        "product_name": str(offer.get("product_name", "")),
                        "vendor_id": str(offer.get("vendor_id", "")),
                        "vendor_name": str(offer.get("vendor_name", "")),
                        "vendor_status": str(offer.get("vendor_status", "")),
                        "price": float(offer.get("price", 0)),
                        "regular_price": float(offer.get("regular_price", 0) or 0),
                        "sale_price": float(offer.get("sale_price", 0) or 0),
                        "category": str(offer.get("category", "")),
                        "specs": str(offer.get("specs", "")),
                        "warranty": str(offer.get("warranty", "")),
                        "stock": int(float(offer.get("stock", 0) or 0)),
                        "product_status": str(offer.get("product_status", "")),
                    }
                )
            if offers:
                products_with_offers.append({"product_name": product_name, "offers": offers})

        return products_with_offers

    def _query_tokens(self, query: str) -> list[str]:
        return [token for token in re.split(r"[^a-zA-Z0-9]+", query.lower()) if token]

    def _normalize_match_text(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    def _match_entities(self, query: str) -> dict[str, list[str]]:
        q = query.lower().strip()
        if not q:
            return {"vendors": [], "products": [], "exact_products": []}

        matched_vendors: list[str] = []
        for vendor in self.vendor_name_index:
            vendor_l = vendor.lower()
            if vendor_l and vendor_l in q:
                matched_vendors.append(vendor)
        if not matched_vendors:
            q_tokens = set(self._query_tokens(q))
            for vendor in self.vendor_name_index:
                vendor_tokens = set(self._query_tokens(vendor))
                if vendor_tokens and len(vendor_tokens.intersection(q_tokens)) >= 2:
                    matched_vendors.append(vendor)

        matched_products: list[str] = []
        exact_products: list[str] = []
        q_norm = self._normalize_match_text(q)
        product_stop_tokens = {
            "tell",
            "me",
            "about",
            "this",
            "product",
            "please",
            "show",
            "list",
            "details",
            "detail",
            "for",
            "the",
            "a",
            "an",
        }
        q_tokens_for_exact = {t for t in self._query_tokens(q) if t not in product_stop_tokens}
        for product in self.product_name_index:
            product_l = product.lower()
            product_norm = self._normalize_match_text(product_l)
            if product_norm and len(product_norm) >= 8 and product_norm in q_norm:
                exact_products.append(product)
            if q_tokens_for_exact:
                product_tokens = {t for t in self._query_tokens(product_l) if t not in product_stop_tokens}
                if product_tokens:
                    overlap = product_tokens.intersection(q_tokens_for_exact)
                    overlap_count = len(overlap)
                    query_coverage = overlap_count / max(len(q_tokens_for_exact), 1)
                    product_coverage = overlap_count / max(len(product_tokens), 1)
                    if overlap_count >= 2 and query_coverage >= 0.8 and product_coverage >= 0.45:
                        exact_products.append(product)
            if product_l and product_l in q:
                matched_products.append(product)
        if not matched_products:
            q_tokens = set(self._query_tokens(q))
            q_tokens = {token for token in q_tokens if token not in product_stop_tokens}
            for product in self.product_name_index:
                product_tokens = set(self._query_tokens(product))
                if not product_tokens or not q_tokens:
                    continue
                overlap = product_tokens.intersection(q_tokens)
                overlap_count = len(overlap)
                coverage = overlap_count / max(len(product_tokens), 1)
                # Drop weak matches that pull in unrelated categories.
                if overlap_count >= 2 and coverage >= 0.55:
                    matched_products.append(product)

        return {
            "vendors": matched_vendors[:5],
            "products": matched_products[:8],
            "exact_products": sorted(set(exact_products))[:3],
        }

    def _infer_query_mode(self, query: str, entities: dict[str, list[str]]) -> str:
        q = query.lower()
        has_vendor_kw = any(word in q for word in ("vendor", "supplier", "seller"))
        has_product_kw = any(word in q for word in ("product", "item", "model", "spec", "details"))
        if entities.get("exact_products"):
            return "exact_product_lookup"
        if entities["vendors"] and not entities["products"]:
            return "vendor_lookup"
        if entities["vendors"] and entities["products"]:
            return "vendor_product_lookup"
        if has_vendor_kw and entities["vendors"]:
            return "vendor_lookup"
        if has_product_kw or entities["products"]:
            return "product_lookup"
        return "product_lookup"

    def deep_research_products(
        self,
        query: str,
        max_products: int = 8,
        max_offers_per_product: int = 10,
        vendor_filter: set[str] | None = None,
        product_filter: set[str] | None = None,
        strict_keyword: bool = False,
    ) -> list[dict[str, Any]]:
        if self.products_df is None or self.products_df.empty:
            return []

        q = query.strip()
        if not q:
            return []

        tokens = self._query_tokens(q)
        stop_tokens = {"product", "item", "ki", "ke", "ka", "hai", "chahiye", "listing", "show", "details"}
        key_tokens = [token for token in tokens if token not in stop_tokens]
        vector_hits = self.search_products(query=q, top_k=40)
        vector_product_names = {
            (hit.get("metadata") or {}).get("product_name", "").strip().lower()
            for hit in vector_hits
            if (hit.get("metadata") or {}).get("product_name")
        }

        scored: list[tuple[float, int]] = []
        for idx, row in self.products_df.iterrows():
            name = str(row.get("product_name", "")).strip()
            if not name:
                continue
            vendor_name = str(row.get("vendor_name", "")).strip()
            if vendor_filter and vendor_name.lower() not in vendor_filter:
                continue
            if product_filter and name.lower() not in product_filter:
                continue
            name_l = name.lower()
            search_text = str(row.get("_search_text", ""))
            signal_tokens = key_tokens or tokens
            def _has_token(text: str, token: str) -> bool:
                return re.search(rf"\b{re.escape(token)}\b", text) is not None

            score = SequenceMatcher(None, q.lower(), name_l).ratio()
            if name_l in vector_product_names:
                score += 3.0
            if q.lower() in search_text:
                score += 1.5
            token_hits = sum(1 for token in signal_tokens if _has_token(search_text, token)) if signal_tokens else 0
            name_token_hits = sum(1 for token in signal_tokens if _has_token(name_l, token)) if signal_tokens else 0
            if signal_tokens:
                score += token_hits / max(len(signal_tokens), 1)
                score += 1.2 * (name_token_hits / max(len(signal_tokens), 1))

            # Prefer products whose names contain the query tokens.
            if key_tokens and token_hits == 0 and name_token_hits == 0:
                continue
            if key_tokens and name_token_hits == 0 and score < 3.2:
                continue
            if strict_keyword and key_tokens:
                # Strict mode requires name-token overlap.
                if name_token_hits == 0:
                    continue

            generic_name = any(
                phrase in name_l
                for phrase in ("untitled product", "simple variable product", "range variable product", "test product")
            )
            if generic_name and token_hits == 0:
                continue

            if score > 0.35:
                scored.append((score, idx))

        if not scored:
            return []

        scored.sort(key=lambda x: x[0], reverse=True)
        candidate_indices = [idx for _, idx in scored[:250]]
        candidate = self.products_df.loc[candidate_indices].copy()

        # Rank products by blended score and price signal.
        product_scores: list[tuple[float, str]] = []
        for product_name, group in candidate.groupby("product_name"):
            name = str(product_name)
            name_l = name.lower()
            base_score = max(
                (SequenceMatcher(None, q.lower(), str(row_name).lower()).ratio() for row_name in group["product_name"]),
                default=0.0,
            )
            vector_bonus = 1.5 if name_l in vector_product_names else 0.0
            offer_count_bonus = min(len(group), 10) * 0.05
            product_scores.append((base_score + vector_bonus + offer_count_bonus, name))

        product_scores.sort(key=lambda x: x[0], reverse=True)
        selected_names = [name for _, name in product_scores[:max_products]]

        output: list[dict[str, Any]] = []
        for product_name in selected_names:
            group_df = self.products_df[self.products_df["product_name"] == product_name].copy()
            if group_df.empty:
                continue

            group_df["price"] = pd.to_numeric(group_df["price"], errors="coerce").fillna(0)
            group_df = group_df[group_df["price"] > 0]
            if group_df.empty:
                continue

            group_df = group_df.sort_values(by=["price", "vendor_name"]).head(max_offers_per_product)
            offers: list[dict[str, Any]] = []
            for _, offer in group_df.iterrows():
                offers.append(
                    {
                        "offer_id": str(offer.get("offer_id", "")),
                        "product_id": str(offer.get("product_id", "")),
                        "vendor_id": str(offer.get("vendor_id", "")),
                        "product_name": str(offer.get("product_name", "")),
                        "category": str(offer.get("category", "")),
                        "manufacturer": str(offer.get("manufacturer", "")),
                        "vendor_name": str(offer.get("vendor_name", "")),
                        "vendor_status": str(offer.get("vendor_status", "")),
                        "product_status": str(offer.get("product_status", "")),
                        "price": float(offer.get("price", 0)),
                        "regular_price": float(offer.get("regular_price", 0) or 0),
                        "sale_price": float(offer.get("sale_price", 0) or 0),
                        "stock": int(float(offer.get("stock", 0) or 0)),
                        "warranty": str(offer.get("warranty", "NA")),
                        "specs": str(offer.get("specs", "")),
                        "summary": str(offer.get("summary", "")),
                        "location": str(offer.get("location", "-") or "-"),
                    }
                )

            if offers:
                prices = [item["price"] for item in offers]
                output.append(
                    {
                        "product_name": product_name,
                        "category": offers[0]["category"],
                        "best_price": min(prices),
                        "max_price": max(prices),
                        "vendor_count": len(offers),
                        "offers": offers,
                    }
                )

        return output

    def query_catalog(
        self,
        query: str,
        max_products: int = 8,
        max_offers_per_product: int = 10,
        strict_keyword: bool = False,
    ) -> dict[str, Any]:
        entities = self._match_entities(query)
        query_mode = self._infer_query_mode(query, entities)

        vendor_filter = {name.lower() for name in entities["vendors"]} if entities["vendors"] else None
        product_filter = {name.lower() for name in entities["products"]} if entities["products"] else None
        if entities.get("exact_products"):
            product_filter = {name.lower() for name in entities["exact_products"]}

        effective_max_products = max_products
        if query_mode == "exact_product_lookup":
            effective_max_products = 1
        elif not entities.get("products"):
            # Keyword/noise queries should still expose broader listing coverage.
            effective_max_products = max(max_products, 20)

        blocks = self.deep_research_products(
            query=query,
            max_products=effective_max_products,
            max_offers_per_product=max_offers_per_product,
            vendor_filter=vendor_filter,
            product_filter=product_filter,
            strict_keyword=strict_keyword,
        )

        # Vendor-specific queries should return more products from that vendor.
        if query_mode == "vendor_lookup" and not blocks and vendor_filter:
            blocks = self.deep_research_products(
                query=" ".join(entities["vendors"]),
                max_products=max_products,
                max_offers_per_product=max_offers_per_product,
                vendor_filter=vendor_filter,
                strict_keyword=strict_keyword,
            )

        return {
            "query_mode": query_mode,
            "matched_vendors": entities["vendors"],
            "matched_products": entities["products"],
            "exact_products": entities.get("exact_products", []),
            "blocks": blocks,
        }
