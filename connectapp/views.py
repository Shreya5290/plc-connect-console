import re
from datetime import datetime
from time import perf_counter
import threading

from django.shortcuts import render
from pycomm3 import LogixDriver, Tag

_POOL_LOCK = threading.Lock()
_PLC_POOL = {}
_IDLE_CLOSE_SECONDS = 90


def parse_logix_result(result):
    if isinstance(result, Tag):
        if result.error:
            return getattr(result, 'value', None), f'Tag read error: {result.error}'
        return result.value, 'Tag read successful.'
    if result is None:
        return None, 'Tag read returned no data.'
    return result, 'Tag read successful.'


def read_logix_tag(plc, tag_name):
    return plc.read(tag_name)

def _close_driver(driver):
    try:
        driver.close()
    except Exception:
        pass

def _get_cached_plc(path):
    now = perf_counter()
    with _POOL_LOCK:
        stale = [k for k, (_, used_at) in _PLC_POOL.items() if now - used_at > _IDLE_CLOSE_SECONDS]
        for key in stale:
            drv, _ = _PLC_POOL.pop(key)
            _close_driver(drv)

        if path in _PLC_POOL:
            driver, _ = _PLC_POOL[path]
            if getattr(driver, 'connected', False):
                _PLC_POOL[path] = (driver, now)
                return driver
            _close_driver(driver)
            _PLC_POOL.pop(path, None)

        driver = LogixDriver(path, init_tags=True, init_program_tags=True)
        driver.open()
        _PLC_POOL[path] = (driver, now)
        return driver

def _invalidate_cached_plc(path):
    with _POOL_LOCK:
        item = _PLC_POOL.pop(path, None)
    if item:
        driver, _ = item
        _close_driver(driver)


def normalize_plc_path(path):
    path = path.strip()
    if not path or ':' in path:
        return '', ''
    if not re.match(r'^\d{1,3}(?:\.\d{1,3}){3}/\d+(?:/0)?$', path):
        return '', ''

    host, *segments = path.split('/')
    segments = [segment for segment in segments if segment]
    if len(segments) == 1:
        return f'{host}/{segments[0]}', f'{host}/{segments[0]}'
    if len(segments) == 2 and segments[1] == '0':
        return path, f'{host}/{segments[0]}'
    return path, path


def index(request):
    message = ''
    tag_name = ''
    tag_value = None
    tag_status = ''
    connection_path = ''
    read_ms = None

    if request.method == 'POST':
        action = request.POST.get('action', '').strip().lower()
        if action == 'clear_history':
            request.session['plc_history'] = []
            message = 'History cleared.'
            return render(
                request,
                'index.html',
                {
                    'message': message,
                    'tag_name': tag_name,
                    'tag_value': tag_value,
                    'tag_status': tag_status,
                    'read_ms': read_ms,
                    'connection_path': connection_path,
                    'history': [],
                },
            )

        ip = request.POST.get('ip', '').strip()
        tag_name = request.POST.get('tag', '').strip()

        if not ip:
            message = 'Please enter a PLC address in the form 10.191.175.15/1.'
        else:
            internal_path, display_path = normalize_plc_path(ip)
            if not internal_path:
                message = 'Address must be in the form 10.191.175.15/1'
            else:
                connection_path = display_path
                try:
                    plc = _get_cached_plc(internal_path)
                    if plc.connected:
                        message = f'Connected to PLC at {display_path}'
                        if tag_name:
                            t0 = perf_counter()
                            result = read_logix_tag(plc, tag_name)
                            read_ms = round((perf_counter() - t0) * 1000, 1)
                            tag_value, tag_status = parse_logix_result(result)
                            tag_status = f'{tag_status} ({read_ms} ms)'
                    else:
                        message = f'Could not connect to PLC at {display_path}'
                except Exception as exc:
                    _invalidate_cached_plc(internal_path)
                    message = f'Error: {exc}'

                history = request.session.get('plc_history', [])
                history.insert(
                    0,
                    {
                        'path': display_path,
                        'tag': tag_name,
                        'message': message,
                        'tag_status': tag_status,
                        'read_ms': read_ms,
                        'when': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    },
                )
                request.session['plc_history'] = history[:10]

    history = request.session.get('plc_history', [])

    return render(
        request,
        'index.html',
        {
            'message': message,
            'tag_name': tag_name,
            'tag_value': tag_value,
            'tag_status': tag_status,
            'read_ms': read_ms,
            'connection_path': connection_path,
            'history': history,
        },
    )
