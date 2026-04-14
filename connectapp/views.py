import re
import threading
from dataclasses import dataclass, field
from time import perf_counter

from django.contrib import messages
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from pycomm3 import LogixDriver, Tag

from .forms import ClearHistoryForm, OpcUaFetchForm, PlcReadForm

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
    def _build_tag_row(path, node_id):
        segments = path.split('.')
        return {
            'tag_name': segments[-1] if segments else '',
            'node_id': node_id,
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
                    tags.append(self._build_tag_row(path, node_id))

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
    def _build_common_rows(request, opcua_state):
        rows = []
        address_map = request.session.get('opcua_plc_address_map', {})
        status_map = request.session.get('opcua_sync_status_map', {})

        if opcua_state.tag_name == 'Discovered tags' and isinstance(opcua_state.tag_value, list):
            for item in opcua_state.tag_value:
                node_id = item.get('node_id', '')
                rows.append(
                    {
                        'node_id': node_id,
                        'plc_address': address_map.get(node_id, ''),
                        'status': status_map.get(node_id, ''),
                    }
                )

        return rows

    def _build_context(self, request, plc_state, opcua_state, plc_form, opcua_form):
        plc_state.history = _plc_history.get(request.session)
        opcua_state.history = _opcua_history.get(request.session)
        has_opcua_tags = bool(request.session.get('opcua_fetch_ready', False))
        return {
            'plc_state': plc_state,
            'opcua_state': opcua_state,
            'plc_form': plc_form,
            'opcua_form': opcua_form,
            'common_rows': self._build_common_rows(request, opcua_state),
            'has_opcua_tags': has_opcua_tags,
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
            }
        )
        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
        plc_state = ViewState()
        opcua_state = self._cached_opcua_state(request.session)
        return render(request, self.template_name, self._build_context(request, plc_state, opcua_state, plc_form, opcua_form))

    def post(self, request):
        action = request.POST.get('action', '').strip().lower()
        plc_state = ViewState()
        opcua_state = self._cached_opcua_state(request.session)
        last_plc_ip = request.session.get('last_plc_ip', '')
        ip_address, slot = self._split_plc_path(last_plc_ip)
        plc_form = PlcReadForm(
            initial={
                'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
                'plc_ip_address': ip_address,
                'plc_slot': slot,
            }
        )
        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})

        if action == 'plc_read':
            plc_form = PlcReadForm(request.POST)
            if plc_form.is_valid():
                plc_ip = plc_form.cleaned_data['plc_ip']
                plc_brand = plc_form.cleaned_data['plc_brand']
                plc_tag = plc_form.cleaned_data['plc_tag'].strip()
                plc_state, display_path = _logix_service.connect_and_read(plc_ip, plc_tag)
                request.session['last_plc_ip'] = plc_ip
                request.session['last_plc_brand'] = plc_brand
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
                    request.session['opcua_plc_address_map'] = {}
                    request.session['opcua_fetch_ready'] = bool(opcua_state.tag_value)
                else:
                    request.session['opcua_fetch_ready'] = False
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
            else:
                request.session['opcua_fetch_ready'] = False
                messages.error(request, 'Please correct the OPC UA input errors and try again.')

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
                    except Exception as exc:
                        for node_id, _ in pairs:
                            status_map[node_id] = f'Connection error: {exc}'
                        messages.error(request, f'Connect failed: {exc}')
                    finally:
                        try:
                            opcua_client.disconnect()
                        except Exception:
                            pass

            request.session['opcua_sync_status_map'] = status_map

        elif action == 'clear_table_addresses':
            request.session['opcua_plc_address_map'] = {}
            messages.info(request, 'All table address inputs have been cleared.')

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

        return render(request, self.template_name, self._build_context(request, plc_state, opcua_state, plc_form, opcua_form))
