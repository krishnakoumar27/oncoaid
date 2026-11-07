"""Create a source-only upload package without local secrets or patient records."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

FILES = {
    'Dockerfile', '.dockerignore', '.gitignore', 'requirements.txt', 'compose.yaml', 'render.yaml',
    'api.py', 'dashboard.py', 'cloud_start.py', 'schema.sql', 'catalog.py', 'costs.py',
    'intelligence.py', 'funding_sources.py', 'processing.py', 'telegram_channel.py', 'whatsapp.py',
    'patient_journey.py', 'benefits.py', 'languages.py', 'navigation.py', 'maintenance.py',
    'setup_local.py', 'configure_telegram.py', 'whatsapp-gateway.conf', 'package_web.py',
    'README.md', 'HOSTING.md', 'WHATSAPP_SETUP.md', 'test_costs.py', 'test_benefits.py', 'test_languages.py',
}


def main():
    root = Path(__file__).resolve().parent
    target = root.parent / 'oncoaid-web-source.zip'
    files = [root / name for name in sorted(FILES)] + sorted((root / 'tests').glob('*.py'))
    with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
        for source in files:
            archive.write(source, 'oncoaid/' + source.relative_to(root).as_posix())
    with ZipFile(target) as archive:
        entries = archive.namelist()
        assert all(not Path(name).name.startswith('.env') and '__pycache__' not in name for name in entries)
        assert 'oncoaid/render.yaml' in entries and 'oncoaid/cloud_start.py' in entries
    print(f'Created {target.name}: {len(entries)} source files. Local secrets and patient data are excluded.')


if __name__ == '__main__':
    main()
