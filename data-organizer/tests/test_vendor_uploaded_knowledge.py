import json

import pytest

from hospkart_chat import integration


def agent_for(source, tmp_path):
    agent = integration.MergedAgent(rag=integration.LiveCatalogueRAG(source),
                                    logs_dir=tmp_path / "logs", exports_dir=tmp_path / "exports")
    agent.ollama_enabled = False
    return agent


def test_default_session_uses_uploaded_data_only(tmp_path, monkeypatch):
    store = integration.open_vendor_store(str(tmp_path / "vendors"), "")
    source = integration.CatalogueSource(store, None)
    requested = []
    monkeypatch.setattr(integration, "catalogue_source", lambda data, database, csv: requested.append(csv) or source)
    monkeypatch.setattr(integration, "ROOT", tmp_path / "chatbot")
    monkeypatch.setenv("HOSPKART_INCLUDE_REFERENCE_CATALOGUE", "false")
    integration._sessions.clear()
    agent = integration.get_session_agent("uploaded-only-test")
    assert requested == [""]
    assert agent.rag.source_status()["reference_catalogue_enabled"] is False
    assert "No searchable catalogue records" in agent.handle_query("show hospital beds")
    integration._sessions.clear()


def test_actual_upload_is_available_to_an_existing_assistant(tmp_path):
    store = integration.open_vendor_store(str(tmp_path / "vendors"), "")
    source = integration.CatalogueSource(store, None)
    agent = agent_for(source, tmp_path)
    assert "No searchable catalogue records" in agent.handle_query("show ZX Glove")
    from import_service import import_documents

    class Upload:
        name = "Acme price list.csv"

        def getvalue(self):
            return b"Product,Code,Price,Specification\nZX Glove,ZX123,125,Sterile latex\n"

    result = import_documents(store, [Upload()], forced_company="Acme Supplies", retain_archive=False)
    assert result.saved_files == 1 and not result.issues
    assert result.catalogue_records_pending == 1
    assert "No searchable catalogue records" in agent.handle_query("ZX123")
    imported = store.product_imports()[0]
    row = store.product_import_rows(imported["id"])[0]
    store.decide_product_import_row(row["id"], "approve")
    answer = agent.handle_query("ZX123")
    assert "Acme Supplies" in answer and "125.00" in answer
    assert "Acme price list.csv" in answer
    assert agent.rag.source_status()["vendor_records"] == 1
    assert agent.rag.source_status()["hospkart_records"] == 0
    with store.connection() as db:
        db.execute("UPDATE vdd_product_records SET price='175' WHERE sku='ZX123'")
    answer = agent.handle_query("ZX123")
    assert "175.00" in answer and agent.state.last_selected_product["price"] == 175
    with store.connection() as db:
        db.execute("DELETE FROM vdd_product_records")
    assert "No searchable catalogue records" in agent.handle_query("quote ZX123 2 units")
    assert agent.state.last_selected_product is None


def test_uploaded_document_questions_do_not_invent_missing_categories(tmp_path):
    store = integration.open_vendor_store(str(tmp_path / "vendors"), "")
    with store.connection() as db:
        db.execute("""INSERT INTO vdd_documents
            (company_key,company_name,types_json,filename,original_path,file_hash,uploaded_at)
            VALUES('acme','Acme Supplies',?,'GST.pdf','Acme/GST.pdf','test-doc','2026-10-10')""",
            (json.dumps(["GST"]),))
    agent = agent_for(integration.CatalogueSource(store, None), tmp_path)
    response = agent.handle_query("Acme GST documents")
    assert "Acme/GST.pdf" in response and "Stored file missing" in response
    assert "validity is not verified" in response
    assert "No matching document" in agent.handle_query("Acme PAN documents")
    assert "No matching document" in agent.handle_query("Unknown Company GST documents")
