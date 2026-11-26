import hashlib
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from backend import llm_limiter
from backend.ai_budget import REFUSAL_MESSAGES, AIBudget
from backend.audit import numbers, untraceable
from backend.config import ROOT, rules, rules_view
from backend.database import Store, uid, utcnow
from backend.llm_limiter import QueueWaitExceeded
from backend.providers.base import SEND_METER, HTTPProvider, ProviderUnavailable, SendMeter
from backend.schemas import ROLE_SCHEMAS, SearchResult
from backend.security import screen_inputs

PROMPTS_FILE = ROOT / 'config' / 'prompts.json'
# Roles whose text may cite only numbers present in their inputs (or the numeric allowlist).
NUMERIC_ROLES = frozenset({'chief_analyst', 'auditor', 'market_interpreter'})
# Roles whose external input text has instruction-like sentences removed before the call (not only reported).
REDACTING_ROLES = frozenset({'market_interpreter'})
# Roles that never run a second opinion: their second result would be discarded (only the auditor uses one).
SINGLE_OPINION_ROLES = frozenset({'market_interpreter'})
# Roles that run only on their assigned models (primary, backup): no automatic fallback model is ever added, so
# clearing the assignment stops the role and a failed run never goes on to a third model (measured: 'kimi-k3' ran as
# an unassigned fallback after the primary and the backup had both failed on the same input, 6 requests per run).
ASSIGNED_ONLY_ROLES = frozenset({'market_interpreter'})
# What a failed attempt was: 'schema' (not JSON, or not the schema's shape), 'content' (a schema-valid output the
# evidence, number or role checks rejected) or 'truncated' (cut at the output token cap).
SCHEMA, CONTENT, TRUNCATED = 'schema', 'content', 'truncated'
DEFAULT_VALIDATION: dict[str, Any] = {'corrections': 1, 'correct': (SCHEMA, CONTENT), 'next_model_after_invalid': True}


class OutputTruncated(ValueError):
    """The provider stopped at the output token cap (Ollama done_reason 'length'): the answer is incomplete."""


@dataclass(frozen=True)
class ChatResult:
    """Vendor-neutral chat response; business logic never parses provider-specific fields. tokens = completion tokens
    (Ollama eval_count), prompt_tokens = prompt tokens (Ollama prompt_eval_count), when the provider reports them."""
    content: str
    tokens: int | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    finish: str | None = None  # why the generation stopped, when the provider says (Ollama done_reason: stop | length)


@runtime_checkable
class LLMProvider(Protocol):
    async def chat(self, model: str, messages: list[dict], schema: dict) -> 'ChatResult | dict': ...


def chat_options(provider: Any, role: str, queue_role: str | None = None) -> dict:
    """Per-role chat timeout/attempts (config ai_runtime) and the role that places the call in the LLM limiter queue
    (queue_role when given, e.g. 'integration_probe' for the Settings test), for providers whose chat() accepts them
    (test doubles may not)."""
    try:
        parameters = inspect.signature(provider.chat).parameters
    except (TypeError, ValueError):
        return {}
    config = rules_view().get('ai_runtime') or {}
    options = {'timeout': (config.get('chat_timeout_seconds') or {}).get(role) or (config.get('chat_timeout_seconds') or {}).get('default'),
               'attempts': (config.get('chat_attempts') or {}).get(role) or (config.get('chat_attempts') or {}).get('default'),
               'role': queue_role or role}
    return {key: value for key, value in options.items() if value is not None and key in parameters}


def generation_options(provider: Any, role: str, registered: dict | None = None) -> dict:
    """Per-role generation limits (config ai_runtime.generation) for providers whose chat() accepts them: max_tokens
    (the cap on the answer's tokens, Ollama options.num_predict) and think (false switches the model's reasoning phase
    off; sent only for a model the registry lists as thinking-capable, since another model may reject the parameter).
    Without a setting nothing is passed and the provider's defaults apply."""
    try:
        parameters = inspect.signature(provider.chat).parameters
    except (TypeError, ValueError):
        return {}
    settings = ((rules_view().get('ai_runtime') or {}).get('generation') or {}).get(role)
    if not isinstance(settings, dict):
        return {}
    options: dict[str, Any] = {}
    cap = settings.get('max_output_tokens')
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        options['max_tokens'] = cap
    if isinstance(settings.get('think'), bool) and (registered or {}).get('thinking_supported') is True:
        options['think'] = settings['think']
    return {key: value for key, value in options.items() if key in parameters}


def validation_policy(role: str) -> dict[str, Any]:
    """How a rejected answer is handled for `role` (config ai_runtime.validation, the role's entry over `default`):
    corrections = correction attempts per model (the model is shown its answer and the error), correct = the kinds of
    error worth one ('schema', 'content'), next_model_after_invalid = whether the next model then gets the same prompt.
    The interpreter corrects only a schema-shape error and never passes a rejected input on to the backup model: a
    blind second request with the same prompt failed the same way (docs/AI_AGENTS.md)."""
    config = (rules_view().get('ai_runtime') or {}).get('validation') or {}
    policy = dict(DEFAULT_VALIDATION)
    for source in (config.get('default'), config.get(role)):
        if isinstance(source, dict):
            policy.update({key: source[key] for key in DEFAULT_VALIDATION if key in source})
    count = policy['corrections']
    policy['corrections'] = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 1
    policy['correct'] = tuple(str(kind) for kind in policy['correct'] or ())
    return policy


def json_text(content: str) -> str:
    """The JSON object of an answer that wraps it in a markdown fence or a sentence (a fenced json block, 'Here is the
    JSON: {...}'): the text from the first '{' to the last '}'. An answer that already starts with '{', or holds no
    object at all, is returned unchanged (and fails the schema as before)."""
    text = content.strip()
    if text.startswith('{'):
        return text
    start, end = text.find('{'), text.rfind('}')
    return text[start:end + 1] if 0 <= start < end else content


def failure_kind(exc: BaseException) -> str:
    """'truncated', 'schema' (not JSON or not the schema's shape) or 'content' (rejected by a check) of a failed
    attempt."""
    if isinstance(exc, OutputTruncated):
        return TRUNCATED
    if isinstance(exc, (ValidationError, json.JSONDecodeError, KeyError, TypeError)):
        return SCHEMA
    return CONTENT


def count_tokens(metadata: dict, response: ChatResult) -> None:
    """Adds the provider's prompt and completion token counts of one answer to the ai_runs row (both stay None when the
    provider reports neither)."""
    for key, value in (('prompt_tokens', response.prompt_tokens), ('completion_tokens', response.tokens)):
        if isinstance(value, int) and not isinstance(value, bool):
            metadata[key] = (metadata.get(key) or 0) + value


def sent_requests(meter: SendMeter, failure: BaseException | None) -> int:
    """HTTP requests one chat call sent to the provider (the daily AI budget counts them): every transport retry and
    every limiter retry after a 429/5xx sends the whole prompt again, and a call refused before sending (circuit open,
    not configured, no limiter slot in time) sent nothing. A provider that is not an HTTPProvider (a test double) is
    counted as one request unless it gave up waiting for a limiter slot."""
    if meter.seen:
        return meter.sends
    return 0 if isinstance(failure, QueueWaitExceeded) else 1


def normalize(response: 'ChatResult | dict') -> ChatResult:
    if isinstance(response, ChatResult):
        return response
    # Legacy Ollama-shaped dicts (and test doubles) are converted at the boundary.
    content = response['message']['content']
    return ChatResult(content=content, tokens=response.get('eval_count'), model=response.get('model'),
                      prompt_tokens=response.get('prompt_eval_count'), finish=response.get('done_reason'))


_prompt_cache: tuple[int, dict] | None = None


def prompts() -> dict:
    global _prompt_cache
    stamp = PROMPTS_FILE.stat().st_mtime_ns
    if not _prompt_cache or _prompt_cache[0] != stamp:
        _prompt_cache = (stamp, json.loads(PROMPTS_FILE.read_text(encoding='utf-8')))
    return _prompt_cache[1]


def numeric_allowlist(role: str) -> list[str]:
    """Bare numbers a role may write without an input source: a role-specific list (role_numeric_allowlist) replaces
    the general one."""
    registry = prompts()
    return list((registry.get('role_numeric_allowlist') or {}).get(role, registry['numeric_allowlist']))


def system_prompt(role: str, schema: dict) -> tuple[str, str, str]:
    """Role-specific versioned prompt; the hash covers prompt text and schema so any change is traceable."""
    registry = prompts()
    text = registry['roles'][role] + '\n' + registry['preamble'] + json.dumps(schema, sort_keys=True)
    return text, registry['prompts_version'], hashlib.sha256(text.encode()).hexdigest()


class OllamaProvider(HTTPProvider):
    def __init__(self, base_url: str, key: str):
        super().__init__('ollama', base_url, key, bool(key), timeout=60)
        self.catalog = HTTPProvider('ollama_catalog', base_url, timeout=20)
        # web_search/web_fetch have their own circuit: a 429 or a timeout of a search pauses only searches, never the
        # chat calls of every role (it did: one search 429 refused all chat with 'circuit open').
        self.web = HTTPProvider('ollama_web', base_url, key, bool(key), timeout=60)

    async def _web(self, path: str, payload: dict) -> Any:
        """A web_search/web_fetch request. The key is shared with chat, so a 401/403 also stops chat: the auth error
        and its circuit are copied to this (the 'ollama') provider."""
        try:
            return (await self.web.request('POST', path, headers=self.headers, json=payload)).json()
        except ProviderUnavailable as exc:
            if exc.status_code in {401, 403}:
                self.health.update(status=self.web.health['status'], error=self.web.health['error'],
                                   error_type=self.web.health['error_type'])
                self.next_attempt = max(self.next_attempt, self.web.next_attempt)
            raise

    @property
    def headers(self) -> dict:
        return {'Authorization': f'Bearer {self.key}'}

    async def models(self) -> list[dict]:
        body = (await self.catalog.request('GET', '/tags')).json()
        if not isinstance(body, dict) or not isinstance(body.get('models'), list):
            raise ValueError('Invalid Ollama model catalog')
        if any(not isinstance(m, dict) or not isinstance(m.get('name'), str) or not m['name'] for m in body['models']):
            raise ValueError('Invalid Ollama model entry')
        return body['models']

    async def model_info(self, model: str) -> dict:
        body = (await self.catalog.request('POST', '/show', json={'model': model})).json()
        if not isinstance(body, dict):
            raise ValueError('Invalid Ollama model metadata')
        return body

    async def chat(self, model: str, messages: list[dict], schema: dict, *, timeout: float | None = None,
                   attempts: int = 2, role: str | None = None, max_tokens: int | None = None,
                   think: bool | None = None) -> ChatResult:
        # Cloud does not support format/schema enforcement. Validate strictly on return. The call waits for a slot in
        # the shared LLM limiter (the timeout starts only when it is sent). A slow generation or an HTTP 429 never
        # opens the provider circuit (other roles keep working; the limiter backs off on 429); 401/403 still do.
        # max_tokens caps the answer (options.num_predict) and think=False switches the reasoning phase off, for the
        # roles that set them (ai_runtime.generation): reasoning tokens are generated, and paid for, like the answer.
        options: dict[str, Any] = {'temperature': 0}
        if max_tokens:
            options['num_predict'] = int(max_tokens)
        payload: dict[str, Any] = {'model': model, 'messages': messages, 'stream': False, 'options': options}
        if think is not None:
            payload['think'] = think
        response = (await llm_limiter.LIMITER.run(role, lambda: self.request(
            'POST', '/chat', headers=self.headers, json=payload, timeout=timeout, attempts=attempts,
            transport_opens_circuit=False, rate_limit_opens_circuit=False), self.health)).json()
        if not isinstance(response, dict) or not isinstance(response.get('message'), dict):
            raise ValueError('Invalid Ollama chat response')
        if response.get('done') is False:
            raise ValueError('Incomplete Ollama chat response')
        content = response['message'].get('content')
        if not isinstance(content, str):
            raise ValueError('Invalid Ollama chat content')
        return ChatResult(content=content, tokens=response.get('eval_count'), model=response.get('model', model),
                          prompt_tokens=response.get('prompt_eval_count'), finish=response.get('done_reason'))

    async def search(self, query: str) -> list[dict]:
        """Discovery only: results are validated, never used as prices or as confirmation."""
        body = await self._web('/web_search', {'query': query, 'max_results': rules()['search_budget']['max_results']})
        rows = body.get('results', []) if isinstance(body, dict) else []
        valid = []
        for row in rows if isinstance(rows, list) else []:
            try:
                valid.append(SearchResult.model_validate(row).model_dump(mode='json'))
            except ValidationError:
                continue
        return valid

    async def fetch(self, url: str) -> dict:
        """Ollama web_fetch: page text for deep research of a discovered URL (discovery only)."""
        body = await self._web('/web_fetch', {'url': url})
        if not isinstance(body, dict):
            raise ValueError('Invalid web_fetch response')
        return SearchResult.model_validate({'title': body.get('title') or '', 'url': url,
                                            'content': (body.get('content') or '')[:20000]}).model_dump(mode='json')


class OpenAIProvider(HTTPProvider):
    """Optional vendor behind the same abstraction. NOT CONFIGURED without OPENAI_API_KEY."""

    def __init__(self, base_url: str, key: str):
        super().__init__('openai', base_url, key, bool(key), timeout=60)

    @property
    def headers(self) -> dict:
        return {'Authorization': f'Bearer {self.key}'}

    async def models(self) -> list[dict]:
        body = (await self.request('GET', '/models', headers=self.headers)).json()
        rows = body.get('data') if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ValueError('Invalid OpenAI model list')
        return [{'name': row['id']} for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)]

    async def model_info(self, model: str) -> dict:
        return {}

    async def chat(self, model: str, messages: list[dict], schema: dict, *, timeout: float | None = None,
                   attempts: int = 2, role: str | None = None) -> ChatResult:
        payload = {'model': model, 'messages': messages, 'temperature': 0, 'response_format': {'type': 'json_object'}}
        body = (await llm_limiter.LIMITER.run(role, lambda: self.request(
            'POST', '/chat/completions', headers=self.headers, json=payload, timeout=timeout, attempts=attempts,
            transport_opens_circuit=False, rate_limit_opens_circuit=False), self.health)).json()
        try:
            content = body['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            raise ValueError('Invalid OpenAI chat response') from None
        if not isinstance(content, str):
            raise ValueError('Invalid OpenAI chat content')
        usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
        return ChatResult(content=content, tokens=usage.get('completion_tokens'), model=model,
                          prompt_tokens=usage.get('prompt_tokens'))


class SearchBudgetManager:
    """Hourly web-search allowance with P0-P3 precedence and per-priority topic cooldowns (spec §8). The cooldown of
    P0/P1 topics is at least refresh.hot_topics.recheck_hours: the research planner behind a search is an AI request,
    and the same event was re-planned every minute (docs/AI_AGENTS.md)."""
    STATE_KEY = 'search-budget-state'

    def __init__(self, hourly: int, cooldown: int, store: Store | None = None):
        self.hourly, self.cooldown, self.store = hourly, cooldown, store
        self.calls: list[float] = []
        self.topics: dict[str, float] = {}
        self.loaded = store is None

    def _load(self) -> None:
        """Persisted state is read lazily: the service is built before its tables exist."""
        if self.loaded or self.store is None:
            return
        state = self.store.get('research_jobs', self.STATE_KEY) or {}
        self.calls = [float(t) for t in state.get('calls', [])] + self.calls
        self.topics = {**{k: float(v) for k, v in state.get('topics', {}).items()}, **self.topics}
        self.loaded = True

    def acquire(self, topic: str, priority: int, *, fresh_evidence: bool = False) -> bool:
        """fresh_evidence: the topic received new evidence since its last research (Runtime.research_due), so its
        cooldown is waived; the hourly allowance still applies."""
        self._load()
        config = rules()
        policy = config['search_budget']['priorities'][str(min(max(priority, 0), 3))]
        now = time.time()
        self.calls = [t for t in self.calls if t > now - 3600]
        allowance = int(self.hourly * policy['share'])
        recheck = float((config['refresh'].get('hot_topics') or {}).get('recheck_hours', 0)) * 3600
        cooldown = max(self.cooldown, policy['cooldown_seconds']) if priority >= 2 else max(self.cooldown, recheck)
        cooling = not fresh_evidence and now - self.topics.get(topic, -float('inf')) < cooldown
        if len(self.calls) >= allowance or cooling:
            return False
        self.calls.append(now)
        self.topics[topic] = now
        if self.store:
            recent = {k: v for k, v in self.topics.items() if v > now - 86400}
            self.store.put('research_jobs', self.STATE_KEY, {'calls': self.calls, 'topics': recent,
                                                             'updated_at': utcnow().isoformat(), 'kind': 'BUDGET_STATE'})
        return True


class AIService:
    def __init__(self, store: Store, provider: LLMProvider):
        self.store, self.provider = store, provider
        self.budget = AIBudget(store)  # daily request budget shared by every role and model (docs/AI_AGENTS.md)

    def audit(self, action: str, **fields) -> None:
        self.store.put('audit_logs', uid(), dict(action=action, timestamp=utcnow().isoformat(), **fields))

    async def discover(self) -> list[dict]:
        """Refresh the model registry from the provider catalog; changes are logged, never silent."""
        list_models = getattr(self.provider, 'models', None)
        model_info = getattr(self.provider, 'model_info', None)
        if list_models is None or model_info is None or not callable(list_models):
            return []
        models = await list_models()
        names = {m['name'] for m in models}
        for old in self.store.list('ai_models'):
            if old['name'] not in names and old.get('availability'):
                self.store.put('ai_models', old['name'], dict(old, availability=False, status='UNAVAILABLE'))
                self.audit('model_unavailable', model=old['name'])
        for row in models:
            name = row['name']
            existing = self.store.get('ai_models', name) or {}
            info = existing.get('provider_metadata') or {}
            if not info or existing.get('digest') != row.get('digest'):
                try:
                    info = await model_info(name)
                except (ProviderUnavailable, ValueError):
                    info = info or {}
            if not existing:
                self.audit('model_discovered', model=name)
            capabilities = info.get('capabilities', row.get('capabilities', []))
            context_window = next((v for k, v in (info.get('model_info') or {}).items() if k.endswith('.context_length')), None)
            self.store.put('ai_models', name, {
                'name': name, 'model_id': name, 'display_name': name, 'capabilities': capabilities,
                'tools_supported': 'tools' in capabilities if info else None,
                'thinking_supported': 'thinking' in capabilities if info else None,
                'context_window': context_window, 'availability': True, 'last_seen': utcnow().isoformat(),
                'status': 'AVAILABLE', 'preferred_roles': existing.get('preferred_roles', []),
                'digest': row.get('digest'), 'provider_metadata': info})
        return self.store.list('ai_models')

    def candidates(self, role: str, assignment: dict, model_override: str | None) -> list[tuple[str, str]]:
        if model_override:
            return [(model_override, 'override')]
        chosen = [(m, kind) for kind in ('primary', 'backup') if (m := assignment.get(kind))]
        if role in ASSIGNED_ONLY_ROLES:
            unique: dict[str, str] = {}
            for model, kind in chosen:
                unique.setdefault(model, kind)
            return list(unique.items())
        assigned = {m for m, _ in chosen}
        # Automatic fallback: other available models already validated for this role, logged when used.
        eligible = sorted(m['name'] for m in self.store.list('ai_models')
                          if m.get('availability') and role in m.get('preferred_roles', []) and m['name'] not in assigned)
        ordered: dict[str, str] = {}
        for model, kind in chosen + [(m, 'fallback') for m in eligible]:
            ordered.setdefault(model, kind)
        return list(ordered.items())

    def ready(self) -> bool:
        """False while the bound LLM provider is switched off or has no credentials: nothing is called or recorded."""
        return getattr(self.provider, 'enabled', True) is not False and getattr(self.provider, 'configured', True) is not False

    def available(self, role: str) -> bool:
        """True when some assigned or fallback model for the role is registered and available (no call is made). A
        role whose daily budget cap (or the whole daily budget) is 0 is never available."""
        if not self.ready() or self.budget.disabled(role):
            return False
        assignment = self.store.get('ai_role_assignments', role) or {}
        return any((self.store.get('ai_models', model) or {}).get('availability')
                   for model, _ in self.candidates(role, assignment, None))

    def allowed(self, role: str) -> bool:
        """available() and the daily budget would send a request of this role now (callers that would spend another
        resource first, such as a search-budget slot, check this)."""
        return self.available(role) and self.budget.refusal(role) is None

    async def run(self, role: str, inputs: dict, *, model_override: str | None = None,
                  check: Callable[[dict], None] | None = None, queue_role: str | None = None,
                  repair: Callable[[Any], tuple[Any, list[str]]] | None = None) -> dict | None:
        return (await self.run_with_id(role, inputs, model_override=model_override, check=check,
                                       queue_role=queue_role, repair=repair))[0]

    async def run_with_id(self, role: str, inputs: dict, *, model_override: str | None = None,
                          check: Callable[[dict], None] | None = None, queue_role: str | None = None,
                          repair: Callable[[Any], tuple[Any, list[str]]] | None = None) -> tuple[dict | None, str | None]:
        """`check` is a role-specific validation of the parsed output against its inputs; it raises ValueError (a
        'content' failure). `repair` receives the parsed JSON before the schema is applied and returns (the answer with
        its unusable parts removed, notes of what it removed); it raises ValueError when nothing usable is left.
        `queue_role` places the chat calls in the LLM limiter queue under another key (its priority and max wait) than
        the role itself. A rejected answer is corrected and passed to the next model as the role's validation policy
        says (validation_policy): by default one corrected retry per model, then the next model."""
        if not self.ready():
            return None, None
        schema = ROLE_SCHEMAS[role]
        json_schema = schema.model_json_schema()
        assignment = self.store.get('ai_role_assignments', role) or {}
        screened, findings = screen_inputs(inputs, redact=role in REDACTING_ROLES)
        input_text = json.dumps(screened, sort_keys=True, default=str)
        if len(input_text) > rules()['search_budget']['max_llm_input_chars']:
            raise ValueError('AI context exceeds application limit')
        prompt, prompt_version, prompt_hash = system_prompt(role, json_schema)
        allowlist = set(numeric_allowlist(role)) | numbers(json.dumps(rules()['assets']))
        policy = validation_policy(role)
        run_id = None
        for model, basis in self.candidates(role, assignment, model_override):
            registered = self.store.get('ai_models', model)
            if not registered or not registered.get('availability'):
                continue
            started = time.monotonic()
            meter = llm_limiter.QueueMeter()  # this model's wait for limiter slots, over both attempts
            run_id = uid()
            # requests: HTTP chat requests of this row that reached the provider (the daily budget counts them).
            metadata: dict[str, Any] = {
                'agent': role, 'model': model, 'model_basis': basis, 'model_digest': registered.get('digest'),
                'prompt_version': prompt_version, 'prompt_hash': prompt_hash, 'input_hash': hashlib.sha256(input_text.encode()).hexdigest(),
                'input_chars': len(input_text),
                'injection_findings': findings, 'timestamp': utcnow().isoformat(), 'success': False, 'output': None, 'tokens': None,
                'requests': 0}
            messages = [{'role': 'system', 'content': prompt},
                        {'role': 'user', 'content': json.dumps({'untrusted_input_data': screened}, default=str)}]
            options = {**chat_options(self.provider, role, queue_role), **generation_options(self.provider, role, registered)}
            admitted = 0  # requests this row reserved in the daily budget, given back once the row is stored
            failures: list[dict[str, str]] = []  # one {kind, error} per rejected attempt of this model
            try:
                for attempt in range(1 + policy['corrections']):
                    # The daily budget is checked before the limiter queue: a refused call sends nothing and the
                    # caller's own fallback runs. Primary, backup and the correction attempt share it.
                    refusal = self.budget.admit(role)
                    if refusal is not None:
                        metadata.update(error=REFUSAL_MESSAGES[refusal], budget_refusal=refusal)
                        break
                    admitted += 1
                    try:
                        sends = SendMeter()
                        meter_token, send_token = llm_limiter.QUEUE_METER.set(meter), SEND_METER.set(sends)
                        failure: BaseException | None = None
                        try:
                            reply = await self.provider.chat(model, messages, json_schema, **options)
                        except BaseException as exc:
                            failure = exc
                            raise
                        finally:
                            SEND_METER.reset(send_token)
                            llm_limiter.QUEUE_METER.reset(meter_token)
                            metadata['requests'] += sent_requests(sends, failure)
                        response = normalize(reply)
                        count_tokens(metadata, response)
                        metadata['raw_output'] = response.content[:40000]
                        if response.finish == 'length':
                            raise OutputTruncated('The answer was cut at the output token cap')
                        text = json_text(response.content)
                        if repair is None:
                            output = schema.model_validate_json(text, strict=True).model_dump(mode='json')
                        else:
                            repaired, notes = repair(json.loads(text))
                            if notes:
                                metadata['repairs'] = [str(note)[:200] for note in notes[:20]]
                            output = schema.model_validate(repaired, strict=True).model_dump(mode='json')
                        # Every reference must be supplied by the backend; hallucinated IDs fail closed.
                        allowed = set(inputs.get('evidence_ids', []))
                        references = list(output.get('evidence_ids', []))
                        for group in ('hypotheses', 'pairs', 'assets', 'developments', 'relations', 'next_events'):
                            references += [ref for item in output.get(group, []) if isinstance(item, dict)
                                           for ref in item.get('evidence_ids', [])]
                        references += [r['evidence_id'] for r in output.get('relations', [])]
                        if any(ref not in allowed for ref in references):
                            raise ValueError('Unknown evidence reference')
                        if role in NUMERIC_ROLES:
                            # Cited values must come from the inputs; the model may not compute or invent numbers.
                            invented = untraceable(output, screened, allowlist)
                            if invented:
                                raise ValueError(f'Unsupported numerical claim in AI output: {sorted(invented)[:10]}')
                        if check is not None:
                            check(output)
                        metadata.update(success=True, output=output, tokens=response.tokens)
                        break
                    except (ValidationError, ValueError, KeyError, TypeError) as exc:
                        kind = failure_kind(exc)
                        metadata['error'] = type(exc).__name__
                        metadata['validation_errors'] = exc.errors(include_input=False, include_url=False, include_context=False) if isinstance(exc, ValidationError) else str(exc)[:500]
                        metadata['failure'] = kind
                        reason = json.dumps(metadata['validation_errors'], default=str)
                        repeated = any(item['error'] == reason for item in failures)
                        failures.append({'kind': kind, 'error': reason})
                        metadata['attempt_errors'] = [dict(item, error=item['error'][:300]) for item in failures]
                        # One correction, only for a kind of error the role's policy says is worth it, never for a
                        # reason the model was already asked to correct.
                        if attempt >= policy['corrections'] or kind not in policy['correct'] or repeated:
                            break
                        if isinstance(metadata.get('raw_output'), str):
                            messages.append({'role': 'assistant', 'content': metadata['raw_output']})
                        messages.append({'role': 'user', 'content': prompts()['correction'].replace('{errors}', reason)})
                    except ProviderUnavailable as exc:
                        metadata['error'] = 'ProviderUnavailable'
                        metadata['failure'] = 'provider'
                        metadata['http_status'] = exc.status_code
                        metadata['error_detail'] = str(exc)[:200]  # 'Rate limited (HTTP 429)', 'LLM queue wait exceeded 600 s'
                        if isinstance(exc, QueueWaitExceeded):  # no slot in time (for this send or a 429 retry)
                            metadata['queue_wait_exceeded'] = True
                        if exc.status_code == 404:
                            self.store.put('ai_models', model, dict(registered, availability=False, status='UNAVAILABLE'))
                            self.audit('model_unavailable', model=model, role=role)
                        break
                # latency_ms is the provider time; the wait for a limiter slot is stored on its own.
                metadata['queue_wait_ms'] = round(meter.seconds * 1000, 2)
                metadata['latency_ms'] = round(max(0.0, time.monotonic() - started - meter.seconds) * 1000, 2)
                if metadata.get('budget_refusal') and admitted == 0:
                    # Nothing was sent: at most one refusal row per role per hour, later refusals counted in it.
                    run_id = self.budget.record_refusal(role, metadata['budget_refusal'], metadata, run_id)
                else:
                    self.store.put('ai_runs', run_id, metadata)
            finally:
                self.budget.settle(role, admitted)
            if metadata.get('queue_wait_exceeded') or metadata.get('budget_refusal'):
                # Every model of the role waits in the same queue and shares the same budget: the caller's own
                # fallback applies now.
                return None, run_id
            if not metadata['success']:
                if metadata.get('failure') in {SCHEMA, CONTENT, TRUNCATED} and (
                        metadata['failure'] == TRUNCATED or not policy['next_model_after_invalid']):
                    # The same prompt is not sent blind to another model; the caller's own fallback applies.
                    return None, run_id
                continue
            if basis in {'backup', 'fallback'}:
                self.audit('model_fallback', role=role, model=model, basis=basis, run_id=run_id)
            if role not in registered.get('preferred_roles', []):
                self.store.put('ai_models', model, dict(registered, preferred_roles=[*registered.get('preferred_roles', []), role]))
            second_model = assignment.get('second_opinion') if role not in SINGLE_OPINION_ROLES else None
            if not model_override and second_model and second_model != model:
                second, second_id = await self.run_with_id(role, inputs, model_override=second_model, check=check,
                                                           queue_role=queue_role, repair=repair)
                self.audit('second_opinion', role=role, model=second_model, success=second is not None, run_id=second_id)
                if role == 'auditor' and (not second or second['status'] != 'PASS'):
                    return {'status': 'BLOCK', 'reasons': ['Second-opinion auditor did not pass'], 'checks': []}, run_id
            return metadata['output'], run_id
        return None, run_id
