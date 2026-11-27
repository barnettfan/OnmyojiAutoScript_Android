"""Explicit source restriction for this deployment's updater."""
from pathlib import Path
import re
import subprocess

from deploy.config import ExecutionError

UPDATE_REPOSITORY = 'https://github.com/barnettfan/OnmyojiAutoScript.git'


def is_update_repository(value):
    # Accept GitHub's HTTPS and SSH spellings of this deployment repository only.
    return bool(re.fullmatch(
        r'(?:https://github[.]com/|git@github[.]com:|ssh://git@github[.]com/)'
        r'barnettfan/OnmyojiAutoScript(?:[.]git)?/?',
        str(value or '').strip(), re.IGNORECASE))


def validate_update_source(git, root, repository, source='origin'):
    message = ('Repository source rejected: updates require '
               + UPDATE_REPOSITORY
               + '. No fetch or pull was performed.')
    if not is_update_repository(repository):
        raise ExecutionError(message)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source):
        raise ExecutionError(message)
    if not (Path(root) / '.git').exists():
        return  # First installation; validate its configured URL above.
    try:
        result = subprocess.run(
            [git, 'remote', 'get-url', '--all', source], cwd=root,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExecutionError(message) from error
    urls = result.stdout.splitlines()
    if result.returncode or not urls or not all(is_update_repository(u) for u in urls):
        raise ExecutionError(message)
