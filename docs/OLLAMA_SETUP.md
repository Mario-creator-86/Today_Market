# Ollama Cloud integration

The backend uses the official native API at `https://ollama.com/api`.

- `GET /tags`: public dynamic model catalog. Catalog availability is separate from authenticated inference health.
- `POST /show`: discovers capabilities, thinking metadata and context length when supplied by the service.
- `POST /chat`: server-side `Authorization: Bearer` header, non-streaming response, no browser key.
- `POST /web_search`: authenticated discovery; returned snippets are never confirmed facts or market quotes.

Set `OLLAMA_API_KEY` in the root `.env` (gitignored), then run:

```powershell
.venv\Scripts\python -m scripts.check_ollama --configure-roles
```

The diagnostic discovers models, selects small available models from returned metadata, validates real audit, extraction and research-planning JSON responses, and checks web search. It saves a redacted report to `artifacts/ollama-integration.json`. `--configure-roles` fills only unassigned roles using tested primary/backup models and records the change in `audit_logs`. Existing role assignments are preserved. Restart the backend after changing environment credentials.

The Settings page permits explicit primary, backup and second-opinion selection. Administrative writes require `ADMIN_TOKEN` configured server-side and entered in the UI; it is held in memory, not browser storage.

`POST /api/admin/ollama/test` with the `X-Admin-Token` header runs a minimal auditor probe through the live application's provider instance. Success means authenticated inference produced schema-valid BLOCK for a deliberately data-empty input. It never publishes a setup.

Cloud currently does not enforce JSON schema through structured output mode. The application supplies the schema in its system message, then validates the returned JSON strictly with Pydantic. One schema-correction retry is allowed; malformed output fails closed. No arbitrary prose parser, tool execution or AI-driven score changes are allowed. Model-specific HTTP 404 does not open a provider-wide circuit and prevent backup selection. Invalid catalog responses do not retire valid models.

Official references:
- [Cloud API and model names](https://docs.ollama.com/cloud)
- [Authentication](https://docs.ollama.com/api/authentication)
- [Chat](https://docs.ollama.com/api/chat)
- [Structured output limitations](https://docs.ollama.com/capabilities/structured-outputs)
- [Web search](https://docs.ollama.com/capabilities/web-search)
