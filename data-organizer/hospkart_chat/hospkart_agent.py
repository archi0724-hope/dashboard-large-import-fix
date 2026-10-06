from __future__ import annotations

import csv
import json
import mimetypes
import os
import re
import statistics
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .hospkart_rag_setup import HospKartRAG


GST_RATE = 0.18
ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_LOGO_CANDIDATES = (
    ROOT_DIR / "hospkart-logo.jpg",
    ROOT_DIR / "hospkart-hero-fallback.jpg",
)


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass
class ConversationState:
    last_search_results: list[dict[str, Any]] = field(default_factory=list)
    last_vendor_offers: list[dict[str, Any]] = field(default_factory=list)
    last_selected_product: dict[str, Any] | None = None
    last_quantity: int | None = None
    last_query_mode: str | None = None
    invoice_preferences: dict[str, Any] = field(default_factory=dict)
    pending_invoice_flow: dict[str, Any] | None = None


class HospKartAgent:
    """Agentic layer that routes search and quotation requests."""

    def __init__(
        self,
        rag: HospKartRAG | None = None,
        logs_dir: str | Path = "logs",
        exports_dir: str | Path = "exports",
        model_name: str | None = None,
    ) -> None:
        self.rag = rag or HospKartRAG()
        self.logs_dir = Path(logs_dir)
        self.exports_dir = Path(exports_dir)
        self.templates_dir = ROOT_DIR / "templates" / "invoices"
        self.model_name = model_name or os.getenv("OLLAMA_MODEL", "llama3.2")
        self.ollama_enabled = os.getenv("USE_OLLAMA", "true").lower() in {"1", "true", "yes"}
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        self.logo_path = self._resolve_logo_path()
        self.template_env = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.state = ConversationState()
        self.interaction_file = self.logs_dir / "interactions.csv"
        self._ensure_interaction_log()

    @staticmethod
    def _stock_label(offer) -> str:
        if str(offer.get("offer_id", "")).startswith("vendor:"):
            return "Not recorded"
        return str(int(float(offer.get("stock", 0) or 0)))

    def _ensure_interaction_log(self) -> None:
        if self.interaction_file.exists():
            return
        with self.interaction_file.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerow(["timestamp", "user_input", "intent", "agent_response"])

    def _resolve_logo_path(self) -> Path | None:
        for candidate in DEFAULT_LOGO_CANDIDATES:
            if candidate.exists():
                return candidate
        return None

    def _logo_data_uri(self) -> str | None:
        if not self.logo_path or not self.logo_path.exists():
            return None
        binary = self.logo_path.read_bytes()
        guessed_type = mimetypes.guess_type(str(self.logo_path))[0] or "image/jpeg"
        encoded = b64encode(binary).decode("ascii")
        return f"data:{guessed_type};base64,{encoded}"

    def _infer_intent(self, user_query: str) -> str:
        lowered = user_query.lower()
        search_words = (
            "find",
            "search",
            "show",
            "list",
            "about",
            "product",
            "detail",
            "details",
            "alternative",
            "inquiry",
            "enquiry",
            "pricing",
            "vendor",
            "compare",
            "catalog",
            "listing",
        )

        if self._looks_like_quote_request(lowered):
            return "quotation"
        if any(word in lowered for word in search_words):
            return "search"
        # If user gives quantity/discount after selecting a product, treat as quote follow-up.
        if self.state.last_selected_product and (
            self._extract_quantity(user_query) is not None or self._extract_discount(user_query) > 0
        ):
            return "quotation"
        return "search"

    def _looks_like_quote_request(self, lowered_query: str) -> bool:
        query = lowered_query.strip()
        if not query:
            return False

        explicit_patterns = (
            r"\bquote\b",
            r"\bquotation\b",
            r"\brfq\b",
            r"\binvoice\b",
            r"\bpurchase\s+invoice\b",
            r"\bpurchase\s+order\b",
            r"\btax\s+invoice\b",
            r"\bcreate\s+quotation\b",
            r"\bcreate\s+invoice\b",
            r"\bgenerate\s+quotation\b",
            r"\bgenerate\s+invoice\b",
            r"\bpo\b",
        )
        if any(re.search(pattern, query) for pattern in explicit_patterns):
            return True

        # Quantity + pricing modifiers usually indicate a quote request.
        has_quantity = re.search(r"\b\d+\s*(units?|unit|qty|quantity|pieces?|pcs?)\b", query) is not None
        has_quote_modifier = any(
            token in query
            for token in (
                "discount",
                "gst",
                "final amount",
                "subtotal",
            )
        )
        return bool(has_quantity and has_quote_modifier)

    def _extract_quantity(self, user_query: str) -> int | None:
        lowered = user_query.lower()
        # Quantity tokens that are part of a product name are ignored.
        explicit = re.search(r"\b(\d+)\s*(units?|unit|qty|quantity|pieces?|pcs?)\b", lowered)
        if explicit:
            return int(explicit.group(1))

        contextual = re.search(r"\b(?:for|of)\s+(\d+)\b", lowered)
        if contextual:
            return int(contextual.group(1))

        return None

    def _extract_discount(self, user_query: str) -> float:
        discount_match = re.search(r"(\d+(?:\.\d+)?)\s*%", user_query)
        if not discount_match:
            return 0.0
        return float(discount_match.group(1)) / 100.0

    def _normalize_invoice_field_key(self, raw_key: str) -> str | None:
        key = re.sub(r"[^a-z0-9]+", "_", raw_key.lower()).strip("_")
        aliases = {
            "company_name": "company_name",
            "seller_name": "company_name",
            "bill_from_name": "company_name",
            "company_address": "company_address",
            "seller_address": "company_address",
            "company_email": "company_email",
            "company_phone": "company_phone",
            "company_pan": "company_pan",
            "company_gstin": "company_gstin",
            "company_tagline": "company_tagline",
            "tagline": "company_tagline",
            "company_state_name": "company_state_name",
            "company_state_code": "company_state_code",
            "bill_to": "bill_to_name",
            "bill_to_name": "bill_to_name",
            "customer_name": "bill_to_name",
            "bill_to_address": "bill_to_address",
            "bill_to_state": "bill_to_state_name",
            "bill_to_state_name": "bill_to_state_name",
            "bill_to_state_code": "bill_to_state_code",
            "state_code": "bill_to_state_code",
            "bill_to_gstin": "bill_to_gstin",
            "ship_to": "ship_to",
            "place_of_supply": "place_of_supply",
            "vendor_name": "vendor_name",
            "quantity": "quantity",
            "qty": "quantity",
            "discount": "discount_percent",
            "discount_percent": "discount_percent",
            "discount_percentage": "discount_percent",
            "gst_rate": "gst_rate_percent",
            "gst_percent": "gst_rate_percent",
            "gst_rate_percent": "gst_rate_percent",
            "hsn": "hsn_code",
            "hsn_sac": "hsn_code",
            "hsn_code": "hsn_code",
            "delivery_charges": "delivery_charges",
            "document_type": "document_type",
            "invoice_type": "document_type",
            "bank_name": "bank_name",
            "account_name": "account_name",
            "account_no": "account_no",
            "account_number": "account_no",
            "ifsc": "ifsc",
            "terms": "terms",
            "note": "note",
            "invoice_no": "invoice_no",
            "invoice_number": "invoice_no",
            "invoice_date": "invoice_date",
            "e_way_bill": "e_way_bill_no",
            "e_way_bill_no": "e_way_bill_no",
            "delivery_note": "delivery_note",
            "reference_no": "reference_no",
            "buyers_order_no": "buyers_order_no",
            "dispatch_doc_no": "dispatch_doc_no",
            "dispatched_through": "dispatched_through",
            "destination": "destination",
            "mode_terms_of_payment": "mode_terms_of_payment",
            "other_references": "other_references",
            "delivery_note_date": "delivery_note_date",
            "declaration": "declaration",
            "tax_rate_label": "tax_rate_label",
        }
        return aliases.get(key)

    def _extract_invoice_field_updates(self, query: str) -> tuple[dict[str, Any], bool]:
        lowered = query.lower()
        should_reset = "reset invoice fields" in lowered or "reset template fields" in lowered
        updates: dict[str, Any] = {}
        segments = [part.strip() for part in re.split(r"[;\n]+", query) if ":" in part]
        for segment in segments:
            key_part, value_part = segment.split(":", 1)
            normalized_key = self._normalize_invoice_field_key(key_part.strip())
            value = value_part.strip()
            if not normalized_key or not value:
                continue
            if normalized_key == "terms":
                terms = [item.strip(" -") for item in re.split(r"[|]+", value) if item.strip()]
                if terms:
                    updates["terms"] = terms
                continue
            updates[normalized_key] = value
        return updates, should_reset

    def _strip_invoice_control_segments(self, query: str) -> str:
        clean_segments: list[str] = []
        for segment in [part.strip() for part in re.split(r"[;\n]+", query) if part.strip()]:
            if ":" not in segment:
                clean_segments.append(segment)
                continue
            key_part = segment.split(":", 1)[0].strip()
            if self._normalize_invoice_field_key(key_part):
                continue
            clean_segments.append(segment)
        return " ".join(clean_segments).strip() or query

    def _extract_product_hint_from_invoice_query(self, query: str) -> str:
        hint = str(query or "").strip()
        hint = re.sub(
            r"\b(?:make|create|generate)\s+(?:a\s+)?(?:tax|purchase|gst)?\s*invoice\b",
            " ",
            hint,
            flags=re.IGNORECASE,
        )
        hint = re.sub(r"\b(?:for|of)\b", " ", hint, flags=re.IGNORECASE)
        hint = re.sub(r"\b(?:quantity|qty)\s*\d+\b", " ", hint, flags=re.IGNORECASE)
        hint = re.sub(r"\b\d+\s*(?:units?|pcs?|pieces?)\b", " ", hint, flags=re.IGNORECASE)
        hint = re.sub(r"\s+", " ", hint).strip(" -,:;.")
        return hint

    def _has_meaningful_product_hint(self, query: str) -> bool:
        hint = self._extract_product_hint_from_invoice_query(query)
        tokens = [t for t in re.findall(r"[a-z0-9]+", hint.lower()) if len(t) >= 2]
        stop_tokens = {
            "this",
            "product",
            "invoice",
            "tax",
            "purchase",
            "gst",
            "make",
            "create",
            "generate",
            "for",
            "of",
        }
        filtered = [t for t in tokens if t not in stop_tokens]
        return len(filtered) >= 2

    def _smalltalk_reply(self, user_query: str) -> str | None:
        lowered = user_query.strip().lower()
        if not lowered:
            return None
        greetings = {"hi", "hello", "hey", "hii", "namaste", "good morning", "good evening"}
        if lowered in greetings:
            return (
                "Namaste! Main BD Helper QuoteSarthi AI hoon. "
                "Aap product search, vendor compare, ya invoice flow kisi bhi format me bol sakte ho."
            )
        if any(token in lowered for token in ("thanks", "thank you", "thx", "shukriya")):
            return "Always welcome. Next batao - product chahiye, vendor listing, ya tax/purchase invoice?"
        if any(token in lowered for token in ("help", "kya kar", "kaise", "samjha", "guide")):
            return (
                "Main smartly assist karunga. Example:\n"
                "- `medical cotton rolls vendor listing`\n"
                "- `medical cotton rolls full details`\n"
                "- `make a tax invoice of medical cotton rolls`\n"
                "Aap normal language me bhi bhejo, main context samajh ke reply dunga."
            )
        if len(re.findall(r"[a-z0-9]+", lowered)) <= 2 and lowered in {"ok", "hmm", "h", "test"}:
            return "Samjha. Thoda specific bolo - kis product ya kis invoice pe kaam karna hai?"
        return None

    def _should_start_invoice_workflow(self, query: str) -> bool:
        lowered = query.lower()
        # Invoice requests always use the guided field collection flow.
        has_invoice_intent = (
            re.search(r"\b(?:make|create|generate)\s+(?:a\s+)?tax\s+invoice\b", lowered) is not None
            or re.search(r"\b(?:make|create|generate)\s+(?:a\s+)?purchase\s+invoice\b", lowered) is not None
            or re.search(r"\btax\s+invoice\b", lowered) is not None
            or re.search(r"\bpurchase\s+invoice\b", lowered) is not None
        )
        return bool(has_invoice_intent)

    def _invoice_workflow_steps(self) -> list[tuple[str, str]]:
        return [
            ("quantity", "Quantity kitni chahiye? (sirf number bhejo)"),
            ("discount_percent", "Discount kitna % rakhna hai? (nahi ho to 0 bhejo)"),
            ("bill_to_name", "Bill To ka naam batao (hospital/customer)."),
            ("bill_to_state_name", "Bill To state ka naam?"),
            ("bill_to_state_code", "Bill To state code? (example: 08)"),
            ("bill_to_gstin", "Bill To GSTIN? (nahi hai to NA likho)"),
            ("ship_to", "Ship To address/location batao."),
            ("place_of_supply", "Place of supply kya rakhna hai?"),
            ("bank_name", "Bank ka naam?"),
            ("account_name", "Account name?"),
            ("account_no", "Account number?"),
            ("ifsc", "IFSC code?"),
            ("terms", "Terms bhejo, `|` se alag-alag (example: 100% advance|Delivery in 7 days)."),
            ("note", "Koi extra note/instruction? (nahi ho to NA likho)"),
        ]

    def _next_invoice_step(self, flow_data: dict[str, Any]) -> tuple[str, str] | None:
        for key, prompt in self._invoice_workflow_steps():
            value = flow_data.get(key)
            if value is None:
                return key, prompt
            if isinstance(value, str) and not value.strip():
                return key, prompt
            if isinstance(value, list) and not value:
                return key, prompt
        return None

    def _parse_workflow_updates(self, query: str) -> dict[str, Any]:
        updates, _ = self._extract_invoice_field_updates(query)
        merged = dict(updates)
        cleaned = self._strip_invoice_control_segments(query)
        qty = self._extract_quantity(cleaned)
        if qty is not None:
            merged["quantity"] = str(qty)
        pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", cleaned)
        if pct_match:
            merged["discount_percent"] = pct_match.group(1)
        if "tax invoice" in query.lower():
            merged["document_type"] = "tax_invoice"
        if "purchase invoice" in query.lower():
            merged["document_type"] = "purchase_invoice"
        return merged

    def _assign_workflow_value(self, flow_data: dict[str, Any], key: str, value: Any) -> None:
        if key == "terms":
            if isinstance(value, list):
                flow_data[key] = [str(item).strip() for item in value if str(item).strip()]
                return
            terms = [item.strip(" -") for item in re.split(r"[|]+", str(value)) if item.strip()]
            flow_data[key] = terms
            return
        flow_data[key] = str(value).strip()

    def _start_invoice_workflow(self, user_query: str) -> str:
        cleaned_query = self._strip_invoice_control_segments(user_query)
        selected = self.state.last_selected_product if "this product" in user_query.lower() else None
        if not selected and not self._has_meaningful_product_hint(cleaned_query):
            return (
                "Invoice banana hai, perfect. Pehle product naam clear bhejo "
                "(example: `make a tax invoice of medical cotton rolls`)."
            )
        if not selected:
            selected = self._select_product(self._extract_product_hint_from_invoice_query(cleaned_query))
        if not selected:
            return "Pehle product select karo. Product naam ke saath invoice request bhejo, phir main step-by-step chalunga."

        flow: dict[str, Any] = {
            "document_type": self._infer_invoice_type(user_query),
            "selected_product": selected,
            "data": {},
        }
        parsed_updates = self._parse_workflow_updates(user_query)
        for key, value in parsed_updates.items():
            if key == "document_type":
                flow["document_type"] = value
                continue
            self._assign_workflow_value(flow["data"], key, value)

        self.state.pending_invoice_flow = flow
        next_step = self._next_invoice_step(flow["data"])
        if not next_step:
            return self._finalize_invoice_workflow("workflow-auto-complete")
        flow["awaiting_field"] = next_step[0]
        return (
            f"Invoice workflow start ho gaya for `{selected.get('product_name', 'selected product')}` "
            f"({ 'Tax Invoice' if flow['document_type'] == 'tax_invoice' else 'Purchase Invoice' }).\n"
            f"{next_step[1]}"
        )

    def _continue_invoice_workflow(self, user_query: str) -> str:
        flow = self.state.pending_invoice_flow
        if not flow:
            return "Abhi koi active invoice workflow nahi chal raha."

        awaiting = flow.get("awaiting_field")
        if awaiting and ":" not in user_query:
            self._assign_workflow_value(flow["data"], awaiting, user_query)
        else:
            updates = self._parse_workflow_updates(user_query)
            if updates:
                for key, value in updates.items():
                    if key == "document_type":
                        flow["document_type"] = value
                        continue
                    self._assign_workflow_value(flow["data"], key, value)
            elif awaiting:
                self._assign_workflow_value(flow["data"], awaiting, user_query)

        next_step = self._next_invoice_step(flow["data"])
        if next_step:
            flow["awaiting_field"] = next_step[0]
            return f"Theek hai. {next_step[1]}"
        return self._finalize_invoice_workflow(user_query)

    def _finalize_invoice_workflow(self, source_query: str) -> str:
        flow = self.state.pending_invoice_flow
        if not flow:
            return "Invoice workflow session not found."

        data = flow["data"]
        selected = flow["selected_product"]
        try:
            quantity = int(float(str(data.get("quantity", "1")).strip()))
        except Exception:
            quantity = 1
        try:
            discount_percent = float(str(data.get("discount_percent", "0")).strip())
        except Exception:
            discount_percent = 0.0

        preferences = {
            "bill_to_name": str(data.get("bill_to_name", "")),
            "bill_to_state_name": str(data.get("bill_to_state_name", "")),
            "bill_to_state_code": str(data.get("bill_to_state_code", "")),
            "bill_to_gstin": str(data.get("bill_to_gstin", "")),
            "ship_to": str(data.get("ship_to", "")),
            "place_of_supply": str(data.get("place_of_supply", "")),
            "bank_name": str(data.get("bank_name", "")),
            "account_name": str(data.get("account_name", "")),
            "account_no": str(data.get("account_no", "")),
            "ifsc": str(data.get("ifsc", "")),
            "terms": data.get("terms", []),
            "note": str(data.get("note", "")),
        }
        self.state.invoice_preferences = preferences
        self.state.pending_invoice_flow = None

        doc_command = "create tax invoice" if flow.get("document_type") == "tax_invoice" else "create purchase invoice"
        product_name = str(selected.get("product_name") or "selected product")
        synthetic_query = f"{doc_command} for {product_name} {quantity} units with {discount_percent}% discount"
        quote = self.generate_quotation(synthetic_query, selected_override=selected)
        json_file = self.export_quotation_json(quote)
        pdf_file = self.export_quotation_pdf(quote)
        response = self._render_quotation_text_template(quote=quote, json_file=json_file, pdf_file=pdf_file)
        self._log_interaction(source_query, "quotation", response)
        return response

    def _infer_invoice_type(self, user_query: str) -> str:
        lowered = user_query.lower()
        if any(token in lowered for token in ("purchase invoice", "purchase order")):
            return "purchase_invoice"
        if any(token in lowered for token in ("tax invoice", "gst invoice")):
            return "tax_invoice"
        return "tax_invoice"

    def _select_product(self, query: str) -> dict[str, Any] | None:
        def pick_relevant(offers: list[dict[str, Any]]) -> dict[str, Any] | None:
            for offer in offers:
                if self._is_offer_relevant_to_query(offer=offer, query=query):
                    return offer
            return offers[0] if offers else None

        # Resolve the product from the current query first.
        catalog_response = self.rag.query_catalog(query=query, max_products=3, max_offers_per_product=1)
        blocks = catalog_response.get("blocks", [])
        if blocks and isinstance(blocks, list):
            offers = blocks[0].get("offers", [])
            selected_offer = pick_relevant(offers)
            if selected_offer:
                return selected_offer

        if self.state.last_vendor_offers:
            first_group = self.state.last_vendor_offers[0]
            offers = first_group.get("offers", [])
            selected_offer = pick_relevant(offers)
            if selected_offer:
                return selected_offer
        if self.state.last_search_results:
            candidate = self.state.last_search_results[0]["metadata"]
            if self._is_offer_relevant_to_query(offer=candidate, query=query):
                return candidate
        results = self.rag.search_products(query=query, top_k=1)
        if not results:
            return None
        candidate = results[0]["metadata"]
        if self._is_offer_relevant_to_query(offer=candidate, query=query):
            return candidate
        return None

    def _is_offer_relevant_to_query(self, offer: dict[str, Any], query: str) -> bool:
        product_name = str(offer.get("product_name", "")).strip().lower()
        if not product_name:
            return False

        query_tokens = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) >= 2}
        stop_tokens = {
            "show",
            "list",
            "listing",
            "detail",
            "details",
            "product",
            "vendor",
            "best",
            "offer",
            "with",
            "for",
            "and",
            "the",
            "per",
            "piece",
            "rate",
            "create",
            "quotation",
            "invoice",
            "purchase",
            "tax",
            "quantity",
            "qty",
            "units",
        }
        query_tokens = {t for t in query_tokens if t not in stop_tokens}
        if not query_tokens:
            return True

        product_tokens = {t for t in re.findall(r"[a-z0-9]+", product_name) if len(t) >= 2}
        overlap = query_tokens.intersection(product_tokens)
        return len(overlap) >= max(1, min(2, len(query_tokens)))

    def _log_interaction(self, user_input: str, intent: str, agent_response: str) -> None:
        with self.interaction_file.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerow([datetime.now().isoformat(), user_input, intent, agent_response])

    def _format_with_ollama(self, query: str, mode: str, payload: dict[str, Any]) -> str | None:
        if not self.ollama_enabled:
            return None

        try:
            import ollama

            prompt = (
                "You are HospKart BD assistant.\n"
                f"Mode: {mode}\n"
                f"User query: {query}\n"
                "Structured data:\n"
                f"{json.dumps(payload, indent=2)}\n\n"
                "Return concise business-friendly response in plain text. "
                "Show INR values clearly and include next action for BD team."
            )
            timeout_sec = float(os.getenv("OLLAMA_TIMEOUT_SEC", "12"))
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(ollama.generate, model=self.model_name, prompt=prompt)
            try:
                response = future.result(timeout=timeout_sec)
            except FutureTimeoutError:
                # Fail over if the local model call exceeds the timeout.
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                return None
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            text = (response or {}).get("response", "").strip()
            if not text:
                return None
            # Drop model output that is not grounded in catalog data.
            if not self._is_response_grounded(text=text, payload=payload):
                return None
            # Search replies must not look like quotes or invoices.
            text_l = text.lower()
            if mode == "search" and (
                text_l.startswith("quotation generated")
                or "json export:" in text_l
                or "pdf export:" in text_l
                or "document type:" in text_l
            ):
                return None
            return text
        except Exception:
            return None

    def _extract_reference_entities(self, payload: dict[str, Any]) -> set[str]:
        refs: set[str] = set()

        def add(value: Any) -> None:
            if not value:
                return
            raw = str(value).strip()
            if not raw:
                return
            if len(raw) < 3:
                return
            refs.add(raw.lower())

        quote = payload.get("quotation")
        if isinstance(quote, dict):
            add(quote.get("product_name"))
            add(quote.get("vendor_name"))
            add(quote.get("category"))

        for key in ("deep_research", "vendor_offers", "results"):
            items = payload.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    metadata = item.get("metadata", item)
                    if isinstance(metadata, dict):
                        add(metadata.get("product_name"))
                        add(metadata.get("vendor_name"))
                        add(metadata.get("category"))
                    offers = item.get("offers")
                    if isinstance(offers, list):
                        for offer in offers:
                            if isinstance(offer, dict):
                                add(offer.get("product_name"))
                                add(offer.get("vendor_name"))
                                add(offer.get("category"))
        return refs

    def _is_response_grounded(self, text: str, payload: dict[str, Any]) -> bool:
        refs = self._extract_reference_entities(payload)
        if not refs:
            return True
        text_l = text.lower()
        # At least one known entity must appear in model output.
        return any(ref in text_l for ref in refs)

    def _format_bd_listing(self, query: str, research: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        lines.append("By product - vendors and pricing (sorted low to high)")
        lines.append(f"Query: {query}")
        lines.append("")

        for block in research:
            product = block["product_name"]
            category = block.get("category", "General")
            lines.append(
                f"{category} > {product} - {block['vendor_count']} vendor(s) | "
                f"Price range INR {block['best_price']:.2f} to INR {block['max_price']:.2f}"
            )
            lines.append("S.No | Vendor Name | Per Piece Price | Remarks | Location")
            for idx, offer in enumerate(block["offers"], start=1):
                remarks = offer.get("summary") or offer.get("specs") or "NA"
                remarks = re.sub(r"\s+", " ", remarks).strip()
                if len(remarks) > 90:
                    remarks = remarks[:87] + "..."
                lines.append(
                    f"{idx} | {offer.get('vendor_name', 'Unknown')} | INR {offer.get('price', 0):.2f}/Piece | "
                    f"{remarks} | {offer.get('location', '-')}"
                )
            top_offer = block["offers"][0]
            lines.append(
                f"Best Offer Detail: Vendor={top_offer.get('vendor_name', 'Unknown')}, "
                f"Stock={self._stock_label(top_offer)}, Warranty={top_offer.get('warranty', 'NA')}, "
                f"Status={top_offer.get('vendor_status', 'Unknown')}/{top_offer.get('product_status', 'Unknown')}"
            )
            lines.append("")
        return "\n".join(lines).strip()

    def _detect_product_action(self, query: str) -> str:
        q = query.lower()
        if "best pricing analysis" in q or "mathematical comparison" in q:
            return "best_pricing_math"
        if "vendor coverage list" in q or "all vendors" in q:
            return "vendor_coverage"
        if "full details with all fields" in q:
            return "product_details"
        if "vendor listing sorted by price" in q:
            return "vendor_listing"
        return "default"

    def _format_best_pricing_math(self, query: str, research: list[dict[str, Any]]) -> str:
        if not research:
            return "Best Pricing Analysis\nNo matching products found."

        block = research[0]
        product = str(block.get("product_name", "Unknown Product"))
        offers = block.get("offers", [])
        if not offers:
            return "Best Pricing Analysis\nNo vendor pricing data available."

        prices = [float(offer.get("price", 0) or 0) for offer in offers if float(offer.get("price", 0) or 0) > 0]
        if not prices:
            return "Best Pricing Analysis\nNo valid price points available."

        min_price = min(prices)
        max_price = max(prices)
        avg_price = statistics.mean(prices)
        median_price = statistics.median(prices)
        savings_vs_max = max_price - min_price
        savings_vs_avg = avg_price - min_price

        lines: list[str] = [
            "Best Pricing Analysis (Mathematical Comparison)",
            f"Query: {query}",
            f"Product: {product}",
            f"Vendors Compared: {len(prices)}",
            f"Lowest Price: INR {min_price:.2f}",
            f"Average Price: INR {avg_price:.2f}",
            f"Median Price: INR {median_price:.2f}",
            f"Highest Price: INR {max_price:.2f}",
            f"Savings vs Highest: INR {savings_vs_max:.2f}",
            f"Savings vs Average: INR {savings_vs_avg:.2f}",
            "",
            "Rank | Vendor Name | Price | Delta vs Lowest | Delta vs Average | Stock | Location",
        ]
        for idx, offer in enumerate(offers, start=1):
            price = float(offer.get("price", 0) or 0)
            lines.append(
                f"{idx} | {offer.get('vendor_name', 'Unknown')} | INR {price:.2f} | "
                f"INR {max(price - min_price, 0):.2f} | INR {price - avg_price:+.2f} | "
                f"{self._stock_label(offer)} | {offer.get('location', '-')}"
            )
        lines.append("")
        lines.append(
            f"Best Priced Vendor: {offers[0].get('vendor_name', 'Unknown')} at INR {float(offers[0].get('price', 0) or 0):.2f}"
        )
        return "\n".join(lines).strip()

    def _format_vendor_coverage(self, query: str, research: list[dict[str, Any]]) -> str:
        if not research:
            return "Vendor Coverage Map\nNo matching products found."

        block = research[0]
        product = str(block.get("product_name", "Unknown Product"))
        offers = block.get("offers", [])
        if not offers:
            return "Vendor Coverage Map\nNo vendor coverage available."

        vendor_names = [str(offer.get("vendor_name", "Unknown")).strip() for offer in offers if offer.get("vendor_name")]
        unique_vendor_names = sorted({name for name in vendor_names if name})
        lines: list[str] = [
            "Vendor Coverage Map",
            f"Query: {query}",
            f"Product: {product}",
            f"Total Vendors Catering: {len(unique_vendor_names)}",
            f"Vendor Names: {', '.join(unique_vendor_names)}",
            "",
            "S.No | Vendor Name | Price | Vendor Status | Product Status | Stock | Location",
        ]
        for idx, offer in enumerate(offers, start=1):
            lines.append(
                f"{idx} | {offer.get('vendor_name', 'Unknown')} | INR {float(offer.get('price', 0) or 0):.2f} | "
                f"{offer.get('vendor_status', 'Unknown')} | {offer.get('product_status', 'Unknown')} | "
                f"{self._stock_label(offer)} | {offer.get('location', '-')}"
            )
        return "\n".join(lines).strip()

    def _format_full_product_details(self, query: str, research: list[dict[str, Any]]) -> str:
        if not research:
            return "No matching products found."
        block = research[0]
        product = block.get("product_name", "Unknown Product")
        category = block.get("category", "General")
        lines: list[str] = [
            "Exact Product Match - Full Details",
            f"Query: {query}",
            f"Product: {product}",
            f"Category: {category}",
            f"Total Vendor Offers: {block.get('vendor_count', 0)}",
            f"Price Range: INR {block.get('best_price', 0):.2f} to INR {block.get('max_price', 0):.2f}",
            "",
        ]

        for idx, offer in enumerate(block.get("offers", []), start=1):
            lines.extend(
                [
                    f"Offer #{idx}",
                    f"offer_id: {offer.get('offer_id', '')}",
                    f"product_id: {offer.get('product_id', '')}",
                    f"vendor_id: {offer.get('vendor_id', '')}",
                    f"product_name: {offer.get('product_name', '')}",
                    f"category: {offer.get('category', '')}",
                    f"manufacturer: {offer.get('manufacturer', '')}",
                    f"vendor_name: {offer.get('vendor_name', '')}",
                    f"vendor_status: {offer.get('vendor_status', '')}",
                    f"product_status: {offer.get('product_status', '')}",
                    f"price: INR {float(offer.get('price', 0) or 0):.2f}",
                    f"regular_price: INR {float(offer.get('regular_price', 0) or 0):.2f}",
                    f"sale_price: INR {float(offer.get('sale_price', 0) or 0):.2f}",
                    f"stock: {self._stock_label(offer)}",
                    f"warranty: {offer.get('warranty', 'NA')}",
                    f"location: {offer.get('location', '-')}",
                    f"specs: {offer.get('specs', '')}",
                    f"summary: {offer.get('summary', '')}",
                    "",
                ]
            )
        return "\n".join(lines).strip()

    def _format_intent_header(
        self,
        query_mode: str,
        matched_vendors: list[str],
        matched_products: list[str],
        exact_products: list[str] | None = None,
    ) -> str:
        intent_label = {
            "vendor_lookup": "Vendor Inquiry",
            "vendor_product_lookup": "Vendor + Product Inquiry",
            "product_lookup": "Product Inquiry",
            "exact_product_lookup": "Exact Product Inquiry",
        }.get(query_mode, "Product Inquiry")
        parts = [f"Intent Understood: {intent_label}"]
        if exact_products:
            parts.append(f"Exact Match Product(s): {', '.join(exact_products)}")
        if matched_vendors:
            parts.append(f"Matched Vendor(s): {', '.join(matched_vendors)}")
        if matched_products:
            parts.append(f"Matched Product(s): {', '.join(matched_products[:5])}")
        return "\n".join(parts)

    def search(self, query: str, top_k: int = 5, options: dict[str, Any] | None = None) -> str:
        options = options or {}
        max_products = int(options.get("max_products", 8) or 8)
        max_offers_per_product = int(options.get("max_offers_per_product", 10) or 10)
        strict_keyword = bool(options.get("strict_keyword", False))
        if not strict_keyword and self._should_auto_enable_strict_keyword(query):
            strict_keyword = True
        max_products = max(1, min(max_products, 30))
        max_offers_per_product = max(1, min(max_offers_per_product, 25))

        results = self.rag.search_products(query=query, top_k=top_k)
        catalog_response = self.rag.query_catalog(
            query=query,
            max_products=max_products,
            max_offers_per_product=max_offers_per_product,
            strict_keyword=strict_keyword,
        )
        vendor_offers = catalog_response["blocks"]
        self.state.last_search_results = results
        self.state.last_vendor_offers = vendor_offers
        self.state.last_query_mode = catalog_response["query_mode"]
        if results:
            self.state.last_selected_product = results[0]["metadata"]

        if not results and not vendor_offers:
            response = "No matching products found."
            self._log_interaction(query, "search", response)
            return response

        intent_header = self._format_intent_header(
            query_mode=catalog_response["query_mode"],
            matched_vendors=catalog_response["matched_vendors"],
            matched_products=catalog_response["matched_products"],
            exact_products=catalog_response.get("exact_products"),
        )
        action = self._detect_product_action(query)
        if action == "best_pricing_math":
            body = self._format_best_pricing_math(query=query, research=vendor_offers)
        elif action == "vendor_coverage":
            body = self._format_vendor_coverage(query=query, research=vendor_offers)
        elif action == "product_details" or catalog_response["query_mode"] == "exact_product_lookup":
            body = self._format_full_product_details(query=query, research=vendor_offers)
        else:
            body = self._format_bd_listing(query=query, research=vendor_offers)
        default_response = f"{intent_header}\n\n{body}"
        model_response = None
        if catalog_response["query_mode"] != "exact_product_lookup" and action not in {"best_pricing_math", "vendor_coverage"}:
            model_response = self._format_with_ollama(
                query=query,
                mode="search",
                payload={
                    "results": results,
                    "query_mode": catalog_response["query_mode"],
                    "matched_vendors": catalog_response["matched_vendors"],
                    "matched_products": catalog_response["matched_products"],
                    "exact_products": catalog_response.get("exact_products", []),
                    "deep_research": vendor_offers,
                },
            )
        response = model_response or default_response
        self._log_interaction(query, "search", response)
        return response

    def _should_auto_enable_strict_keyword(self, query: str) -> bool:
        tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) >= 2]
        if len(tokens) < 2:
            return False
        broad_tokens = {
            "best",
            "product",
            "products",
            "vendor",
            "vendors",
            "category",
            "list",
            "listing",
            "show",
            "compare",
            "price",
            "pricing",
            "details",
        }
        if any(token in broad_tokens for token in tokens):
            return False
        return True

    def generate_quotation(self, query: str, selected_override: dict[str, Any] | None = None) -> dict[str, Any]:
        document_type = self._infer_invoice_type(query)
        updates, should_reset = self._extract_invoice_field_updates(query)
        if should_reset:
            self.state.invoice_preferences = {}
        if updates:
            merged_preferences = dict(self.state.invoice_preferences)
            merged_preferences.update(updates)
            self.state.invoice_preferences = merged_preferences
        preferences = dict(self.state.invoice_preferences)
        product_query = self._strip_invoice_control_segments(query)
        if not selected_override and self._looks_like_quote_request(query.lower()) and not self._has_meaningful_product_hint(product_query):
            raise ValueError("Product name missing. Please include product name with quote/invoice request.")
        quantity = self._extract_quantity(product_query) or self.state.last_quantity or 1
        discount = self._extract_discount(product_query)
        selected = selected_override or self._select_product(product_query)

        if not selected:
            raise ValueError("Could not identify product for quotation. Run a search first.")

        unit_price = float(selected["price"])
        if unit_price <= 0:
            raise ValueError("This product has no recorded price. Request a vendor price before generating a quotation.")
        subtotal = unit_price * quantity
        discount_amount = subtotal * discount
        taxable_amount = subtotal - discount_amount
        gst_rate_raw = preferences.get("gst_rate_percent", GST_RATE * 100)
        try:
            gst_rate = float(str(gst_rate_raw).strip()) / 100.0
        except (TypeError, ValueError):
            gst_rate = GST_RATE
        gst_rate = max(0.0, gst_rate)
        gst_amount = taxable_amount * gst_rate
        final_amount = taxable_amount + gst_amount

        quote = {
            "quote_id": f"HOSP-{_now_stamp()}",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "document_type": document_type,
            "product_id": selected.get("product_id") or selected.get("id", ""),
            "offer_id": selected.get("offer_id", ""),
            "product_name": selected.get("product_name", "Unknown Product"),
            "category": selected.get("category", "General"),
            "manufacturer": selected.get("manufacturer", "Unknown"),
            "vendor_name": str(preferences.get("vendor_name") or selected.get("vendor_name", "Unknown")),
            "quantity": quantity,
            "unit_price": round(unit_price, 2),
            "subtotal": round(subtotal, 2),
            "discount_rate": round(discount, 4),
            "discount_amount": round(discount_amount, 2),
            "taxable_amount": round(taxable_amount, 2),
            "gst_rate": gst_rate,
            "gst_amount": round(gst_amount, 2),
            "final_amount": round(final_amount, 2),
            "warranty": selected.get("warranty", "NA"),
            "stock": int(selected.get("stock", 0)),
            "invoice_preferences": preferences,
            "source_file": selected.get("source_file", ""),
            "source_sheet": selected.get("source_sheet", ""),
            "source_row": selected.get("source_row", ""),
            "source_page": selected.get("source_page", ""),
        }
        self.state.last_selected_product = selected
        self.state.last_quantity = quantity
        return quote

    def export_quotation_json(self, quote: dict[str, Any]) -> Path:
        file_path = self.exports_dir / f"{quote['quote_id']}.json"
        file_path.write_text(json.dumps(quote, indent=2), encoding="utf-8")
        return file_path

    def export_quotation_pdf(self, quote: dict[str, Any]) -> Path:
        file_path = self.exports_dir / f"{quote['quote_id']}.pdf"
        # Approval preview uses one shared template.
        pdf = canvas.Canvas(str(file_path), pagesize=A4)
        self._render_general_invoice_pdf(pdf, quote)
        pdf.save()
        return file_path

    def _build_invoice_context(self, quote: dict[str, Any]) -> dict[str, Any]:
        stamp = datetime.fromisoformat(quote["timestamp"])
        gst_rate = float(quote.get("gst_rate", GST_RATE))
        gst_rate_pct = round(gst_rate * 100, 2)
        preferences = quote.get("invoice_preferences") or {}
        default_terms = [
            "50% payment is required at order confirmation.",
            "Remaining 50% payment is due after successful installation/training.",
            "Delivery and logistics are managed by HOSPkart as per agreed terms.",
            "Warranty as per product listing and agreed proposal.",
        ]
        terms = preferences.get("terms") if isinstance(preferences, dict) else None
        if not isinstance(terms, list) or not terms:
            terms = default_terms
        state_name = str(preferences.get("bill_to_state_name") or "Rajasthan")
        state_code = str(preferences.get("bill_to_state_code") or "08")
        place_of_supply = str(preferences.get("place_of_supply") or f"{state_name}")
        taxable_amount = float(quote.get("taxable_amount", 0.0))
        invoice_no = str(preferences.get("invoice_no") or quote["quote_id"].replace("HOSP-", "HOSP"))
        invoice_date = str(preferences.get("invoice_date") or stamp.strftime("%d-%b-%y"))
        tax_rate_label = str(preferences.get("tax_rate_label") or f"{gst_rate_pct:.2f}%")
        if not tax_rate_label.endswith("%"):
            tax_rate_label = f"{tax_rate_label}%"
        freight_text = "Freight/Delivery charges are included."
        if isinstance(terms, list) and terms:
            freight_text = str(terms[0])
        try:
            delivery_charges = round(float(str(preferences.get("delivery_charges") or "0").strip() or "0"), 2)
        except (TypeError, ValueError):
            delivery_charges = 0.0
        taxable_with_delivery = taxable_amount + delivery_charges
        igst_amount = round(taxable_with_delivery * gst_rate, 2)
        final_amount = round(taxable_with_delivery + igst_amount, 2)
        context = {
            "quote": quote,
            "bill_date": stamp.strftime("%d-%b-%Y"),
            "bill_date_iso": stamp.strftime("%Y-%m-%d"),
            "cgst_amount": round(igst_amount / 2, 2),
            "sgst_amount": round(igst_amount / 2, 2),
            "igst_amount": igst_amount,
            "gst_rate_pct": gst_rate_pct,
            "place_of_supply": place_of_supply,
            "invoice_no": invoice_no,
            "invoice_date": invoice_date,
            "e_way_bill_no": str(preferences.get("e_way_bill_no") or ""),
            "delivery_note": str(preferences.get("delivery_note") or ""),
            "reference_no": str(preferences.get("reference_no") or ""),
            "buyers_order_no": str(preferences.get("buyers_order_no") or ""),
            "dispatch_doc_no": str(preferences.get("dispatch_doc_no") or ""),
            "dispatched_through": str(preferences.get("dispatched_through") or ""),
            "destination": str(preferences.get("destination") or state_name),
            "delivery_note_date": str(preferences.get("delivery_note_date") or ""),
            "mode_terms_of_payment": str(preferences.get("mode_terms_of_payment") or ""),
            "other_references": str(preferences.get("other_references") or ""),
            "hsn_code": str(preferences.get("hsn_code") or quote.get("product_id") or "90183990"),
            "delivery_charges": delivery_charges,
            "tax_rate_label": tax_rate_label,
            "amount_words": self._amount_to_inr_words(final_amount),
            "tax_words": self._amount_to_inr_words(igst_amount),
            "company": {
                "name": str(preferences.get("company_name") or "HOSPKART HEALTHIQUE PRIVATE LIMITED"),
                "tagline": str(preferences.get("company_tagline") or "NO MIDDLE MAN, SOURCE FROM SOURCE"),
                "address": str(preferences.get("company_address") or "Mahaveer Nagar, Kota, Rajasthan, India - 324005"),
                "pan": str(preferences.get("company_pan") or "AAHCH7245N"),
                "gstin": str(preferences.get("company_gstin") or "08AAHCH7245N1Z0"),
                "email": str(preferences.get("company_email") or "hospkart@gmail.com"),
                "phone": str(preferences.get("company_phone") or "+91-9887119475"),
                "state_name": str(preferences.get("company_state_name") or "Rajasthan"),
                "state_code": str(preferences.get("company_state_code") or "08"),
                "bank_name": str(preferences.get("bank_name") or "Kotak Bank"),
                "account_name": str(preferences.get("account_name") or "HOSPKART HEALTHIQUE PRIVATE LIMITED"),
                "account_no": str(preferences.get("account_no") or "0505505505"),
                "ifsc": str(preferences.get("ifsc") or "KKBK0003020"),
                "logo_data_uri": self._logo_data_uri(),
            },
            "party": {
                "name": str(preferences.get("bill_to_name") or "BD Team Customer (To be confirmed)"),
                "address": str(preferences.get("bill_to_address") or ""),
                "state_name": state_name,
                "state_code": state_code,
                "gstin": str(preferences.get("bill_to_gstin") or ""),
                "ship_to": str(preferences.get("ship_to") or "To be confirmed"),
            },
            "terms": terms,
            "freight_text": freight_text,
            "declaration": str(
                preferences.get("declaration")
                or "We declare that this invoice shows the actual price of the goods described and that all particulars are true and correct."
            ),
            "note": str(preferences.get("note") or ""),
            "taxable_amount": taxable_amount,
            "taxable_with_delivery": taxable_with_delivery,
            "final_amount": final_amount,
        }
        return context

    def _render_pdf_using_html_template(self, quote: dict[str, Any], output_path: Path) -> bool:
        template_name = "general_invoice.html"
        template_path = self.templates_dir / template_name
        if not template_path.exists():
            return False

        try:
            from xhtml2pdf import pisa  # type: ignore[import-not-found]
        except Exception:
            return False
        try:
            template = self.template_env.get_template(template_name)
            html = template.render(**self._build_invoice_context(quote))
            with output_path.open("wb") as file_obj:
                status = pisa.CreatePDF(html, dest=file_obj)
            return not bool(getattr(status, "err", 1))
        except Exception:
            return False

    def _render_quotation_text_template(self, quote: dict[str, Any], json_file: Path, pdf_file: Path) -> str:
        preferences = quote.get("invoice_preferences") or {}
        doc_label = "Purchase Invoice" if quote.get("document_type") == "purchase_invoice" else "Tax Invoice"
        bill_to_name = str(preferences.get("bill_to_name") or "BD Team Customer (To be confirmed)")
        place_of_supply = str(
            preferences.get("place_of_supply")
            or f"{preferences.get('bill_to_state_name', 'Rajasthan')} ({preferences.get('bill_to_state_code', '08')})"
        )
        return (
            "QUOTATION GENERATED\n"
            f"Document Type: {doc_label}\n"
            f"Quotation ID: {quote['quote_id']}\n"
            f"Product: {quote['product_name']}\n"
            f"Vendor: {quote['vendor_name']}\n"
            f"Bill To: {bill_to_name}\n"
            f"Place of Supply: {place_of_supply}\n"
            f"Quantity: {quote['quantity']}\n"
            f"Unit Price: INR {quote['unit_price']:.2f}\n"
            f"Subtotal: INR {quote['subtotal']:.2f}\n"
            f"Discount: INR {quote['discount_amount']:.2f}\n"
            f"GST: INR {quote['gst_amount']:.2f}\n"
            f"Final Amount: INR {quote['final_amount']:.2f}\n"
            f"JSON Export: {json_file}\n"
            f"PDF Export: {pdf_file}"
        )

    def _split_text_lines(self, text: str, max_chars: int) -> list[str]:
        words = str(text or "").split()
        if not words:
            return [""]
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if len(candidate) <= max_chars:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _number_to_words(self, value: int) -> str:
        ones = [
            "",
            "One",
            "Two",
            "Three",
            "Four",
            "Five",
            "Six",
            "Seven",
            "Eight",
            "Nine",
            "Ten",
            "Eleven",
            "Twelve",
            "Thirteen",
            "Fourteen",
            "Fifteen",
            "Sixteen",
            "Seventeen",
            "Eighteen",
            "Nineteen",
        ]
        tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

        if value == 0:
            return "Zero"
        if value < 20:
            return ones[value]
        if value < 100:
            return tens[value // 10] + (f" {ones[value % 10]}" if value % 10 else "")
        if value < 1000:
            return (
                f"{ones[value // 100]} Hundred"
                + (f" {self._number_to_words(value % 100)}" if value % 100 else "")
            )
        if value < 100000:
            return (
                f"{self._number_to_words(value // 1000)} Thousand"
                + (f" {self._number_to_words(value % 1000)}" if value % 1000 else "")
            )
        if value < 10000000:
            return (
                f"{self._number_to_words(value // 100000)} Lakh"
                + (f" {self._number_to_words(value % 100000)}" if value % 100000 else "")
            )
        return (
            f"{self._number_to_words(value // 10000000)} Crore"
            + (f" {self._number_to_words(value % 10000000)}" if value % 10000000 else "")
        )

    def _amount_to_inr_words(self, amount: float) -> str:
        total_paise = int(round(max(amount, 0.0) * 100))
        rupees = total_paise // 100
        paise = total_paise % 100
        rupee_words = self._number_to_words(rupees)
        if paise:
            return f"INR {rupee_words} Rupees and {self._number_to_words(paise)} Paise only"
        return f"INR {rupee_words} Rupees only"

    def _render_general_invoice_pdf(self, pdf: canvas.Canvas, quote: dict[str, Any]) -> None:
        ctx = self._build_invoice_context(quote)
        taxable_value = float(ctx["taxable_with_delivery"])
        tax_amount = float(ctx["igst_amount"])
        final_amount = float(ctx["final_amount"])
        title = "PURCHASE INVOICE" if quote.get("document_type") == "purchase_invoice" else "TAX INVOICE"

        pdf.setTitle(f"{quote['quote_id']} {title}")
        left = 22
        right = 573
        top = 798
        bottom = 46
        split_x = 330
        meta_split_x = 468
        pdf.setLineWidth(0.9)
        pdf.rect(left, bottom, right - left, top - bottom)
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawCentredString((left + right) / 2, 815, title)

        # Header area
        head_top = top
        head_bottom = 640
        pdf.line(left, head_bottom, right, head_bottom)
        pdf.line(split_x, head_bottom, split_x, head_top)
        pdf.line(meta_split_x, head_bottom, meta_split_x, head_top)
        meta_rows = [770, 744, 718, 692, 666]
        for y in meta_rows:
            pdf.line(split_x, y, right, y)

        if self.logo_path and self.logo_path.exists():
            try:
                pdf.drawImage(ImageReader(str(self.logo_path)), 56, 690, width=128, height=80, preserveAspectRatio=True)
            except Exception:
                pass
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawCentredString(205, 787, str(ctx["company"]["name"])[:60])
        for i, line in enumerate(self._split_text_lines(str(ctx["company"]["address"]), 48)[:3]):
            pdf.drawCentredString(205, 776 - (i * 10), line)
        pdf.drawCentredString(205, 742, f"GSTIN: {ctx['company']['gstin']}")
        pdf.drawCentredString(205, 731, f"State: {ctx['company']['state_name']} ({ctx['company']['state_code']})")
        pdf.drawCentredString(205, 720, f"E-Mail: {ctx['company']['email']}")

        pdf.setFont("Helvetica", 7)
        pdf.drawString(meta_split_x + 4, 778, "e-Way Bill No.")
        pdf.drawRightString(right - 5, 778, str(ctx["e_way_bill_no"])[:20])
        pdf.drawString(split_x + 4, 752, "Invoice No.")
        pdf.drawString(meta_split_x + 4, 752, "Dated")
        pdf.drawString(split_x + 4, 726, "Delivery Note")
        pdf.drawString(meta_split_x + 4, 726, "Mode/Terms of Payment")
        pdf.drawString(split_x + 4, 700, "Reference No. & Date")
        pdf.drawString(meta_split_x + 4, 700, "Other References")
        pdf.drawString(split_x + 4, 674, "Buyer's Order No.")
        pdf.drawString(meta_split_x + 4, 674, "Dated")
        pdf.drawString(split_x + 4, 648, "Dispatch Doc No.")
        pdf.drawString(meta_split_x + 4, 648, "Delivery Note Date")
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawRightString(meta_split_x - 5, 752, str(ctx["invoice_no"])[:22])
        pdf.drawRightString(right - 5, 752, str(ctx["invoice_date"])[:18])
        pdf.drawRightString(meta_split_x - 5, 726, str(ctx["delivery_note"])[:22])
        pdf.drawRightString(right - 5, 726, str(ctx["mode_terms_of_payment"])[:22])
        pdf.drawRightString(meta_split_x - 5, 700, str(ctx["reference_no"])[:22])
        pdf.drawRightString(right - 5, 700, str(ctx["other_references"])[:22])
        pdf.drawRightString(meta_split_x - 5, 674, str(ctx["buyers_order_no"])[:22])
        pdf.drawRightString(meta_split_x - 5, 648, str(ctx["dispatch_doc_no"])[:22])
        pdf.drawRightString(right - 5, 648, str(ctx["delivery_note_date"])[:22])

        # Consignee section
        cons_top = head_bottom
        cons_bottom = 545
        pdf.line(left, cons_bottom, right, cons_bottom)
        pdf.line(split_x, cons_bottom, split_x, cons_top)
        pdf.line(split_x, 602, right, 602)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(left + 4, 625, "Consignee (Ship to)")
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(left + 4, 610, str(ctx["party"]["name"])[:58])
        pdf.setFont("Helvetica", 8)
        for idx, line in enumerate(self._split_text_lines(str(ctx["party"]["ship_to"]), 55)[:3]):
            pdf.drawString(left + 4, 596 - (idx * 11), line)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(left + 4, 556, f"State: {ctx['party']['state_code']}-{ctx['party']['state_name']}")

        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(split_x + 4, 612, "Terms of Delivery")
        pdf.setFont("Helvetica", 8)
        for idx, line in enumerate(self._split_text_lines(str(ctx["freight_text"]), 40)[:3]):
            pdf.drawString(split_x + 4, 598 - (idx * 11), line)
        pdf.drawString(split_x + 4, 568, f"Dispatch through: {ctx['dispatched_through']}"[:40])
        pdf.drawString(split_x + 4, 557, f"Destination: {ctx['destination']}"[:40])

        # Buyer section
        buyer_top = cons_bottom
        buyer_bottom = 480
        pdf.line(left, buyer_bottom, right, buyer_bottom)
        pdf.line(split_x, buyer_bottom, split_x, buyer_top)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(left + 4, 533, "Buyer (Bill to)")
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(left + 4, 520, str(ctx["party"]["name"])[:58])
        pdf.setFont("Helvetica", 8)
        bill_to_text = str(ctx["party"]["address"]).strip() or str(ctx["party"]["ship_to"])
        for idx, line in enumerate(self._split_text_lines(bill_to_text, 56)[:2]):
            pdf.drawString(left + 4, 507 - (idx * 11), line)
        pdf.drawString(left + 4, 486, f"GSTIN: {str(ctx['party']['gstin']) or 'NA'}")

        # Main goods table
        table_top = buyer_bottom
        table_bottom = 258
        header_y = 456
        total_y = 258
        col_x = [left, 46, 292, 340, 386, 435, 476, right]
        pdf.rect(left, table_bottom, right - left, table_top - table_bottom)
        for x in col_x:
            pdf.line(x, table_bottom, x, table_top)
        pdf.line(left, header_y, right, header_y)
        pdf.line(left, total_y + 24, right, total_y + 24)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawCentredString((left + 46) / 2, 468, "SI")
        pdf.drawCentredString((left + 46) / 2, 460, "No.")
        pdf.drawCentredString((46 + 292) / 2, 464, "Description of Goods")
        pdf.drawCentredString((292 + 340) / 2, 464, "HSN/SAC")
        pdf.drawCentredString((340 + 386) / 2, 464, "Quantity")
        pdf.drawCentredString((386 + 435) / 2, 464, "Rate")
        pdf.drawCentredString((435 + 476) / 2, 464, "Unit")
        pdf.drawCentredString((476 + right) / 2, 464, "Amount")

        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(left + 6, 442, "1")
        pdf.drawString(52, 442, str(quote["product_name"])[:46])
        pdf.drawRightString(336, 442, str(ctx["hsn_code"]))
        pdf.drawRightString(382, 442, f"{quote['quantity']:,}")
        pdf.drawRightString(431, 442, f"{quote['unit_price']:.2f}")
        pdf.drawCentredString((435 + 476) / 2, 442, "PCS")
        pdf.drawRightString(right - 5, 442, f"{quote['taxable_amount']:.2f}")

        running_y = 424
        if float(ctx["delivery_charges"]) > 0:
            pdf.setFont("Helvetica-Oblique", 8)
            pdf.drawString(52, running_y, "Delivery Charges")
            pdf.setFont("Helvetica-Bold", 8)
            pdf.drawRightString(right - 5, running_y, f"{ctx['delivery_charges']:.2f}")
            running_y -= 16
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(260, running_y, "IGST")
        pdf.drawRightString(right - 5, running_y + 16, f"{taxable_value:.2f}")
        pdf.drawRightString(right - 5, running_y, f"{tax_amount:.2f}")
        pdf.drawRightString(288, total_y + 8, "Total")
        pdf.drawRightString(right - 5, total_y + 8, f"₹ {final_amount:,.2f}")

        # Amount in words
        words_top = 258
        words_bottom = 218
        pdf.rect(left, words_bottom, right - left, words_top - words_bottom)
        pdf.line(left, words_top - 16, right, words_top - 16)
        pdf.setFont("Helvetica-BoldOblique", 8)
        pdf.drawString(left + 4, words_top - 12, "Amount Chargeable (in words)")
        pdf.drawRightString(right - 5, words_top - 12, "E. & O.E")
        pdf.setFont("Helvetica-BoldOblique", 8)
        for idx, line in enumerate(self._split_text_lines(str(ctx["amount_words"]), 92)[:2]):
            pdf.drawString(left + 4, words_top - 28 - (idx * 10), line)

        # Tax summary table
        tax_top = words_bottom
        tax_bottom = 158
        tax_cols = [left, 250, 332, 426, 510, right]
        pdf.rect(left, tax_bottom, right - left, tax_top - tax_bottom)
        for x in tax_cols:
            pdf.line(x, tax_bottom, x, tax_top)
        pdf.line(left, tax_top - 24, right, tax_top - 24)
        pdf.setFont("Helvetica", 7)
        pdf.drawCentredString((left + 250) / 2, tax_top - 15, "HSN/SAC")
        pdf.drawCentredString((250 + 332) / 2, tax_top - 15, "Taxable Value")
        pdf.drawCentredString((332 + 510) / 2, tax_top - 15, "IGST")
        pdf.drawCentredString((332 + 426) / 2, tax_top - 31, "Rate")
        pdf.drawCentredString((426 + 510) / 2, tax_top - 31, "Amount")
        pdf.drawCentredString((510 + right) / 2, tax_top - 15, "Total Tax Amount")
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(left + 4, tax_bottom + 12, str(ctx["hsn_code"]))
        pdf.drawRightString(328, tax_bottom + 12, f"{taxable_value:.2f}")
        pdf.drawCentredString((332 + 426) / 2, tax_bottom + 12, str(ctx["tax_rate_label"]))
        pdf.drawRightString(506, tax_bottom + 12, f"{tax_amount:.2f}")
        pdf.drawRightString(right - 5, tax_bottom + 12, f"{tax_amount:.2f}")

        # Tax words + declaration / bank
        note_top = 158
        note_mid = 140
        decl_bottom = 70
        pdf.rect(left, decl_bottom, right - left, note_top - decl_bottom)
        pdf.line(left, note_mid, right, note_mid)
        pdf.line(364, decl_bottom, 364, note_mid)
        pdf.setFont("Helvetica-BoldOblique", 8)
        pdf.drawString(left + 4, 146, f"Tax Amount (in words): {ctx['tax_words']}"[:104])
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(left + 4, 130, "Declaration")
        pdf.setFont("Helvetica", 7)
        for idx, line in enumerate(self._split_text_lines(str(ctx["declaration"]), 72)[:3]):
            pdf.drawString(left + 4, 119 - (idx * 9), line)
        pdf.drawString(370, 130, "Company's Bank Details")
        pdf.drawString(370, 120, f"Bank Name      : {ctx['company']['bank_name']}"[:34])
        pdf.drawString(370, 110, f"A/c No.        : {ctx['company']['account_no']}"[:34])
        pdf.drawString(370, 100, f"Branch & IFSC  : {ctx['company']['ifsc']}"[:34])
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawRightString(right - 5, 86, f"for {ctx['company']['name']}"[:56])
        pdf.setFont("Helvetica", 7)
        pdf.drawRightString(right - 5, 76, "Authorised Signatory")

        # Footer
        pdf.rect(left, bottom, right - left, decl_bottom - bottom)
        pdf.setFont("Helvetica", 7)
        pdf.drawCentredString((left + right) / 2, 58, "SUBJECT TO RAJASTHAN JURISDICTION")
        pdf.drawCentredString((left + right) / 2, 49, "This is a Computer Generated Invoice")

    def _render_tax_invoice_pdf(self, pdf: canvas.Canvas, quote: dict[str, Any]) -> None:
        pdf.setTitle(f"{quote['quote_id']} TAX INVOICE")
        bill_date = datetime.fromisoformat(quote["timestamp"]).strftime("%d-%b-%Y")
        cgst = quote["gst_amount"] / 2
        sgst = quote["gst_amount"] / 2

        y = 810
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(50, y, "TAX INVOICE")
        y -= 24
        pdf.setFont("Helvetica", 10)
        pdf.drawString(50, y, f"Bill No.: {quote['quote_id']}")
        pdf.drawString(250, y, f"Bill Date: {bill_date}")
        pdf.drawString(430, y, "Place of Supply: Rajasthan (08)")
        y -= 20
        pdf.drawString(50, y, "Reference Quotation: Prepared by HospKart BD Assistant")

        y -= 30
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(50, y, "Bill From")
        pdf.drawString(310, y, "Bill To")
        y -= 16
        pdf.setFont("Helvetica", 10)
        pdf.drawString(50, y, "HOSPkart Healthique Pvt Ltd")
        pdf.drawString(310, y, "BD Team Customer (To be confirmed)")
        y -= 14
        pdf.drawString(50, y, "Mahaveer Nagar, Kota, Rajasthan, India - 324005")
        pdf.drawString(310, y, f"Vendor Offer Source: {quote['vendor_name']}")
        y -= 14
        pdf.drawString(50, y, "PAN: AAHCH7245N | GSTIN: 08AAHCH7245N1Z0")
        pdf.drawString(310, y, "State Name: Rajasthan, Code: 08")
        y -= 14
        pdf.drawString(50, y, "Email: hospkart@gmail.com | Phone: +91-9887119475")

        y -= 28
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(50, y, "SL")
        pdf.drawString(80, y, "Description")
        pdf.drawString(330, y, "Qty")
        pdf.drawString(370, y, "Rate")
        pdf.drawString(440, y, "GST(%)")
        pdf.drawString(500, y, "Amount")
        y -= 6
        pdf.line(50, y, 560, y)
        y -= 14

        pdf.setFont("Helvetica", 10)
        description = f"{quote['product_name']} | Warranty: {quote['warranty']}"
        pdf.drawString(50, y, "1")
        pdf.drawString(80, y, description[:42])
        if len(description) > 42:
            y -= 12
            pdf.drawString(80, y, description[42:84])
        pdf.drawRightString(355, y, str(quote["quantity"]))
        pdf.drawRightString(430, y, f"{quote['unit_price']:.2f}")
        pdf.drawRightString(485, y, f"{int(quote['gst_rate'] * 100)}")
        pdf.drawRightString(560, y, f"{quote['taxable_amount']:.2f}")

        y -= 28
        pdf.line(50, y, 560, y)
        y -= 16
        pdf.drawRightString(470, y, "Taxable Value:")
        pdf.drawRightString(560, y, f"INR {quote['taxable_amount']:.2f}")
        y -= 14
        pdf.drawRightString(470, y, "CGST:")
        pdf.drawRightString(560, y, f"INR {cgst:.2f}")
        y -= 14
        pdf.drawRightString(470, y, "SGST:")
        pdf.drawRightString(560, y, f"INR {sgst:.2f}")
        y -= 14
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawRightString(470, y, "Total (incl. GST):")
        pdf.drawRightString(560, y, f"INR {quote['final_amount']:.2f}")

        y -= 32
        pdf.setFont("Helvetica", 9)
        pdf.drawString(50, y, "Terms: 50% advance, remaining after installation/training.")
        y -= 12
        pdf.drawString(50, y, "This is an electronically generated tax invoice.")

    def _render_purchase_invoice_pdf(self, pdf: canvas.Canvas, quote: dict[str, Any]) -> None:
        pdf.setTitle(f"{quote['quote_id']} PURCHASE INVOICE")
        bill_date = datetime.fromisoformat(quote["timestamp"]).strftime("%d-%b-%Y")

        y = 810
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(50, y, "PURCHASE INVOICE")
        y -= 24
        pdf.setFont("Helvetica", 10)
        pdf.drawString(50, y, f"Purchase No.: {quote['quote_id']}")
        pdf.drawString(260, y, f"Date: {bill_date}")
        pdf.drawString(430, y, "Station: Jaipur")

        y -= 26
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(50, y, "Supplier")
        pdf.drawString(310, y, "Buyer")
        y -= 16
        pdf.setFont("Helvetica", 10)
        pdf.drawString(50, y, quote["vendor_name"][:40])
        pdf.drawString(310, y, "HOSPkart Healthique Pvt Ltd")
        y -= 14
        pdf.drawString(50, y, f"Product Category: {quote['category']}")
        pdf.drawString(310, y, "Mahaveer Nagar, Kota, Rajasthan")
        y -= 14
        pdf.drawString(50, y, f"Manufacturer: {quote['manufacturer']}")
        pdf.drawString(310, y, "Email: hospkart@gmail.com")

        y -= 30
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(50, y, "Description")
        pdf.drawString(320, y, "Qty")
        pdf.drawString(360, y, "Unit Price")
        pdf.drawString(440, y, "Discount")
        pdf.drawString(520, y, "Total")
        y -= 6
        pdf.line(50, y, 560, y)
        y -= 14

        pdf.setFont("Helvetica", 10)
        pdf.drawString(50, y, quote["product_name"][:44])
        if len(quote["product_name"]) > 44:
            y -= 12
            pdf.drawString(50, y, quote["product_name"][44:88])
        pdf.drawRightString(345, y, str(quote["quantity"]))
        pdf.drawRightString(425, y, f"INR {quote['unit_price']:.2f}")
        pdf.drawRightString(505, y, f"INR {quote['discount_amount']:.2f}")
        pdf.drawRightString(560, y, f"INR {quote['taxable_amount']:.2f}")

        y -= 28
        pdf.line(50, y, 560, y)
        y -= 16
        pdf.drawRightString(470, y, "Subtotal:")
        pdf.drawRightString(560, y, f"INR {quote['subtotal']:.2f}")
        y -= 14
        pdf.drawRightString(470, y, "GST (18%):")
        pdf.drawRightString(560, y, f"INR {quote['gst_amount']:.2f}")
        y -= 14
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawRightString(470, y, "Final Amount:")
        pdf.drawRightString(560, y, f"INR {quote['final_amount']:.2f}")

        y -= 30
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(50, y, "Bank Details & Declaration")
        y -= 14
        pdf.setFont("Helvetica", 9)
        pdf.drawString(50, y, "Bank: Kotak Bank | A/C: 0505505505 | IFSC: KKBK0003020")
        y -= 12
        pdf.drawString(50, y, "Payment: 50% advance. Remaining post installation and handover.")
        y -= 12
        pdf.drawString(50, y, "This is a computer generated purchase invoice.")

    def handle_query(self, user_query: str, options: dict[str, Any] | None = None) -> str:
        if self.state.pending_invoice_flow:
            return self._continue_invoice_workflow(user_query)

        smalltalk = self._smalltalk_reply(user_query)
        if smalltalk is not None:
            self._log_interaction(user_query, "smalltalk", smalltalk)
            return smalltalk

        if self._should_start_invoice_workflow(user_query):
            return self._start_invoice_workflow(user_query)

        intent = self._infer_intent(user_query)
        if intent == "search":
            return self.search(user_query, options=options)

        quote = self.generate_quotation(user_query)
        json_file = self.export_quotation_json(quote)
        pdf_file = self.export_quotation_pdf(quote)
        # Fixed quote template for consistent BD output.
        response = self._render_quotation_text_template(quote=quote, json_file=json_file, pdf_file=pdf_file)
        self._log_interaction(user_query, intent, response)
        return response
