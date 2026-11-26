"""Safe live integration diagnostic. Never prints headers, keys or raw failures."""
import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path

from backend.ai import AIService, OllamaProvider
from backend.config import ROOT, get_settings
from backend.database import Store, uid, utcnow
from backend.schemas import ROLE_SCHEMAS


def size(model: dict) -> float:
    value = str(model.get('provider_metadata', {}).get('details', {}).get('parameter_size', ''))
    match = re.search(r'([\d.]+)\s*([BM])', value, re.I)
    return float(match[1]) * (1 if match[2].upper() == 'B' else .001) if match else float('inf')


async def check(configure: bool = False) -> dict:
    settings = get_settings()
    provider = OllamaProvider(settings.ollama_base_url, settings.ollama_api_key.get_secret_value())
    report: dict = {'checked_at': utcnow().isoformat(), 'base_url': settings.ollama_base_url,
                    'key_configured': provider.configured, 'checks': {}}
    with tempfile.TemporaryDirectory(prefix='hub-ollama-') as temp:
        store = Store(f'sqlite:///{Path(temp).as_posix()}/probe.db')
        store.initialize()
        service = AIService(store, provider)
        try:
            models = await service.discover()
            available = sorted([m for m in models if m.get('availability')], key=size)
            report['checks']['catalog'] = {'status': 'PASS', 'models': len(available),
                'metadata_discovered': sum(bool(m.get('provider_metadata')) for m in available)}
            if not provider.configured:
                report['status'] = 'NEEDS_API_KEY'
                return report
            validated = []
            for model in available[:3]:
                store.put('ai_role_assignments', 'auditor', {'primary': model['name']})
                result = await service.run('auditor', {'integration_probe': True,
                    'instruction_context': 'No prices, facts or trading setup are provided. Publication must be blocked.',
                    'evidence_ids': []})
                if result and result['status'] == 'BLOCK':
                    validated.append(model['name'])
                    if len(validated) == 2:
                        break
            report['checks']['chat_and_schema'] = {'status': 'PASS' if validated else 'FAIL', 'validated_models': validated}
            if validated:
                store.put('ai_role_assignments', 'extractor', {'primary': validated[0], 'backup': validated[1] if len(validated)>1 else None})
                extraction = await service.run('extractor', {'integration_probe':True, 'document':
                    'Integration test document: Example organization announces a software documentation update. No market claims.',
                    'evidence_ids': []})
                report['checks']['extractor_schema'] = {'status': 'PASS' if extraction else 'FAIL'}
                store.put('ai_role_assignments', 'research_planner', {'primary': validated[0], 'backup': validated[1] if len(validated)>1 else None})
                planner = await service.run('research_planner', {'integration_probe':True,
                    'topic':'Find official Ollama API documentation; no market claims or prices.', 'evidence_ids':[]})
                report['checks']['planner_schema'] = {'status':'PASS' if planner else 'FAIL'}
            try:
                results = await provider.search('site:docs.ollama.com cloud API authentication')
                report['checks']['web_search'] = {'status': 'PASS' if results else 'EMPTY', 'results': len(results)}
            except Exception as exc:
                report['checks']['web_search'] = {'status': 'FAIL', 'error_type': type(exc).__name__}
            report['runs'] = [{'agent': row['agent'], 'model': row['model'], 'success': row['success'],
                              'latency_ms': row['latency_ms'], 'error': row.get('error'),
                              'validation_errors': row.get('validation_errors')}
                             for row in store.list('ai_runs')]
            report['status'] = 'PASS' if all(v['status']=='PASS' for v in report['checks'].values()) else 'PARTIAL'
            if configure and report['status'] == 'PASS':
                production = Store(settings.database_url)
                production.initialize()
                try:
                    for model in available:
                        production.put('ai_models', model['name'], model)
                    for role in ROLE_SCHEMAS:
                        existing = production.get('ai_role_assignments', role) or {}
                        if not existing.get('primary'):
                            assignment = {'role':role, 'primary':validated[0],
                                          'backup':validated[1] if len(validated)>1 else None, 'second_opinion':None}
                            production.put('ai_role_assignments',role,assignment)
                            production.put('audit_logs',uid(),{'action':'validated_model_bootstrap',
                                'assignment':assignment,'timestamp':utcnow().isoformat()})
                    report['roles_configured'] = True
                finally:
                    production.engine.dispose()
        except Exception as exc:
            report['status'] = 'FAIL'
            report['error_type'] = type(exc).__name__
            report['provider_status'] = provider.health['status']
        finally:
            store.engine.dispose()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--configure-roles', action='store_true')
    args = parser.parse_args()
    result = asyncio.run(check(args.configure_roles))
    output = ROOT / 'artifacts' / 'ollama-integration.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status']=='PASS' else 1)
