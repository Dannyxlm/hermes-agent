"""Closed-name, bounded memory editor. No arbitrary workspace-context access."""
import os
import stat
import uuid
from contextlib import contextmanager
from hermes_constants import get_hermes_home

MAX_MEMORY = 256 * 1024
NAMES = {'memory': ('memories', 'MEMORY.md'), 'user': ('memories', 'USER.md'), 'soul': ('', 'SOUL.md')}


@contextmanager
def directory(section, create=False):
    if section not in NAMES:
        raise ValueError('Unknown memory section')
    sub, name = NAMES[section]
    home = get_hermes_home()
    # Directory fds bind all IO to the checked directories, even during renames.
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if sub:
            if create:
                try:
                    os.mkdir(sub, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            inner = os.open(sub, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = inner
        yield fd, name
    finally:
        os.close(fd)


def checked_stat(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Memory file must be regular and not a symlink')
    return info


def read_section(section):
    try:
        with directory(section) as (fd, name):
            info = checked_stat(fd, name)
            if info is None:
                return '', None
            with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), 'rb') as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError('Memory file must be regular')
                data = source.read(MAX_MEMORY + 1)
            if len(data) > MAX_MEMORY:
                raise OverflowError('Memory file too large')
            return data.decode('utf-8', errors='replace'), info.st_mtime
    except FileNotFoundError:
        return '', None


def read_memory():
    result = {}
    for section, (sub, name) in NAMES.items():
        result[section], result[section+'_mtime'] = read_section(section)
        result[section+'_path'] = str(get_hermes_home() / sub / name)
    # Deliberately not an arbitrary path reader. Use native project context APIs.
    result.update(project_context='', project_context_path='', project_context_name='', project_context_workspace='')
    return result


def write_memory(section, content):
    if not isinstance(content, str):
        raise ValueError('Content must be text')
    data = content.encode('utf-8')
    if len(data) > MAX_MEMORY:
        raise OverflowError('Memory file too large')
    with directory(section, create=True) as (fd, name):
        checked_stat(fd, name)
        temporary = '.mobile-'+uuid.uuid4().hex
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd), 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            checked_stat(fd, name)
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
    sub, name = NAMES[section]
    return {'ok': True, 'section': section, 'path': str(get_hermes_home() / sub / name)}
