"""Run the small hosted workspace on one web instance, with private API access."""
import logging
import os
import signal
import subprocess
import sys
import time

import requests

log = logging.getLogger('oncoaid.cloud')


def main():
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv('PORT', '10000'))
    if not 1 <= port <= 65535 or port == 8000:
        raise RuntimeError('PORT must be between 1 and 65535 and different from the private API port 8000.')
    children = []
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def start(name, arguments, environment=None):
        process = subprocess.Popen([sys.executable, *arguments], env=environment)
        children.append((name, process))
        log.info('%s started', name)
        return process

    result = 0
    try:
        api = start('API', ['-m', 'uvicorn', 'api:app', '--host', '127.0.0.1', '--port', '8000'])
        deadline = time.monotonic() + 90
        ready = False
        while not stopping and time.monotonic() < deadline:
            if api.poll() is not None:
                raise RuntimeError('API failed to start. Check the database and bootstrap settings.')
            try:
                ready = requests.get('http://127.0.0.1:8000/health', timeout=2).ok
            except requests.RequestException:
                pass
            if ready:
                break
            time.sleep(.5)
        if not ready:
            if not stopping:
                raise RuntimeError('The API did not become ready before the startup deadline.')
            return 0
        dashboard_env = {**os.environ, 'API_URL':'http://127.0.0.1:8000'}
        start('Dashboard', ['-m', 'streamlit', 'run', 'dashboard.py', '--server.address=0.0.0.0',
                           f'--server.port={port}', '--server.headless=true', '--browser.gatherUsageStats=false'], dashboard_env)
        start('Document processor', ['processing.py'])
        if os.getenv('TELEGRAM_BOT_TOKEN', '').strip():
            start('Telegram', ['telegram_channel.py'])
        else:
            log.info('Telegram is not configured; add its runtime secret to enable patient messaging.')
        while not stopping:
            for name, process in children:
                if process.poll() is not None:
                    raise RuntimeError(f'{name} stopped. Restarting the container is required.')
            time.sleep(.5)
    except Exception:
        # Never log secret-bearing connection strings or provider exception payloads.
        log.error('A hosted service could not stay running. Check the named service startup logs.')
        result = 1
    finally:
        for _, process in reversed(children):
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 10
        for name, process in reversed(children):
            try:
                process.wait(timeout=max(.1, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning('%s required forced shutdown', name)
                process.kill()
                process.wait()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
