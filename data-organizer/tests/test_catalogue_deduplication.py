from pathlib import Path


def test_reviewed_catalogue_counts_exports_and_api_group_exact_copies(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'vendor_dashboard'))
    from storage import Store
    from vendor_core import Decision
    store = Store(tmp_path)
    row = {'record_type':'catalogue','product_name':'Wheelchair','sku':'W1','price':5000,
           'currency':'INR','specification':'Steel frame'}
    for number in range(2):
        content = ('Synthetic catalogue '+str(number)).encode()
        path = f'Alpha Medical/catalogue-{number}.txt'
        store.save_document(path,content,Decision('Alpha Medical',('Catalogue',),'Test',''))
        doc = store.document_id_for(path,content)
        records = [{**row,'source_row':number+1}]
        if number:
            records += [{**row,'price':6000},{**row,'specification':'Aluminium frame'}]
        store.save_product_records(doc,records)
    with store.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM vdd_product_records').fetchone()['n'] == 4
    assert store.product_record_count() == 3
    assert len(store.product_records()) == 3
    assert store.product_record_count('Wheelchair') == 3
    assert len(store.product_records(limit=1,offset=2)) == 1
    rows, total = store.published_catalogue_rows()
    assert total == len(rows) == 3
    assert {row['price'] for row in rows} == {5000,6000}
