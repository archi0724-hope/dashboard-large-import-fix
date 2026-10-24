from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.drive import index_api, indexer
from app.drive.source_links import parse_source


@pytest.mark.parametrize('link',[
    'http://drive.google.com/file/d/archive12345/view','https://evil.example/drive/folders/folder123456',
    'https://drive.google.com.evil.example/file/d/archive12345/view',
    'https://user:pass@drive.google.com/file/d/archive12345/view',
    'C:\\Users\\Downloads\\vendors.zip','https://drive.google.com/drive/home',
    'https://drive.google.com:444/drive/folders/folder123456'])
def test_rejects_invalid_source_before_network(link):
    with pytest.raises(ValueError):
        parse_source(link)


def test_folder_and_zip_links_are_normalized():
    assert parse_source('https://drive.google.com/drive/folders/folder123456?usp=sharing')['kind'] == 'folder'
    source = parse_source('https://drive.google.com/file/d/archive12345/view?usp=drive_link')
    assert source == {'kind':'zip','id':'archive12345','url':'https://drive.google.com/file/d/archive12345/view'}


def test_pasted_source_persists_and_does_not_hide_existing_records(tmp_path,monkeypatch):
    from pathlib import Path

    from app.drive.index_store import DriveIndex
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'vendor_dashboard'))
    from vendor_dashboard.storage import Store
    index = DriveIndex(Store(tmp_path,initialize=False))
    entry = {'entry_key':'existing','folder_id':'folder123456','drive_id':'existing12345',
        'source_name':'old.csv','size_bytes':10,'version':'1'}
    index.discover([entry])
    index.save_records(entry,[{'company':'Existing Medical','product':'Pump','kind':'product'}])
    monkeypatch.setattr(index_api,'library',lambda:index)
    monkeypatch.setattr(index_api,'get_settings',lambda:SimpleNamespace(
        dashboard_token='source-test-token',drive_folder_id='folder123456',drive_folder_url=''))
    worker = Mock()
    monkeypatch.setattr(indexer,'start_index',worker)
    monkeypatch.setattr(indexer,'ACTIVE',None)
    app = FastAPI()
    app.include_router(index_api.router)
    client = TestClient(app)
    headers = {'X-Token':'source-test-token'}
    assert client.post('/api/drive-index/start',json={'link':'https://drive.google.com/file/d/archive12345/view'}).status_code == 401
    invalid = client.post('/api/drive-index/start',headers=headers,json={'link':'https://evil.example/file.zip'})
    assert invalid.status_code == 422
    worker.assert_not_called()
    response = client.post('/api/drive-index/start',headers=headers,json={'link':'https://drive.google.com/file/d/archive12345/view'})
    assert response.status_code == 200,response.text
    assert worker.call_args.args[1] == 'folder123456'
    assert worker.call_args.args[2]['kind'] == 'zip'
    assert client.get('/api/drive-index/search',headers=headers).json()['total'] == 1
    assert DriveIndex(index.store).source_selection()['source']['id'] == 'archive12345'
    assert client.get('/api/drive-index/status',headers=headers).json()['source_link'].endswith('/archive12345/view')
    monkeypatch.setattr(indexer,'ACTIVE',SimpleNamespace(is_alive=lambda:True))
    blocked = client.post('/api/drive-index/start',headers=headers,json={'link':'https://drive.google.com/drive/folders/another12345'})
    assert blocked.status_code == 409
    assert index.source_selection()['source']['id'] == 'archive12345'
    monkeypatch.setattr(indexer,'ACTIVE',None)
    response = client.post('/api/drive-index/start',headers=headers,json={'link':'https://drive.google.com/drive/folders/another12345'})
    assert response.status_code == 200
    assert worker.call_args.args[2]['kind'] == 'folder'
    assert index.source_selection()['scope'] == 'folder123456'
    assert client.get('/api/drive-index/search',headers=headers).json()['total'] == 1
