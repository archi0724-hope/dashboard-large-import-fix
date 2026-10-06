"""Exercise live imports, price safety, mounted URLs and session isolation."""
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from hospkart_chat import integration
from hospkart_chat.app import app as flask_app


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    store = integration.open_vendor_store(str(tmp_path / "vendors"), "")
    csv_path = tmp_path / "products.csv"
    pd.DataFrame([{
        "id": "base-1", "product_name": "Original Test Scalpel", "vendor_name": "Original Supplier",
        "price": 30, "category": "Surgical", "manufacturer": "", "specs": "", "warranty": "", "stock": 5,
    }]).to_csv(csv_path, index=False)
    with store.connection() as db:
        db.execute("""INSERT INTO vdd_documents
            (id,company_key,company_name,filename,original_path,file_hash,uploaded_at)
            VALUES(1,'acme','Acme Supplies','catalogue.csv','catalogue.csv','test-doc','2026-10-06')""")
    store.save_product_records(1, [{"product_name": "Unique Test Glove", "sku": "UTG123", "price": "125",
                                   "currency": "INR", "source_file": "catalogue.csv", "source_row": 2},
                                  {"product_name": "Unpriced Test Probe", "sku": "UTP123"}])
    source = integration.CatalogueSource(store, csv_path)
    monkeypatch.setattr(integration, "catalogue_source", lambda *args: source)
    monkeypatch.setattr(integration, "ROOT", tmp_path / "chatbot")
    integration._sessions.clear()
    yield source
    integration._sessions.clear()


def test_live_imports_and_original_catalogue(catalogue):
    rag = integration.LiveCatalogueRAG(catalogue)
    assert rag.search_products("Unique Test Glove")[0]["metadata"]["vendor_name"] == "Acme Supplies"
    assert rag.search_products("Original Test Scalpel")[0]["metadata"]["price"] == 30
    assert rag.search_products("Fresh Test Needle") == []
    catalogue.store.save_product_records(1, [{"product_name": "Fresh Test Needle", "price": "50", "currency": "INR"}])
    assert rag.search_products("Fresh Test Needle")[0]["metadata"]["price"] == 50
    assert rag.search_products("UTG123")[0]["metadata"]["source_file"] == "catalogue.csv"
    with catalogue.store.connection() as db:
        db.execute("UPDATE vdd_product_records SET price='175' WHERE sku='UTG123'")
    assert rag.search_products("UTG123")[0]["metadata"]["price"] == 175


def test_unpriced_matches_are_visible_and_cannot_be_quoted(catalogue):
    agent = integration.get_session_agent("price-test")
    result = agent.handle_query("Unpriced Test Probe")
    assert "Unpriced Test Probe" in result
    assert "Price not recorded" in result
    assert "INR 0.00" not in result
    with pytest.raises(ValueError, match="no recorded price"):
        agent.generate_quotation("quote for 2 units", selected_override={"product_name": "Unpriced Test Probe", "price": 0})


def test_chat_context_and_downloads_are_isolated(catalogue):
    a, b = flask_app.test_client(), flask_app.test_client()
    assert a.post("/api/chat", json={"query": "Unique Test Glove"}).status_code == 200
    b.get("/health")
    with a.session_transaction() as session:
        agent_a = integration.get_session_agent(session["chat_id"])
    with b.session_transaction() as session:
        agent_b = integration.get_session_agent(session["chat_id"])
    assert agent_a.state.last_selected_product["price"] == 125
    assert agent_b.state.last_selected_product is None
    quote = agent_a.generate_quotation("quote for 2 units", selected_override=agent_a.state.last_selected_product)
    assert quote["final_amount"] == 295
    exported = agent_a.export_quotation_json(quote)
    pdf = agent_a.export_quotation_pdf(quote)
    assert pdf.read_bytes().startswith(b"%PDF")
    assert a.get(f"/downloads/{exported.name}").status_code == 200
    assert b.get(f"/downloads/{exported.name}").status_code == 404


def test_mounted_chat_assets_and_api(catalogue):
    from app.main import app
    with TestClient(app) as client:
        page = client.get("/chatbot/", follow_redirects=False)
        assert page.status_code == 302
        assert page.headers["location"] == "/vendor/?assistant=1"
        assert client.get("/chatbot/assets/hospkart-logo").headers["content-type"] == "image/jpeg"
        assert client.get("/chatbot/health").json()["status"] == "ok"
        response = client.post("/chatbot/api/chat", json={"query": "Unique Test Glove"})
        assert response.status_code == 200
        assert "Acme Supplies" in response.json()["response"]
        assert client.post("/chatbot/api/chat", json={}).status_code == 400
        assert 'data-workspace="chatbot"' not in client.get("/").text
        page = client.get("/chatbot/ui/")
        assert page.status_code == 200
        assert "QuoteSarthi AI" in page.text
        assert 'id="getStartedBtn"' in page.text
        assert 'id="exportChatBtn"' in page.text
        assert 'id="maxProducts"' in page.text
        assert 'src="/chatbot/assets/hospkart-hero"' in page.text
        assert 'window.location.origin + "/chatbot"' in page.text
        assert "__CHAT_PREFIX__" not in page.text
        response = client.post("/chatbot/api/chat", json={"query": "Unique Test Glove"})
        assert response.status_code == 200
        assert "Acme Supplies" in response.json()["response"]
        quote = client.post("/chatbot/api/chat", json={"query": "quote for Unique Test Glove 2 units"})
        assert quote.status_code == 200
        assert "295.00" in quote.json()["response"]


def test_vendor_assistant_embeds_quotesarthi_ui():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_string("from hospkart_chat.vendor_assistant import render_assistant\nrender_assistant()").run(timeout=30)
    assert not at.exception
    assert at.get("iframe")[0].proto.src == "/chatbot/ui/"


@pytest.mark.parametrize("value,expected", [("₹1,250.50", 1250.5), ("Rs. 25", 25), ("100-200", 0), ("on request", 0)])
def test_only_explicit_prices_are_used(value, expected):
    assert integration.recorded_price(value) == expected
