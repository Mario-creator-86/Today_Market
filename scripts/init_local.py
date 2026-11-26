"""Create missing local configuration values without revealing secrets."""
import secrets
from pathlib import Path


def prepare(path: Path) -> list[str]:
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    values = {line.partition('=')[0].strip():line.partition('=')[2].strip() for line in lines if '=' in line}
    added = []
    for name in ['ADMIN_TOKEN', 'POSTGRES_PASSWORD']:
        if not values.get(name):
            lines = [line for line in lines if line.partition('=')[0].strip()!=name]
            lines.append(f'{name}={secrets.token_urlsafe(32)}')
            added.append(name)
    path.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return added

if __name__=='__main__':
    added=prepare(Path(__file__).resolve().parent.parent / '.env')
    print('Local configuration prepared; secret values are not printed. Added: ' + ', '.join(added))
