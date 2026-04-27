import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from time import perf_counter

import yaml
from django.conf import settings
from django.contrib import messages
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from pycomm3 import LogixDriver, Tag

from .forms import (
    ClearHistoryForm,
    OpcUaFetchForm,
    PlcConfigImportForm,
    PlcConfigLoadForm,
    PlcConfigSaveForm,
    PlcReadForm,
)

try:
    from opcua import Client as OpcUaClient, ua
except Exception:
    OpcUaClient = None
    ua = None


@dataclass
class ViewState:
    message: str = ''
    tag_name: str = ''
    tag_value: object = None
    tag_status: str = ''
    connection_path: str = ''
    history: list = field(default_factory=list)


class SessionHistoryManager:
    def __init__(self, key, limit=10):
        self.key = key
        self.limit = limit

    def get(self, session):
        return session.get(self.key, [])

    def clear(self, session):
        session[self.key] = []

    def add(self, session, entry):
        history = self.get(session)
        history.insert(0, entry)
        session[self.key] = history[: self.limit]


class LogixClientPool:
    def __init__(self, idle_close_seconds=90):
        self._lock = threading.Lock()
        self._pool = {}
        self._idle_close_seconds = idle_close_seconds

    def _close_driver(self, driver):
        try:
            driver.close()
        except Exception:
            pass

    def _prune_stale(self):
        now = perf_counter()
        stale = [k for k, (_, used_at) in self._pool.items() if now - used_at > self._idle_close_seconds]
        for key in stale:
            driver, _ = self._pool.pop(key)
            self._close_driver(driver)

    def get(self, path):
        now = perf_counter()
        with self._lock:
            self._prune_stale()

            if path in self._pool:
                driver, _ = self._pool[path]
                if getattr(driver, 'connected', False):
                    self._pool[path] = (driver, now)
                    return driver
                self._close_driver(driver)
                self._pool.pop(path, None)

            driver = LogixDriver(path, init_tags=True, init_program_tags=True)
            driver.open()
            self._pool[path] = (driver, now)
            return driver

    def invalidate(self, path):
        with self._lock:
            item = self._pool.pop(path, None)
        if item:
            driver, _ = item
            self._close_driver(driver)


class LogixService:
    PATH_PATTERN = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}/\d+(?:/0)?$')

    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def parse_result(result):
        if isinstance(result, Tag):
            if result.error:
                return getattr(result, 'value', None), f'Tag read failed: {result.error}'
            return result.value, 'Tag read completed successfully.'
        if result is None:
            return None, 'Tag read completed with no value returned.'
        return result, 'Tag read completed successfully.'

    def normalize_path(self, path):
        path = path.strip()
        if not path or ':' in path:
            return '', ''
        if not self.PATH_PATTERN.match(path):
            return '', ''

        host, *segments = path.split('/')
        segments = [segment for segment in segments if segment]
        if len(segments) == 1:
            return f'{host}/{segments[0]}', f'{host}/{segments[0]}'
        if len(segments) == 2 and segments[1] == '0':
            return path, f'{host}/{segments[0]}'
        return path, path

    def connect_and_read(self, ip, tag_name):
        state = ViewState(tag_name=tag_name)
        internal_path, display_path = self.normalize_path(ip)
        if not internal_path:
            state.message = 'PLC address must follow this format: 10.191.175.15/1'
            return state, display_path

        state.connection_path = display_path
        try:
            plc = self.pool.get(internal_path)
            if plc.connected:
                state.message = f'Connected to PLC at {display_path}'
                if tag_name:
                    result = plc.read(tag_name)
                    state.tag_value, state.tag_status = self.parse_result(result)
            else:
                state.message = f'Unable to connect to PLC at {display_path}'
        except Exception as exc:
            self.pool.invalidate(internal_path)
            state.message = f'Connection error: {exc}'

        return state, display_path


class OpcUaService:
    ENDPOINT_PATTERN = re.compile(r'^[A-Za-z0-9.\-]+:\d{1,5}$')

    def normalize_endpoint(self, endpoint):
        endpoint = endpoint.strip()
        if not endpoint:
            return ''
        if endpoint.lower().startswith('opc.tcp://'):
            return endpoint.rstrip('/')
        if self.ENDPOINT_PATTERN.match(endpoint):
            return f'opc.tcp://{endpoint}'
        return ''

    @staticmethod
    def _node_class_name(node):
        try:
            node_class = node.get_node_class()
            return getattr(node_class, 'name', str(node_class))
        except Exception:
            return ''

    @staticmethod
    def _is_custom_string_node(node_id, path):
        if path.startswith('Objects.Server'):
            return False
        if 'ns=' not in node_id or ';s=' not in node_id:
            return False
        if 'ns=0;' in node_id:
            return False
        return True

    @staticmethod
    def _build_tag_row(path, node_id, node=None):
        segments = path.split('.')
        namespace = ''
        identifier = node_id
        identifier_type = ''
        if ';' in node_id:
            left, right = node_id.split(';', 1)
            namespace = left
            identifier = right
            identifier_type = right.split('=', 1)[0] if '=' in right else ''

        data_type = ''
        if node is not None:
            try:
                data_type = str(node.get_data_type_as_variant_type())
            except Exception:
                data_type = ''

        return {
            'tag_name': segments[-1] if segments else '',
            'node_id': node_id,
            'path': path,
            'namespace': namespace or '-',
            'identifier': identifier or '-',
            'identifier_type': identifier_type or '-',
            'data_type': data_type or '-',
            'node_class': 'Variable',
        }

    def fetch_tags(self, endpoint, max_tags=75, max_depth=4):
        if OpcUaClient is None:
            raise RuntimeError('OPC UA client library is not installed. Install it with: pip install opcua')

        client = OpcUaClient(endpoint, timeout=4)
        try:
            client.connect()
            root = client.get_objects_node()

            tags = []
            seen = set()
            queue = [(root, 0, 'Objects')]

            while queue and len(tags) < max_tags:
                node, depth, path = queue.pop(0)
                node_id = node.nodeid.to_string()
                if node_id in seen:
                    continue
                seen.add(node_id)

                if (
                    depth > 0
                    and self._node_class_name(node) == 'Variable'
                    and self._is_custom_string_node(node_id, path)
                ):
                    tags.append(self._build_tag_row(path, node_id, node=node))

                if depth >= max_depth:
                    continue

                try:
                    children = node.get_children()
                except Exception:
                    continue

                for child in children:
                    try:
                        browse_name = child.get_browse_name()
                        name = getattr(browse_name, 'Name', str(browse_name))
                    except Exception:
                        name = str(child.nodeid)
                    child_path = f'{path}.{name}' if path else name
                    queue.append((child, depth + 1, child_path))

            tags.sort(key=lambda item: item.get('tag_name', ''))
            return tags
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    def connect_and_fetch(self, endpoint_input):
        state = ViewState()
        endpoint = self.normalize_endpoint(endpoint_input)
        if not endpoint:
            state.message = 'Enter an OPC UA endpoint in this format: 10.191.175.15:4840 or opc.tcp://10.191.175.15:4840'
            return state, endpoint_input

        state.connection_path = endpoint
        try:
            tags = self.fetch_tags(endpoint)
            state.tag_name = 'Discovered tags'
            state.tag_value = tags
            state.tag_status = f'{len(tags)} OPC UA tags discovered.'
            state.message = f'Connected to OPC UA server at {endpoint}'
        except Exception as exc:
            state.message = f'Connection error: {exc}'

        return state, endpoint_input


_logix_pool = LogixClientPool(idle_close_seconds=90)
_logix_service = LogixService(pool=_logix_pool)
_opcua_service = OpcUaService()
_plc_history = SessionHistoryManager('plc_history', limit=10)
_opcua_history = SessionHistoryManager('opcua_history', limit=10)
_CACHE_DIR = Path(settings.BASE_DIR) / 'cache'
_CONFIG_DIR = _CACHE_DIR / 'yaml'
_LANDING_DATA_FILE = _CACHE_DIR / 'landing_data.json'
_LANDING_BACKUP_DIR = _CACHE_DIR / 'backups'
_RECENT_BACKUP_FILE = _LANDING_BACKUP_DIR / 'recent_backup.json'


class LandingDataStore:
    def __init__(self, data_path, backup_dir):
        self.data_path = Path(data_path)
        self.backup_dir = Path(backup_dir)
        self._lock = threading.Lock()

    def _timestamp(self):
        return timezone.now().astimezone(timezone.get_current_timezone()).isoformat()

    def default_payload(self):
        return {
            'updated_at': self._timestamp(),
            'plc': {
                'brand': 'allen_bradley',
                'connection_path': '',
                'last_tag': '',
                'last_value': None,
                'last_status': '',
            },
            'scada': {
                'endpoint': '',
                'last_fetch_status': '',
                'discovered_tags': [],
            },
            'bridge': {
                'mode': 'plc_to_scada',
                'node_address_map': {},
                'mapped_count': 0,
                'synced_count': 0,
                'last_sync_at': None,
                'last_sync_result': '',
                'last_sync_status_map': {},
            },
            'runtime': {
                'last_action': 'initialized',
                'last_message': 'Cache file created.',
            },
            'backup': {
                'last_backup': None,
                'reason': '',
                'path': str(self.backup_dir.relative_to(settings.BASE_DIR)).replace('\\', '/') + '/',
                'recent_file': str(_RECENT_BACKUP_FILE.relative_to(settings.BASE_DIR)).replace('\\', '/'),
            },
        }

    def _read_unlocked(self):
        if not self.data_path.exists():
            return self.default_payload()
        try:
            with self.data_path.open('r', encoding='utf-8') as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return self.default_payload()

    def read(self):
        with self._lock:
            payload = self._read_unlocked()
            self._write_unlocked(payload)
            return payload

    def _write_unlocked(self, payload):
        payload['updated_at'] = self._timestamp()
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        with self.data_path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=True)
            handle.write('\n')

    def update(self, mutate_callback, backup_reason=None):
        with self._lock:
            payload = self._read_unlocked()
            mutate_callback(payload)
            self._write_unlocked(payload)
            if backup_reason:
                self._create_backup_unlocked(payload, backup_reason)
            return payload

    def _create_backup_unlocked(self, payload, reason):
        backup = payload.setdefault('backup', {})
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = timezone.now().astimezone(timezone.get_current_timezone()).strftime('%Y%m%d_%H%M%S')
        backup_file = self.backup_dir / f'landing_data_{stamp}.json'
        backup_payload = deepcopy(payload)
        backup_payload['backup_snapshot'] = {
            'reason': reason,
            'created_at': self._timestamp(),
            'source_file': self.data_path.name,
        }
        with backup_file.open('w', encoding='utf-8') as handle:
            json.dump(backup_payload, handle, indent=2, ensure_ascii=True)
            handle.write('\n')

        with _RECENT_BACKUP_FILE.open('w', encoding='utf-8') as handle:
            json.dump(backup_payload, handle, indent=2, ensure_ascii=True)
            handle.write('\n')

        payload['backup']['last_backup'] = self._timestamp()
        payload['backup']['reason'] = reason

    @staticmethod
    def append_task(payload, action, message):
        runtime = payload.setdefault('runtime', {})
        runtime['last_action'] = action
        runtime['last_message'] = message

    def sync_from_session(self, request, action, message, backup_reason=None):
        def mutate(payload):
            plc_path = request.session.get('last_plc_ip', '')
            payload['plc'] = {
                'brand': request.session.get('last_plc_brand', 'allen_bradley'),
                'connection_path': plc_path,
                'last_tag': request.session.get('last_plc_tag', ''),
                'last_value': request.session.get('landing_last_plc_value'),
                'last_status': request.session.get('landing_last_plc_status', ''),
            }
            payload['scada'] = {
                'endpoint': request.session.get('last_opcua_endpoint', ''),
                'last_fetch_status': request.session.get('landing_last_opcua_status', ''),
                'discovered_tags': request.session.get('opcua_last_tags', []),
            }
            payload['bridge'] = {
                'mode': 'plc_to_scada',
                'node_address_map': request.session.get('opcua_plc_address_map', {}),
                'mapped_count': len(request.session.get('opcua_plc_address_map', {})),
                'synced_count': request.session.get('landing_last_synced_count', 0),
                'last_sync_at': request.session.get('landing_last_sync_at'),
                'last_sync_result': request.session.get('landing_last_sync_result', ''),
                'last_sync_status_map': request.session.get('opcua_sync_status_map', {}),
            }
            self.append_task(payload, action, message)

        return self.update(mutate, backup_reason=backup_reason)


_landing_store = LandingDataStore(_LANDING_DATA_FILE, _LANDING_BACKUP_DIR)
_landing_store.read()


class CombinedPageView(View):
    template_name = 'index.html'

    @staticmethod
    def _split_plc_path(path):
        path = (path or '').strip()
        if not path:
            return '', 0
        parts = [part for part in path.split('/') if part]
        if len(parts) >= 2:
            try:
                return parts[0], int(parts[1])
            except Exception:
                return parts[0], 0
        return parts[0], 0

    @staticmethod
    def _split_opcua_endpoint(endpoint):
        endpoint = (endpoint or '').strip()
        if not endpoint:
            return '', 4840
        raw = endpoint
        if raw.lower().startswith('opc.tcp://'):
            raw = raw[len('opc.tcp://') :]
        raw = raw.rstrip('/')
        if ':' not in raw:
            return raw, 4840
        host, port = raw.rsplit(':', 1)
        try:
            return host, int(port)
        except Exception:
            return host, 4840

    @staticmethod
    def _coerce_for_variant(value, variant_type):
        if value is None:
            return None
        if variant_type in (
            ua.VariantType.SByte,
            ua.VariantType.Byte,
            ua.VariantType.Int16,
            ua.VariantType.UInt16,
            ua.VariantType.Int32,
            ua.VariantType.UInt32,
            ua.VariantType.Int64,
            ua.VariantType.UInt64,
        ):
            return int(value)
        if variant_type in (ua.VariantType.Float, ua.VariantType.Double):
            return float(value)
        if variant_type == ua.VariantType.Boolean:
            if isinstance(value, str):
                return value.strip().lower() in ('1', 'true', 'yes', 'on')
            return bool(value)
        if variant_type == ua.VariantType.String:
            return str(value)
        return value

    @staticmethod
    def _build_common_rows(request):
        rows = []
        if not request.session.get('opcua_last_tags', []):
            return rows
        address_map = request.session.get('opcua_plc_address_map', {})
        status_map = request.session.get('opcua_sync_status_map', {})

        for node_id, plc_address in sorted(address_map.items()):
            rows.append(
                {
                    'node_id': node_id,
                    'plc_address': plc_address,
                    'status': status_map.get(node_id, ''),
                }
            )

        return rows

    @staticmethod
    def _ensure_config_dir():
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        return _CONFIG_DIR

    @classmethod
    def _config_file_choices(cls):
        cfg_dir = cls._ensure_config_dir()
        files = sorted(
            [p.name for p in cfg_dir.iterdir() if p.is_file() and p.suffix.lower() in ('.yaml', '.yml')],
            key=lambda name: name.lower(),
        )
        return [(name, name) for name in files]

    @staticmethod
    def _sanitize_config_stem(name):
        stem = (name or '').strip()
        if not stem:
            return ''
        stem = re.sub(r'\s+', '_', stem)
        stem = re.sub(r'[^A-Za-z0-9_.\-]', '', stem)
        stem = stem.strip('._-')
        return stem

    @classmethod
    def _config_path_from_name(cls, name):
        stem = cls._sanitize_config_stem(name)
        if not stem:
            return None
        lowered = stem.lower()
        if lowered.endswith('.yaml'):
            filename = stem
        elif lowered.endswith('.yml'):
            filename = f'{stem[:-4]}.yaml'
        else:
            filename = f'{stem}.yaml'
        return cls._ensure_config_dir() / filename

    def _build_plc_config_payload(self, request):
        last_plc_ip = request.session.get('last_plc_ip', '')
        ip_address, slot = self._split_plc_path(last_plc_ip)
        return {
            'version': 1,
            'saved_at': timezone.localtime().isoformat(),
            'plc': {
                'brand': request.session.get('last_plc_brand', 'allen_bradley'),
                'ip_address': ip_address,
                'slot': slot,
                'tag': request.session.get('last_plc_tag', ''),
            },
            'opcua': {
                'endpoint': request.session.get('last_opcua_endpoint', ''),
            },
            'mappings': {
                'node_address_map': request.session.get('opcua_plc_address_map', {}),
            },
        }

    @classmethod
    def _apply_plc_config_payload(cls, request, payload):
        if not isinstance(payload, dict):
            raise ValueError('Invalid YAML format.')
        plc = payload.get('plc') or {}
        opcua = payload.get('opcua') or {}
        mappings = payload.get('mappings') or {}

        ip_address = str(plc.get('ip_address', '') or '').strip()
        slot_raw = plc.get('slot', 0)
        try:
            slot = int(slot_raw)
        except Exception as exc:
            raise ValueError('Invalid slot value in YAML.') from exc
        if slot < 0:
            slot = 0
        brand = str(plc.get('brand', 'allen_bradley') or 'allen_bradley').strip().lower()
        if brand not in {'allen_bradley', 'siemens', 'modbus'}:
            brand = 'allen_bradley'
        tag = str(plc.get('tag', '') or '').strip()

        opcua_endpoint = str(opcua.get('endpoint', '') or '').strip()
        node_address_map = mappings.get('node_address_map') or {}
        if not isinstance(node_address_map, dict):
            raise ValueError('node_address_map must be a key-value object.')
        cleaned_map = {}
        for key, value in node_address_map.items():
            node_id = str(key or '').strip()
            plc_address = str(value or '').strip()
            if node_id:
                cleaned_map[node_id] = plc_address

        if ip_address:
            request.session['last_plc_ip'] = f'{ip_address}/{slot}'
        else:
            request.session['last_plc_ip'] = ''
        request.session['last_plc_brand'] = brand
        request.session['last_plc_tag'] = tag
        request.session['last_opcua_endpoint'] = opcua_endpoint
        request.session['opcua_plc_address_map'] = cleaned_map
        request.session['opcua_sync_status_map'] = {}

    @staticmethod
    def _clear_plc_config_session(request):
        request.session['last_plc_ip'] = ''
        request.session['last_plc_brand'] = 'allen_bradley'
        request.session['last_plc_tag'] = ''
        request.session['last_opcua_endpoint'] = ''
        request.session['opcua_plc_address_map'] = {}
        request.session['opcua_sync_status_map'] = {}

    def _build_context(
        self,
        request,
        plc_state,
        opcua_state,
        plc_form,
        opcua_form,
        plc_config_save_form=None,
        plc_config_load_form=None,
        plc_config_import_form=None,
        show_tag_popup=False,
    ):
        plc_state.history = _plc_history.get(request.session)
        opcua_state.history = _opcua_history.get(request.session)
        has_discovered_tags = bool(request.session.get('opcua_last_tags', []))
        has_opcua_tags = bool(request.session.get('opcua_plc_address_map', {})) and has_discovered_tags
        plc_config_save_form = plc_config_save_form or PlcConfigSaveForm()
        plc_config_load_form = plc_config_load_form or PlcConfigLoadForm(file_choices=self._config_file_choices())
        plc_config_import_form = plc_config_import_form or PlcConfigImportForm()
        return {
            'plc_state': plc_state,
            'opcua_state': opcua_state,
            'plc_form': plc_form,
            'opcua_form': opcua_form,
            'plc_config_save_form': plc_config_save_form,
            'plc_config_load_form': plc_config_load_form,
            'plc_config_import_form': plc_config_import_form,
            'common_rows': self._build_common_rows(request),
            'has_opcua_tags': has_opcua_tags,
            'has_discovered_tags': has_discovered_tags,
            'show_tag_popup': show_tag_popup and has_discovered_tags,
            'landing_data_file': _LANDING_DATA_FILE.name,
            'landing_backup_dir': str(_LANDING_BACKUP_DIR.relative_to(settings.BASE_DIR)).replace('\\', '/'),
            'landing_recent_backup_file': str(_RECENT_BACKUP_FILE.relative_to(settings.BASE_DIR)).replace('\\', '/'),
        }

    @staticmethod
    def _cached_opcua_state(session):
        state = ViewState()
        cached_tags = session.get('opcua_last_tags', [])
        if cached_tags:
            state.tag_name = 'Discovered tags'
            state.tag_value = cached_tags
            state.connection_path = session.get('opcua_last_endpoint', '')
        return state

    def get(self, request):
        last_plc_ip = request.session.get('last_plc_ip', '')
        ip_address, slot = self._split_plc_path(last_plc_ip)
        plc_form = PlcReadForm(
            initial={
                'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
                'plc_ip_address': ip_address,
                'plc_slot': slot,
                'plc_tag': request.session.get('last_plc_tag', ''),
            }
        )
        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
        plc_state = ViewState()
        opcua_state = self._cached_opcua_state(request.session)
        return render(request, self.template_name, self._build_context(request, plc_state, opcua_state, plc_form, opcua_form))

    def post(self, request):
        action = request.POST.get('action', '').strip().lower()
        show_tag_popup = False
        plc_state = ViewState()
        opcua_state = self._cached_opcua_state(request.session)
        last_plc_ip = request.session.get('last_plc_ip', '')
        ip_address, slot = self._split_plc_path(last_plc_ip)
        plc_form = PlcReadForm(
            initial={
                'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
                'plc_ip_address': ip_address,
                'plc_slot': slot,
                'plc_tag': request.session.get('last_plc_tag', ''),
            }
        )
        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
        plc_config_save_form = PlcConfigSaveForm()
        plc_config_load_form = PlcConfigLoadForm(file_choices=self._config_file_choices())
        plc_config_import_form = PlcConfigImportForm()

        if action == 'plc_read':
            plc_form = PlcReadForm(request.POST)
            if plc_form.is_valid():
                plc_ip = plc_form.cleaned_data['plc_ip']
                plc_brand = plc_form.cleaned_data['plc_brand']
                plc_tag = plc_form.cleaned_data['plc_tag'].strip()
                plc_state, display_path = _logix_service.connect_and_read(plc_ip, plc_tag)
                request.session['last_plc_ip'] = plc_ip
                request.session['last_plc_brand'] = plc_brand
                request.session['last_plc_tag'] = plc_tag
                _plc_history.add(
                    request.session,
                    {
                        'path': display_path,
                        'tag': plc_tag,
                        'message': plc_state.message,
                        'tag_status': plc_state.tag_status,
                        'when': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
                    },
                )
                if plc_state.message.startswith('Connected to PLC'):
                    messages.success(request, plc_state.message)
                else:
                    messages.error(request, plc_state.message)
                request.session['landing_last_plc_value'] = plc_state.tag_value
                request.session['landing_last_plc_status'] = plc_state.tag_status
                _landing_store.sync_from_session(
                    request,
                    action='plc_read',
                    message=plc_state.message,
                )
            else:
                messages.error(request, 'Please correct the PLC input errors and try again.')

        elif action == 'opcua_fetch':
            opcua_form = OpcUaFetchForm(request.POST)
            if opcua_form.is_valid():
                endpoint_input = opcua_form.cleaned_data['opcua_endpoint']
                opcua_state, raw_endpoint = _opcua_service.connect_and_fetch(endpoint_input)
                request.session['last_opcua_endpoint'] = endpoint_input
                if opcua_state.tag_name == 'Discovered tags' and isinstance(opcua_state.tag_value, list):
                    request.session['opcua_last_tags'] = opcua_state.tag_value
                    request.session['opcua_last_endpoint'] = opcua_state.connection_path or raw_endpoint
                    request.session['opcua_sync_status_map'] = {}
                    show_tag_popup = True
                _opcua_history.add(
                    request.session,
                    {
                        'path': opcua_state.connection_path or raw_endpoint,
                        'tag': opcua_state.tag_name,
                        'message': opcua_state.message,
                        'tag_status': opcua_state.tag_status,
                        'when': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
                    },
                )
                if opcua_state.message.startswith('Connected to OPC UA'):
                    messages.success(request, opcua_state.message)
                else:
                    messages.error(request, opcua_state.message)
                request.session['landing_last_opcua_status'] = opcua_state.tag_status
                _landing_store.sync_from_session(
                    request,
                    action='opcua_fetch',
                    message=opcua_state.message,
                )
            else:
                messages.error(request, 'Please correct the OPC UA input errors and try again.')

        elif action == 'add_selected_tags':
            selected_node_ids = request.POST.getlist('selected_node_ids')
            address_map = request.session.get('opcua_plc_address_map', {})
            status_map = request.session.get('opcua_sync_status_map', {})
            added_count = 0
            for node_id in selected_node_ids:
                if node_id not in address_map:
                    address_map[node_id] = ''
                    status_map[node_id] = ''
                    added_count += 1
            request.session['opcua_plc_address_map'] = address_map
            request.session['opcua_sync_status_map'] = status_map
            if added_count > 0:
                messages.success(request, f'Added {added_count} tags to mapping.')
            else:
                messages.info(request, 'No new tags added (already in mapping).')
            _landing_store.sync_from_session(
                request,
                action='add_selected_tags',
                message=f'Added {added_count} OPC UA tags to PLC/SCADA mapping.',
            )

        elif action == 'sync_opcua_to_plc':
            endpoint = request.session.get('opcua_last_endpoint', '').strip()
            plc_ip = request.session.get('last_plc_ip', '').strip()
            node_ids = request.POST.getlist('node_ids')
            plc_addresses = request.POST.getlist('plc_addresses')

            address_map = {}
            for idx, node_id in enumerate(node_ids):
                node_id = node_id.strip()
                plc_tag = plc_addresses[idx].strip() if idx < len(plc_addresses) else ''
                if node_id:
                    address_map[node_id] = plc_tag
            request.session['opcua_plc_address_map'] = address_map

            pairs = [(node_id, plc_tag) for node_id, plc_tag in address_map.items() if node_id and plc_tag]
            status_map = {}
            for node_id, plc_tag in address_map.items():
                if not plc_tag:
                    status_map[node_id] = 'Skipped: address is empty.'

            if not endpoint:
                messages.error(request, 'Fetch OPC UA tags first before running connect.')
            elif not plc_ip:
                messages.error(request, 'Enter a PLC address first, then run connect.')
            elif not pairs:
                messages.error(request, 'Add at least one PLC address mapping before running connect.')
            elif OpcUaClient is None:
                messages.error(request, 'OPC UA client library is not installed. Install it with: pip install opcua')
            else:
                internal_path, display_path = _logix_service.normalize_path(plc_ip)
                if not internal_path:
                    messages.error(request, 'PLC address must follow this format: 10.191.175.15/1')
                else:
                    opcua_client = OpcUaClient(endpoint, timeout=4)
                    success_count = 0
                    try:
                        opcua_client.connect()
                        plc = _logix_pool.get(internal_path)
                        if not plc.connected:
                            raise RuntimeError(f'Unable to connect to PLC at {display_path}')

                        plc_tags = [plc_tag for _, plc_tag in pairs]
                        read_results = plc.read(*plc_tags) if plc_tags else []
                        if plc_tags and not isinstance(read_results, list):
                            read_results = [read_results]
                        read_map = {tag_name: result for tag_name, result in zip(plc_tags, read_results)}

                        for node_id, plc_tag in pairs:
                            try:
                                read_result = read_map.get(plc_tag)
                                plc_value, read_status = _logix_service.parse_result(read_result)
                                if 'failed' in read_status.lower() or plc_value is None:
                                    status_map[node_id] = f'PLC read failed for {plc_tag}: {read_status}'
                                    continue

                                node = opcua_client.get_node(node_id)
                                variant_type = node.get_data_type_as_variant_type()
                                typed_value = self._coerce_for_variant(plc_value, variant_type)
                                node.set_value(ua.DataValue(ua.Variant(typed_value, variant_type)))
                                status_map[node_id] = f'Synced PLC {plc_tag} -> NodeID ({plc_value})'
                                success_count += 1
                            except Exception as exc:
                                status_map[node_id] = f'Sync failed: {exc}'

                        if success_count == len(pairs):
                            messages.success(request, f'Connect completed successfully for {success_count} mapped tags.')
                        elif success_count > 0:
                            messages.info(
                                request,
                                f'Connect completed with partial success: {success_count} of {len(pairs)} mapped tags.',
                            )
                        else:
                            messages.error(request, 'Connect completed with no successful tag sync operations.')
                        request.session['landing_last_synced_count'] = success_count
                        request.session['landing_last_sync_at'] = timezone.localtime().isoformat()
                        request.session['landing_last_sync_result'] = (
                            f'Sync completed for {success_count} of {len(pairs)} mapped tags.'
                        )
                    except Exception as exc:
                        for node_id, _ in pairs:
                            status_map[node_id] = f'Connection error: {exc}'
                        messages.error(request, f'Connect failed: {exc}')
                        request.session['landing_last_synced_count'] = 0
                        request.session['landing_last_sync_at'] = timezone.localtime().isoformat()
                        request.session['landing_last_sync_result'] = f'Connect failed: {exc}'
                    finally:
                        try:
                            opcua_client.disconnect()
                        except Exception:
                            pass

            request.session['opcua_sync_status_map'] = status_map
            _landing_store.sync_from_session(
                request,
                action='sync_opcua_to_plc',
                message=request.session.get('landing_last_sync_result', 'Sync attempt completed.'),
                backup_reason='sync_opcua_to_plc',
            )

        elif action == 'clear_table_addresses':
            request.session['opcua_plc_address_map'] = {}
            messages.info(request, 'All table address inputs have been cleared.')
            _landing_store.sync_from_session(
                request,
                action='clear_table_addresses',
                message='All PLC address mappings were cleared.',
            )

        elif action == 'save_plc_config':
            plc_config_save_form = PlcConfigSaveForm(request.POST)
            if plc_config_save_form.is_valid():
                config_name = plc_config_save_form.cleaned_data['config_name']
                config_path = self._config_path_from_name(config_name)
                if config_path is None:
                    messages.error(request, 'Invalid configuration name.')
                else:
                    payload = self._build_plc_config_payload(request)
                    try:
                        with config_path.open('w', encoding='utf-8') as handle:
                            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=False)
                        messages.success(request, f'Configuration saved: {config_path.name}')
                        plc_config_save_form = PlcConfigSaveForm()
                        plc_config_load_form = PlcConfigLoadForm(file_choices=self._config_file_choices())
                        _landing_store.sync_from_session(
                            request,
                            action='save_plc_config',
                            message=f'Configuration saved to YAML: {config_path.name}',
                            backup_reason='save_plc_config',
                        )
                    except Exception as exc:
                        messages.error(request, f'Unable to save YAML file: {exc}')

        elif action == 'load_plc_config':
            plc_config_load_form = PlcConfigLoadForm(request.POST, file_choices=self._config_file_choices())
            if plc_config_load_form.is_valid():
                selected_file = plc_config_load_form.cleaned_data['config_file']
                config_path = self._config_path_from_name(selected_file)
                if config_path is None or not config_path.exists():
                    messages.error(request, 'Selected configuration file was not found.')
                else:
                    try:
                        with config_path.open('r', encoding='utf-8') as handle:
                            payload = yaml.safe_load(handle) or {}
                        self._apply_plc_config_payload(request, payload)
                        messages.success(request, f'Configuration loaded: {config_path.name}')
                        last_plc_ip = request.session.get('last_plc_ip', '')
                        ip_address, slot = self._split_plc_path(last_plc_ip)
                        plc_form = PlcReadForm(
                            initial={
                                'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
                                'plc_ip_address': ip_address,
                                'plc_slot': slot,
                                'plc_tag': request.session.get('last_plc_tag', ''),
                            }
                        )
                        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
                        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
                        _landing_store.sync_from_session(
                            request,
                            action='load_plc_config',
                            message=f'Configuration loaded from YAML: {config_path.name}',
                        )
                    except Exception as exc:
                        messages.error(request, f'Unable to load YAML file: {exc}')

        elif action == 'import_plc_config':
            plc_config_import_form = PlcConfigImportForm(request.POST, request.FILES)
            if plc_config_import_form.is_valid():
                file_obj = plc_config_import_form.cleaned_data['config_upload']
                suggested_name = (getattr(file_obj, 'name', '') or '').rsplit('.', 1)[0]
                config_path = self._config_path_from_name(suggested_name)
                if config_path is None:
                    messages.error(request, 'Invalid import file name.')
                else:
                    try:
                        raw_data = file_obj.read().decode('utf-8')
                        payload = yaml.safe_load(raw_data) or {}
                        self._apply_plc_config_payload(request, payload)
                        with config_path.open('w', encoding='utf-8') as handle:
                            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=False)
                        messages.success(request, f'Configuration imported and loaded: {config_path.name}')
                        plc_config_import_form = PlcConfigImportForm()
                        plc_config_load_form = PlcConfigLoadForm(file_choices=self._config_file_choices())
                        last_plc_ip = request.session.get('last_plc_ip', '')
                        ip_address, slot = self._split_plc_path(last_plc_ip)
                        plc_form = PlcReadForm(
                            initial={
                                'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
                                'plc_ip_address': ip_address,
                                'plc_slot': slot,
                                'plc_tag': request.session.get('last_plc_tag', ''),
                            }
                        )
                        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
                        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
                        _landing_store.sync_from_session(
                            request,
                            action='import_plc_config',
                            message=f'Configuration imported from YAML: {config_path.name}',
                            backup_reason='import_plc_config',
                        )
                    except UnicodeDecodeError:
                        messages.error(request, 'Import failed: file must be UTF-8 encoded text.')
                    except Exception as exc:
                        messages.error(request, f'Import failed: {exc}')

        elif action == 'clear_plc_config':
            self._clear_plc_config_session(request)
            messages.info(request, 'PLC configuration values have been cleared from the current session.')
            plc_form = PlcReadForm(
                initial={
                    'plc_brand': 'allen_bradley',
                    'plc_ip_address': '',
                    'plc_slot': 0,
                    'plc_tag': '',
                }
            )
            opcua_form = OpcUaFetchForm(initial={'opcua_host': '', 'opcua_port': 4840})
            request.session['landing_last_plc_value'] = None
            request.session['landing_last_plc_status'] = ''
            request.session['landing_last_opcua_status'] = ''
            request.session['landing_last_synced_count'] = 0
            request.session['landing_last_sync_at'] = None
            request.session['landing_last_sync_result'] = ''
            _landing_store.sync_from_session(
                request,
                action='clear_plc_config',
                message='Current PLC/SCADA session values were cleared.',
            )

        elif action == 'clear_history':
            clear_form = ClearHistoryForm(request.POST)
            if clear_form.is_valid():
                protocol = clear_form.cleaned_data['protocol']
                if protocol == 'plc':
                    _plc_history.clear(request.session)
                    messages.info(request, 'PLC history cleared.')
                elif protocol == 'opcua':
                    _opcua_history.clear(request.session)
                    messages.info(request, 'OPC UA history cleared.')
            else:
                messages.error(request, 'Invalid history-clear request.')
        else:
            messages.error(request, 'Unsupported action requested.')

        return render(
            request,
            self.template_name,
            self._build_context(
                request,
                plc_state,
                opcua_state,
                plc_form,
                opcua_form,
                plc_config_save_form=plc_config_save_form,
                plc_config_load_form=plc_config_load_form,
                plc_config_import_form=plc_config_import_form,
                show_tag_popup=show_tag_popup,
            ),
        )
