import pytest


@pytest.fixture(autouse=True)
def isolate_secrets(monkeypatch):
    # Tests must never consume production credentials from the local dotenv file.
    for name in ['OLLAMA_API_KEY','MASSIVE_API_KEY','FMP_API_KEY','X_BEARER_TOKEN',
                 'REUTERS_API_KEY','AP_API_KEY','ADMIN_TOKEN','REDIS_URL','OPENAI_API_KEY','CONGRESS_API_KEY',
                 'FINNHUB_API_KEY','FRED_API_KEY','BLS_API_KEY','TELEGRAM_BOT_TOKEN','SMTP_PASSWORD',
                 'WEBPUSH_VAPID_PRIVATE_KEY','ALPACA_API_KEY_ID','ALPACA_API_SECRET_KEY']:
        monkeypatch.setenv(name, '')
    # Existing API tests exercise the token path; local (passwordless) administration has its own tests.
    monkeypatch.setenv('ADMIN_AUTH', 'token')
    from backend.config import get_settings
    from backend.llm_limiter import LIMITER
    get_settings.cache_clear()
    LIMITER.reset()  # the process-wide LLM limiter: no pause, limit or health binding carried between tests
    yield
    get_settings.cache_clear()
    LIMITER.reset()


@pytest.fixture
def config_copy(tmp_path, monkeypatch):
    """A private copy of config/ for tests that write configuration (PUT /api/admin/config/...): the owner's running
    app re-reads config/rules.d by file mtime about once a second, so a test writing the real files briefly changed its
    live settings (and left them changed if the run was killed). Yields the copy's directory."""
    import shutil

    from backend import api_admin, config
    target = tmp_path / 'config-copy'
    shutil.copytree(config.CONFIG_DIR, target)
    monkeypatch.setattr(config, 'CONFIG_DIR', target)
    monkeypatch.setattr(api_admin, 'CONFIG_DIR', target)
    config.reload_rules()
    yield target
    monkeypatch.undo()
    config.reload_rules()
