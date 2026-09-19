"""Build explicit source-only distributions; never include user data or keys."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def add(archive, relative, prefix):
    path = ROOT / relative
    archive.write(path, str(Path(prefix) / relative))


def build():
    shared = ['启动AI助手.cmd', 'launch_ai.py', 'run_ai.py', 'AI助手使用说明.md',
              'app/__init__.py', 'app/config.py', 'app/ai_gateway.py', 'app/llm.py',
              'app/ai_studio.py', 'static/ai.html', 'static/ai.css', 'static/ai.js',
              'docs/AI_ASSISTANT.md']
    with ZipFile(ROOT / 'AI助手独立版.zip', 'w', ZIP_DEFLATED) as archive:
        for relative in shared:
            add(archive, relative, 'AI助手')
        archive.writestr('AI助手/README.md', (ROOT / 'AI助手使用说明.md').read_bytes())
    root_files = ['.gitignore', '启动系统.cmd', '启动AI助手.cmd', '验证系统.cmd',
                  'launch.py', 'launch_ai.py', 'run.py', 'run_ai.py', 'start.sh',
                  'README.md', 'AGENTS.md', 'CONTRACT.md', 'AI助手使用说明.md']
    with ZipFile(ROOT / 'auction-lab.zip', 'w', ZIP_DEFLATED) as archive:
        for relative in root_files:
            add(archive, relative, 'auction-lab')
        for directory, allowed in [('app', {'.py'}), ('static', {'.js', '.css', '.html'}),
                                   ('tests', {'.py'}), ('docs', {'.md'}), ('tools', {'.py'})]:
            for path in sorted((ROOT / directory).glob('*')):
                if path.is_file() and path.suffix in allowed:
                    add(archive, path.relative_to(ROOT), 'auction-lab')
    for name in ('AI助手独立版.zip', 'auction-lab.zip'):
        with ZipFile(ROOT / name) as archive:
            if archive.testzip() is not None:
                raise ValueError('分发包校验失败')
            assert not any('/data/' in n or n.endswith('.env') or 'credentials' in n for n in archive.namelist())
            print(name, len(archive.namelist()), 'files', (ROOT / name).stat().st_size, 'bytes')


if __name__ == '__main__':
    build()
