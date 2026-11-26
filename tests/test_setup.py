from scripts.init_local import prepare


def test_local_setup_fills_empty_secrets_without_overwriting_key(tmp_path):
    path=tmp_path/'.env'
    path.write_text('ADMIN_TOKEN=\nPOSTGRES_PASSWORD=\nOLLAMA_API_KEY=test-value\n')
    assert set(prepare(path))=={'ADMIN_TOKEN','POSTGRES_PASSWORD'}
    values=dict(line.split('=',1) for line in path.read_text().splitlines())
    assert len(values['ADMIN_TOKEN'])>=32
    assert values['OLLAMA_API_KEY']=='test-value'
    before=path.read_text()
    assert prepare(path)==[]
    assert path.read_text()==before
