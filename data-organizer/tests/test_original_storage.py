from io import BytesIO
from types import SimpleNamespace
import pytest
from app.drive import original_storage


class Body(BytesIO):
    pass


class Missing(Exception):
    response={'Error':{'Code':'NoSuchKey'}}


def client(monkeypatch, payload=b'original', advertised=None, error=None):
    body=Body(payload)
    calls=[]
    def get_object(**kwargs):
        calls.append(kwargs)
        if error: raise error
        return {'Body':body,'ContentLength':len(payload) if advertised is None else advertised}
    monkeypatch.setattr(original_storage,'_client',lambda:(SimpleNamespace(get_object=get_object),'private-originals'))
    return body,calls


def test_private_original_key_is_hash_only_and_stream_is_closed(tmp_path,monkeypatch):
    body,calls=client(monkeypatch)
    target=tmp_path/'original'
    assert original_storage.download_saved_original('A'*64,target,20)
    assert target.read_bytes()==b'original'
    assert calls==[{'Bucket':'private-originals','Key':'sha256/'+'a'*64}]
    assert body.closed


@pytest.mark.parametrize('payload,advertised',[(b'original',50),(b'x'*50,5),(b'x',5)])
def test_size_limits_and_truncation_cleanup(tmp_path,monkeypatch,payload,advertised):
    body,_=client(monkeypatch,payload,advertised)
    target=tmp_path/'original'
    with pytest.raises(original_storage.OriginalStorageUnavailable):
        original_storage.download_saved_original('a'*64,target,20)
    assert not target.exists()
    assert body.closed


def test_missing_object_falls_back_without_disclosing_provider_errors(tmp_path,monkeypatch):
    client(monkeypatch,error=Missing())
    assert not original_storage.download_saved_original('a'*64,tmp_path/'original',20)
    client(monkeypatch,error=ValueError('private access key should never appear'))
    with pytest.raises(original_storage.OriginalStorageUnavailable) as error:
        original_storage.download_saved_original('a'*64,tmp_path/'original',20)
    assert 'private access key' not in str(error.value)


def test_unconfigured_storage_and_unregistered_hash_make_no_network_call(tmp_path,monkeypatch):
    monkeypatch.setattr(original_storage,'_client',lambda:(None,''))
    assert not original_storage.download_saved_original('a'*64,tmp_path/'original',20)
    monkeypatch.setattr(original_storage,'_client',lambda:pytest.fail('Network access attempted'))
    assert not original_storage.download_saved_original('../../unregistered',tmp_path/'original',20)
