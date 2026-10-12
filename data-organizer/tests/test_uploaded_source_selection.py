from dataclasses import replace

from .test_api_export import client


def test_select_saved_uploads_includes_zip_and_loose_files(client, tmp_path):
    from app.main import st
    state = st()
    state.settings = replace(state.settings, data_dir=tmp_path / "data")
    state.cfg.local_input_dir = ""
    root = state.settings.uploads_dir
    extracted = root / "_extracted" / "upload-123456789abc" / "Acme"
    extracted.mkdir(parents=True)
    (extracted / "catalogue.csv").write_text("Product,Price\nGlove,125", encoding="utf-8")
    loose = root / "prices.csv"
    loose.write_text("Company,Product,Price\nAcme,Glove,125", encoding="utf-8")
    before = loose.read_bytes()
    response = client.post("/api/source/uploads")
    assert response.status_code == 200
    assert response.json()["folder"] == str(root / "_extracted")
    assert loose.read_bytes() == before
    assert (root / "_extracted" / "_loose_uploads" / "prices.csv").read_bytes() == before
    assert client.post("/api/scan").status_code == 200


def test_no_uploads_gives_source_selection_guidance(client, tmp_path):
    from app.main import st
    state = st()
    state.settings = replace(state.settings, data_dir=tmp_path / "empty-data")
    state.cfg.local_input_dir = ""
    response = client.post("/api/source/uploads")
    assert response.status_code == 400
    assert "Open Import files" in response.json()["detail"]
    assert state.cfg.local_input_dir == ""


def test_technical_upload_folder_is_not_a_company_name():
    from app.services import _company_folder_from_source_path
    assert _company_folder_from_source_path("upload-123456789abc/Acme/Documents/file.pdf") == "Acme"
    assert _company_folder_from_source_path("_loose_uploads/file.xlsx") == ""
