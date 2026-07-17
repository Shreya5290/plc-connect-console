import asyncio
import re
import struct
import threading
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from time import perf_counter

import yaml
from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from django.views.decorators.http import require_GET
from pycomm3 import LogixDriver, Tag
from pymodbus.client import ModbusTcpClient

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

_SNAP7_IMPORT_ERROR = None
_Snap7Client = None
try:
    # snap7 v3.0+ is pure Python - use the new s7 package
    from s7 import Client as _Snap7Client
except Exception:
    try:
        # Fallback: snap7 v2.x legacy API
        from snap7.client import Client as _Snap7Client
    except Exception as _snap7_exc:
        _Snap7Client = None
        _SNAP7_IMPORT_ERROR = str(_snap7_exc)


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

class PymodbusService:
    """Lightweight Modbus TCP reader for simple numeric/register tag names."""

    @staticmethod
    def _parse_host(path):
        path = (path or '').strip()
        if not path:
            return '', ''
        parts = [p for p in path.split('/') if p]
        host = parts[0] if parts else ''
        display = path
        return host, display

    @staticmethod
    def _is_error_response(result):
        if result is None:
            return True
        is_error = getattr(result, 'isError', None)
        if callable(is_error):
            return bool(is_error())
        is_error = getattr(result, 'is_error', None)
        if callable(is_error):
            return bool(is_error())
        return bool(is_error)

    @staticmethod
    def _to_modbus_offset(func, address):
        """Convert common Modbus reference labels to zero-based offsets."""
        if func in ('holding', 'reg', 'register') and 40001 <= address <= 49999:
            return address - 40001, f'40001-style label {address}'
        if func in ('input', 'input_register', 'input_registers') and 30001 <= address <= 39999:
            return address - 30001, f'30001-style label {address}'
        if func in ('discrete_input', 'discrete', 'input_discrete') and 10001 <= address <= 19999:
            return address - 10001, f'10001-style label {address}'
        return address, f'offset {address}'

    @staticmethod
    def _register_count(data_type, string_length=1):
        if data_type in ('uint32', 'int32', 'float32'):
            return 2
        if data_type in ('uint64', 'int64', 'float64'):
            return 4
        if data_type == 'string':
            return max(1, (string_length + 1) // 2)
        return 1

    @staticmethod
    def _decode_registers(registers, data_type, debug_info=None, word_swapped=False):
        if not registers:
            return None
        registers = [int(r) for r in registers]
        first = registers[0]

        # Optional 16-bit word swapping for floats.
        # - float32: swap the two 16-bit words
        # - float64: swap the four 16-bit words in pairs (word order: 1-0-3-2)
        if data_type == 'float32':
            w0, w1 = registers[0], registers[1]
            if word_swapped:
                w0, w1 = w1, w0
            return struct.unpack('>f', struct.pack('>HH', w0, w1))[0]

        if data_type == 'float64':
            w0, w1, w2, w3 = registers[0], registers[1], registers[2], registers[3]
            if word_swapped:
                w0, w1, w2, w3 = w1, w0, w3, w2
            return struct.unpack('>d', struct.pack('>HHHH', w0, w1, w2, w3))[0]

        if data_type == 'int16':
            return first - 0x10000 if first & 0x8000 else first
        if data_type == 'uint32':
            return (int(registers[0]) << 16) | int(registers[1])
        if data_type == 'int32':
            value = (int(registers[0]) << 16) | int(registers[1])
            return value - 0x100000000 if value & 0x80000000 else value
        if data_type == 'uint64':
            return (int(registers[0]) << 48) | (int(registers[1]) << 32) | (int(registers[2]) << 16) | int(registers[3])
        if data_type == 'int64':
            value = (int(registers[0]) << 48) | (int(registers[1]) << 32) | (int(registers[2]) << 16) | int(registers[3])
            return value - (1 << 64) if value & (1 << 63) else value
        if data_type == 'float64':
            return struct.unpack('>d', struct.pack('>HHHH', int(registers[0]), int(registers[1]), int(registers[2]), int(registers[3])))[0]
        if data_type == 'bool':
            return bool(first)
        if data_type == 'string':
            raw = b''
            for r in registers:
                raw += struct.pack('>H', int(r))
            return raw.split(b'\x00', 1)[0].decode('ascii', errors='replace')
        return first

    @staticmethod
    def _parse_bool(value):
        normalized = str(value).strip().lower()
        if normalized in ('1', 'true', 'on', 'yes'):
            return True
        if normalized in ('0', 'false', 'off', 'no'):
            return False
        raise ValueError('Boolean values must be true/false or 1/0.')

    @classmethod
    def _encode_registers(cls, value, data_type):
        value = str(value).strip()
        if data_type == 'bool':
            return [1 if cls._parse_bool(value) else 0]
        if data_type == 'int16':
            number = int(value)
            if not -32768 <= number <= 32767:
                raise ValueError('Int16 value must be between -32768 and 32767.')
            return [number & 0xFFFF]
        if data_type == 'uint32':
            number = int(value)
            if not 0 <= number <= 0xFFFFFFFF:
                raise ValueError('UInt32 value must be between 0 and 4294967295.')
            return [(number >> 16) & 0xFFFF, number & 0xFFFF]
        if data_type == 'int32':
            number = int(value)
            if not -2147483648 <= number <= 2147483647:
                raise ValueError('Int32 value must be between -2147483648 and 2147483647.')
            number &= 0xFFFFFFFF
            return [(number >> 16) & 0xFFFF, number & 0xFFFF]
        if data_type == 'uint64':
            number = int(value)
            if not 0 <= number <= 0xFFFFFFFFFFFFFFFF:
                raise ValueError('UInt64 value must be between 0 and 18446744073709551615.')
            return [(number >> 48) & 0xFFFF, (number >> 32) & 0xFFFF, (number >> 16) & 0xFFFF, number & 0xFFFF]
        if data_type == 'int64':
            number = int(value)
            if not -9223372036854775808 <= number <= 9223372036854775807:
                raise ValueError('Int64 value must be between -9223372036854775808 and 9223372036854775807.')
            number &= 0xFFFFFFFFFFFFFFFF
            return [(number >> 48) & 0xFFFF, (number >> 32) & 0xFFFF, (number >> 16) & 0xFFFF, number & 0xFFFF]
        if data_type == 'float32':
            return list(struct.unpack('>HH', struct.pack('>f', float(value))))
        if data_type == 'float64':
            return list(struct.unpack('>HHHH', struct.pack('>d', float(value))))
        if data_type == 'string':
            encoded = value.encode('ascii', errors='replace')
            if len(encoded) % 2:
                encoded += b'\x00'
            return [struct.unpack('>H', encoded[i:i+2])[0] for i in range(0, len(encoded), 2)]
        number = int(value)
        if not 0 <= number <= 65535:
            raise ValueError('UInt16 value must be between 0 and 65535.')
        return [number]

    def connect_and_read(
        self,
        ip,
        tag_name,
        port=None,
        unit=1,
        count=1,
        function='holding',
        operation='read',
        data_type='uint16',
        write_value='',
        string_length=10,
    ):
        state = ViewState(tag_name=tag_name)
        host, display = self._parse_host(ip)
        if not host:
            state.message = 'PLC address must follow this format: 10.191.175.15/1'
            return state, display

        state.connection_path = display
        effective_port = int(port) if port else 502

        try:
            client = ModbusTcpClient(host, port=effective_port)
            if not client.connect():
                state.message = f'Unable to connect to Modbus PLC at {display} (port {effective_port}) — verify the PLC is powered on, reachable, and Modbus TCP is enabled.'
                return state, display

            tag = (tag_name or '').strip()
            if not tag:
                state.message = f'Connected to Modbus PLC at {display} (no tag requested)'
                client.close()
                return state, display

            read_type = 'holding'
            address = None
            if ':' in tag:
                typ, addr = tag.split(':', 1)
                read_type = typ.lower()
                address = addr.strip()
            else:
                address = tag

            try:
                addr = int(address)
            except Exception:
                state.message = 'For pymodbus reads, use a numeric register address or prefix (e.g. holding:40001 or coil:10)'
                client.close()
                return state, display

            func = (read_type if ':' in tag else function or read_type or 'holding').lower()
            addr, address_label = self._to_modbus_offset(func, addr)
            operation = (operation or 'read').lower()
            data_type = (data_type or 'uint16').lower()
            count = self._register_count(data_type, string_length) if func in ('holding', 'reg', 'register', 'input', 'input_register', 'input_registers') else 1
            try:
                if operation == 'write':
                    if func == 'coil':
                        value = self._parse_bool(write_value)
                        result = client.write_coil(addr, value, device_id=int(unit))
                        if self._is_error_response(result):
                            error_msg = getattr(result, 'exception', '') or getattr(result, 'message', '')
                            state.message = f'Write failed for coil {address_label}: {error_msg}' if error_msg else f'Write failed for coil {address_label}'
                        else:
                            state.tag_value = value
                            state.tag_status = f'Coil {address_label} written at offset {addr}'
                            state.message = f'Connected to Modbus PLC at {display}'
                    elif func in ('holding', 'reg', 'register'):
                        registers = self._encode_registers(write_value, data_type)
                        if len(registers) == 1:
                            result = client.write_register(addr, registers[0], device_id=int(unit))
                        else:
                            result = client.write_registers(addr, registers, device_id=int(unit))
                        if self._is_error_response(result):
                            error_msg = getattr(result, 'exception', '') or getattr(result, 'message', '')
                            state.message = f'Write failed for holding register {address_label}: {error_msg}' if error_msg else f'Write failed for holding register {address_label}'
                        else:
                            state.tag_value = write_value
                            state.tag_status = f'{data_type.upper()} value written to {address_label} at offset {addr}'
                            state.message = f'Connected to Modbus PLC at {display}'
                    else:
                        state.message = 'Writes are supported only for holding registers and coils.'
                elif func in ('holding', 'reg', 'register'):
                    try:
                        result = client.read_holding_registers(addr, count=int(count), device_id=int(unit))
                        if self._is_error_response(result):
                            error_msg = getattr(result, 'exception', '') or getattr(result, 'message', '')
                            state.message = f'Read failed for holding register {address_label}: {error_msg}' if error_msg else f'Read failed for holding register {address_label}'
                        else:
                            regs = getattr(result, 'registers', []) or []
                            state.tag_value = self._decode_registers(regs, data_type)
                            state.tag_status = f'Holding register {address_label} read as {data_type.upper()} at offset {addr}'
                            state.message = f'Connected to Modbus PLC at {display}'
                    except Exception as exc:
                        state.message = f'Read error for holding register {address_label}: {exc}'
                elif func == 'coil':
                    try:
                        result = client.read_coils(addr, count=int(count), device_id=int(unit))
                        if self._is_error_response(result):
                            error_msg = getattr(result, 'exception', '') or getattr(result, 'message', '')
                            state.message = f'Read failed for coil {address_label}: {error_msg}' if error_msg else f'Read failed for coil {address_label}'
                        else:
                            bits = getattr(result, 'bits', []) or []
                            state.tag_value = bits[0] if bits else None
                            state.tag_status = f'Coil {address_label} read at offset {addr}'
                            state.message = f'Connected to Modbus PLC at {display}'
                    except Exception as exc:
                        state.message = f'Read error for coil {address_label}: {exc}'
                elif func in ('input', 'input_register', 'input_registers'):
                    try:
                        result = client.read_input_registers(addr, count=int(count), device_id=int(unit))
                        if self._is_error_response(result):
                            error_msg = getattr(result, 'exception', '') or getattr(result, 'message', '')
                            state.message = f'Read failed for input register {address_label}: {error_msg}' if error_msg else f'Read failed for input register {address_label}'
                        else:
                            regs = getattr(result, 'registers', []) or []
                            state.tag_value = self._decode_registers(regs, data_type)
                            state.tag_status = f'Input register {address_label} read as {data_type.upper()} at offset {addr}'
                            state.message = f'Connected to Modbus PLC at {display}'
                    except Exception as exc:
                        state.message = f'Read error for input register {address_label}: {exc}'
                elif func in ('discrete_input', 'discrete', 'input_discrete'):
                    try:
                        result = client.read_discrete_inputs(addr, count=int(count), device_id=int(unit))
                        if self._is_error_response(result):
                            error_msg = getattr(result, 'exception', '') or getattr(result, 'message', '')
                            state.message = f'Read failed for discrete input {address_label}: {error_msg}' if error_msg else f'Read failed for discrete input {address_label}'
                        else:
                            bits = getattr(result, 'bits', []) or []
                            state.tag_value = bits[0] if bits else None
                            state.tag_status = f'Discrete input {address_label} read at offset {addr}'
                            state.message = f'Connected to Modbus PLC at {display}'
                    except Exception as exc:
                        state.message = f'Read error for discrete input {address_label}: {exc}'
                else:
                    state.message = f'Unsupported Modbus read type: {func}. Use holding, coil, input, or discrete_input.'
            except Exception as exc:
                state.message = f'Modbus read/write error on {display}: {exc}'

            client.close()
        except ConnectionError as exc:
            state.message = f'Modbus connection to {display} (port {effective_port}) refused — check network and PLC settings. ({exc})'
        except Exception as exc:
            state.message = f'Modbus error on {display}: {exc}'

        return state, display


class Snap7Service:
    DB_PATTERN = re.compile(r'^DB(\d+)\.DB([XBWDL])(\d+)(?:\.(\d))?$', re.IGNORECASE)
    DB_REAL_PATTERN = re.compile(r'^DB(\d+)\.(?:REAL|DBR)(\d+)$', re.IGNORECASE)
    AREA_PATTERN = re.compile(r'^([MQIE])([XBWDL])(\d+)(?:\.(\d))?$', re.IGNORECASE)
    AREA_REAL_PATTERN = re.compile(r'^([MQIE])REAL(\d+)$', re.IGNORECASE)
    COUNTER_PATTERN = re.compile(r'^C(\d+)$', re.IGNORECASE)
    TIMER_PATTERN = re.compile(r'^T(\d+)$', re.IGNORECASE)

    SIZE_MAP = {'X': 1, 'B': 1, 'W': 2, 'D': 4, 'L': 8}

    @staticmethod
    def _parse_host(ip_input):
        ip_input = (ip_input or '').strip()
        if not ip_input:
            return '', ''
        host = ip_input.strip()
        return host, host

    def _parse_address(self, address):
        address = (address or '').strip().upper()
        if not address:
            return None
        m = self.DB_REAL_PATTERN.match(address)
        if m:
            return {'area': 'DB', 'db_number': int(m.group(1)), 'data_type': 'REAL', 'offset': int(m.group(2)), 'bit': None, 'size': 4}
        m = self.DB_PATTERN.match(address)
        if m:
            db_num = int(m.group(1))
            type_char = m.group(2).upper()
            offset = int(m.group(3))
            bit = int(m.group(4)) if m.group(4) is not None else None
            return {'area': 'DB', 'db_number': db_num, 'data_type': type_char, 'offset': offset, 'bit': bit, 'size': self.SIZE_MAP.get(type_char, 1)}
        m = self.AREA_REAL_PATTERN.match(address)
        if m:
            return {'area': m.group(1).upper(), 'db_number': 0, 'data_type': 'REAL', 'offset': int(m.group(2)), 'bit': None, 'size': 4}
        m = self.AREA_PATTERN.match(address)
        if m:
            area_char = m.group(1).upper()
            type_char = m.group(2).upper()
            offset = int(m.group(3))
            bit = int(m.group(4)) if m.group(4) is not None else None
            return {'area': area_char, 'db_number': 0, 'data_type': type_char, 'offset': offset, 'bit': bit, 'size': self.SIZE_MAP.get(type_char, 1)}
        m = self.COUNTER_PATTERN.match(address)
        if m:
            return {'area': 'C', 'db_number': 0, 'data_type': 'W', 'offset': int(m.group(1)), 'bit': None, 'size': 2}
        m = self.TIMER_PATTERN.match(address)
        if m:
            return {'area': 'T', 'db_number': 0, 'data_type': 'W', 'offset': int(m.group(1)), 'bit': None, 'size': 2}
        return None

    AREA_READ_MAP = {'M': 'mb_read', 'Q': 'ab_read', 'A': 'ab_read', 'I': 'eb_read', 'E': 'eb_read'}
    AREA_WRITE_MAP = {'M': 'mb_write', 'Q': 'ab_write', 'A': 'ab_write', 'I': 'eb_write', 'E': 'eb_write'}

    def _read_area(self, client, parsed):
        area = parsed['area']
        offset = parsed['offset']
        size = parsed['size']
        db_number = parsed['db_number']
        data_type = parsed['data_type']
        bit = parsed['bit']
        if area == 'DB':
            data = client.db_read(db_number, offset, size)
        elif area == 'C':
            data = client.ct_read(offset)
            return int.from_bytes(bytes(data) if not isinstance(data, (bytes, bytearray)) else data, 'big') if data else 0
        elif area == 'T':
            data = client.tm_read(offset)
            return int.from_bytes(bytes(data) if not isinstance(data, (bytes, bytearray)) else data, 'big') if data else 0
        else:
            method = self.AREA_READ_MAP.get(area)
            if method is None:
                raise ValueError(f'Unknown area: {area}')
            data = getattr(client, method)(offset, size)
        return self._decode_data(data, data_type, bit)

    def _decode_data(self, data, data_type, bit):
        if data_type == 'X':
            byte_val = data[0] if isinstance(data, (bytes, bytearray)) else int(data)
            return bool((byte_val >> (bit or 0)) & 1)
        elif data_type == 'B':
            return data[0] if isinstance(data, (bytes, bytearray)) else int(data)
        elif data_type == 'W':
            return int.from_bytes(data[:2], 'big')
        elif data_type == 'D':
            return int.from_bytes(data[:4], 'big')
        elif data_type == 'L':
            return int.from_bytes(data[:8], 'big')
        elif data_type == 'REAL':
            return struct.unpack('>f', data[:4])[0]
        return int.from_bytes(data, 'big')

    def _write_area(self, client, parsed, value):
        area = parsed['area']
        offset = parsed['offset']
        db_number = parsed['db_number']
        data_type = parsed['data_type']
        bit = parsed['bit']
        if area in ('C', 'T'):
            raise ValueError(f'Writing to {"counters" if area == "C" else "timers"} is not supported via Snap7.')
        write_data = self._encode_value(value, data_type, bit, client, parsed)
        if area == 'DB':
            client.db_write(db_number, offset, write_data)
        else:
            method = self.AREA_WRITE_MAP.get(area)
            if method is None:
                raise ValueError(f'Unknown area: {area}')
            getattr(client, method)(offset, write_data)

    def _encode_value(self, value, data_type, bit, client, parsed):
        value_str = str(value).strip()
        if data_type == 'X':
            area = parsed['area']
            offset = parsed['offset']
            db_number = parsed['db_number']
            if area == 'DB':
                current = client.db_read(db_number, offset, 1)
            else:
                method = self.AREA_READ_MAP.get(area)
                current = getattr(client, method)(offset, 1) if method else bytearray(1)
            byte_val = current[0] if isinstance(current, (bytes, bytearray)) else 0
            bool_val = value_str.lower() in ('1', 'true', 'on', 'yes')
            if bool_val:
                byte_val |= (1 << (bit or 0))
            else:
                byte_val &= ~(1 << (bit or 0))
            return bytearray([byte_val])
        elif data_type == 'B':
            return bytearray([int(value_str) & 0xFF])
        elif data_type == 'W':
            return int(value_str).to_bytes(2, 'big')
        elif data_type == 'D':
            return int(value_str).to_bytes(4, 'big')
        elif data_type == 'L':
            return int(value_str).to_bytes(8, 'big')
        elif data_type == 'REAL':
            return bytearray(struct.pack('>f', float(value_str)))
        else:
            return int(value_str).to_bytes(2, 'big')

    def connect_and_read(self, ip, tag_name, operation='read', write_value=''):
        state = ViewState(tag_name=tag_name)
        if _Snap7Client is None:
            detail = f' ({_SNAP7_IMPORT_ERROR})' if _SNAP7_IMPORT_ERROR else ''
            state.message = f'python-snap7 could not be loaded{detail}.'
            return state, ip
        host, display = self._parse_host(ip)
        if not host:
            state.message = 'Enter a valid Siemens PLC IP address (e.g., 192.168.0.1)'
            return state, ''
        state.connection_path = display
        tag = (tag_name or '').strip()
        try:
            client = _Snap7Client()
            client.connect(host, 0, 0)
            if not client.get_connected():
                state.message = f'Unable to connect to Siemens PLC at {display} — verify IP and that the PLC allows remote access.'
                return state, display
            if not tag:
                state.message = f'Connected to Siemens PLC at {display} (no address requested)'
                client.disconnect()
                return state, display
            parsed = self._parse_address(tag)
            if parsed is None:
                state.message = (f'Invalid Siemens address: "{tag}". Use formats like DB1.DBW0, DB1.DBX0.1, MW0, QX0.0, IW0, C0, T0')
                client.disconnect()
                return state, display
            operation = (operation or 'read').strip().lower()
            if operation == 'write':
                if not write_value.strip():
                    state.message = 'Enter a value to write.'
                    client.disconnect()
                    return state, display
                self._write_area(client, parsed, write_value)
                state.tag_value = write_value
                state.tag_status = f'Value written to {tag}'
                state.message = f'Connected to Siemens PLC at {display}'
            else:
                result = self._read_area(client, parsed)
                state.tag_value = result
                state.tag_status = f'Read {tag} successfully'
                state.message = f'Connected to Siemens PLC at {display}'
            client.disconnect()
        except Exception as exc:
            err = str(exc)
            hint = ''
            if '0x81' in err and '0x04' in err:
                hint = (' Possible causes: (1) PUT/GET communication is disabled — enable it in TIA Portal. (2) DB block has "Optimized block access" enabled — disable it.')
            state.message = f'Siemens PLC error at {display}: {exc}{hint}'
        return state, display


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
                dt = node.get_data_type_as_variant_type()
                if hasattr(dt, 'name'):
                    dt_str = dt.name
                else:
                    dt_str = str(dt)
                dt_str = dt_str.lower()
                if dt_str.startswith('varianttype.'):
                    dt_str = dt_str.split('.', 1)[1]
                mapping = {
                    'uint16': 'uint16', 'int16': 'int16', 'uint32': 'uint32', 'int32': 'int32',
                    'uint64': 'uint64', 'int64': 'int64', 'float': 'float32', 'float32': 'float32',
                    'double': 'float64', 'float64': 'float64', 'boolean': 'bool', 'bool': 'bool', 'string': 'string',
                }
                data_type = mapping.get(dt_str, 'uint16')
            except Exception:
                data_type = 'uint16'

        return {
            'tag_name': segments[-1] if segments else '',
            'node_id': node_id,
            'path': path,
            'namespace': namespace or '-',
            'identifier': identifier or '-',
            'identifier_type': identifier_type or '-',
            'data_type': data_type or 'uint16',
            'node_class': 'Variable',
        }

    def fetch_tags(self, endpoint, max_tags=1000, max_depth=4):
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
                if (depth > 0 and self._node_class_name(node) == 'Variable' and self._is_custom_string_node(node_id, path)):
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

    def connect_and_fetch(self, endpoint_input, max_tags=1000):
        state = ViewState()
        endpoint = self.normalize_endpoint(endpoint_input)
        if not endpoint:
            state.message = 'Enter an OPC UA endpoint in this format: 10.191.175.15:4840 or opc.tcp://10.191.175.15:4840'
            return state, endpoint_input
        state.connection_path = endpoint
        try:
            tags = self.fetch_tags(endpoint, max_tags=max_tags)
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

# Load version from version.txt
def _load_app_version():
    """Load version from version.txt file."""
    version_file = Path(settings.BASE_DIR) / 'version.txt'
    try:
        if version_file.exists():
            return version_file.read_text(encoding='utf-8').strip()
    except Exception as e:
        print(f'Warning: Could not load version: {e}')
    # If file was replaced with a semantic version like "V1.0", keep it as-is.
    return version_file.read_text(encoding='utf-8').strip() if version_file.exists() else 'V1.0'

APP_VERSION = _load_app_version()


def _ensure_app_ready():
    """
    Auto-initialize app directories and database on first run.
    Called on import to ensure portable deployment works seamlessly.
    """
    try:
        # Create all required directories
        required_dirs = [
            Path(settings.BASE_DIR) / 'cache',
            Path(settings.BASE_DIR) / 'cache' / 'yaml',
            Path(settings.BASE_DIR) / 'cache' / 'backups',
            Path(settings.BASE_DIR) / 'logs',
            Path(settings.BASE_DIR) / 'connectapp' / 'migrations',
        ]
        
        for directory in required_dirs:
            directory.mkdir(parents=True, exist_ok=True)
        
        # Ensure migrations __init__.py exists
        migrations_init = Path(settings.BASE_DIR) / 'connectapp' / 'migrations' / '__init__.py'
        if not migrations_init.exists():
            migrations_init.touch()
        
        # Run migrations silently (non-blocking)
        try:
            from django.core.management import call_command
            call_command('migrate', verbosity=0, interactive=False, run_syncdb=True)
        except Exception as migration_error:
            # Migrations may fail on first portable run - that's OK
            print(f'[Init] Migration note: {migration_error}')
        
        # Verify database file exists or will be created
        db_file = Path(settings.BASE_DIR) / 'db.sqlite3'
        if not db_file.exists():
            print(f'[Init] Database will be created automatically')
        
        return True
    except Exception as init_error:
        print(f'[Init] Warning: {init_error}')
        # Non-critical - app will continue
        return False


# NOTE:
# Avoid side-effects at import time in the frozen PyInstaller EXE.
# App initialization is performed by server_entrypoint.py instead.



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
            'plc': {'brand': 'allen_bradley', 'connection_path': '', 'last_tag': '', 'last_value': None, 'last_status': ''},
            'scada': {'endpoint': '', 'last_fetch_status': '', 'discovered_tags': []},
            'bridge': {
                'mode': 'json_async_bridge', 'active': False, 'poll_interval_seconds': 1.0,
                'node_address_map': {}, 'mapped_count': 0, 'synced_count': 0,
                'last_sync_at': None, 'last_sync_result': '', 'last_sync_status_map': {}, 'last_sync_snapshot': {},
            },
            'runtime': {'last_action': 'initialized', 'last_message': 'Cache file created.'},
            'backup': {
                'last_backup': None, 'reason': '',
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
        payload.setdefault('backup', {})
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = self.backup_dir / 'landing_data_backup.json'
        backup_payload = deepcopy(payload)
        backup_payload['backup_snapshot'] = {'reason': reason, 'created_at': self._timestamp(), 'source_file': self.data_path.name}
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
            existing_bridge = payload.get('bridge') or {}
            payload['plc'] = {
                'brand': request.session.get('last_plc_brand', 'allen_bradley'),
                'connection_path': plc_path,
                'port': request.session.get('last_plc_port'),
                'word_swapped': request.session.get('last_plc_word_swapped', 'false'),
                'rack': 0, 'siemens_slot': 0,
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
                'mode': 'json_async_bridge',
                'active': bool(existing_bridge.get('active', False)),
                'poll_interval_seconds': existing_bridge.get('poll_interval_seconds', 1.0),
                'node_address_map': request.session.get(CombinedPageView.BRIDGE_MAP_KEY, {}),
                'data_type_map': request.session.get(CombinedPageView.BRIDGE_DTYPE_KEY, {}),
                'mapped_count': len(request.session.get(CombinedPageView.BRIDGE_MAP_KEY, {})),
                'synced_count': request.session.get('landing_last_synced_count', 0),
                'last_sync_at': request.session.get('landing_last_sync_at'),
                'last_sync_result': request.session.get('landing_last_sync_result', ''),
                'last_sync_status_map': existing_bridge.get('last_sync_status_map', {}),
                'last_sync_snapshot': existing_bridge.get('last_sync_snapshot', {}),
            }
            self.append_task(payload, action, message)
        return self.update(mutate, backup_reason=backup_reason)


class AsyncBridgeService:
    def __init__(self, store, logix_service, pool, opcua_service):
        self.store = store
        self.logix_service = logix_service
        self.pool = pool
        self.opcua_service = opcua_service
        self._thread = None
        self._guard = threading.Lock()
        self._stop_event = threading.Event()
        # Production-grade resilience tracking
        self._consecutive_failures = 0
        self._max_consecutive_failures = 50  # Allow many failures before throttling
        self._last_failure_time = None
        self._last_success_time = None
        self._health_check_interval = 10  # Check health every 10 cycles
        self._cycle_count = 0
        self._plc_connection_errors = {}  # Track errors per PLC address
        self._opcua_connection_errors = {}  # Track errors per endpoint
        self._error_backoff_multiplier = 1  # Start normal speed

    @staticmethod
    def _values_match(left, right):
        if left is None and right is None:
            return True
        if isinstance(left, bool) or isinstance(right, bool):
            return left is right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            left_float = float(left)
            right_float = float(right)
            tolerance = max(1e-6, 1e-6 * max(abs(left_float), abs(right_float)))
            return abs(left_float - right_float) <= tolerance
        if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            return all(AsyncBridgeService._values_match(a, b) for a, b in zip(left, right))
        if isinstance(left, dict) and isinstance(right, dict) and set(left.keys()) == set(right.keys()):
            return all(AsyncBridgeService._values_match(left[key], right[key]) for key in left)
        return left == right

    @staticmethod
    def _parse_modbus_tag(plc_tag):
        plc_tag = str(plc_tag or '').strip()
        func = 'holding'
        address = plc_tag
        if ':' in plc_tag:
            typ, addr = plc_tag.split(':', 1)
            func = typ.lower()
            address = addr.strip()
        return func, int(address)

    @staticmethod
    def _coerce_for_variant(value, variant_type):
        if value is None:
            return None
        if variant_type in (ua.VariantType.SByte, ua.VariantType.Byte, ua.VariantType.Int16, ua.VariantType.UInt16,
                            ua.VariantType.Int32, ua.VariantType.UInt32, ua.VariantType.Int64, ua.VariantType.UInt64):
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
    def _coerce_for_plc_value(value, plc_current_value):
        if value is None:
            return None
        if isinstance(plc_current_value, bool):
            if isinstance(value, str):
                return value.strip().lower() in ('1', 'true', 'yes', 'on')
            return bool(value)
        if isinstance(plc_current_value, int) and not isinstance(plc_current_value, bool):
            return int(value)
        if isinstance(plc_current_value, float):
            return float(value)
        if isinstance(plc_current_value, str):
            return str(value)
        return value

    def ensure_running(self):
        with self._guard:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._thread_main, name='plc-opcua-bridge', daemon=True)
            self._thread.start()

    def stop(self):
        """Gracefully stop the bridge with resource cleanup."""
        with self._guard:
            if not self._stop_event.is_set():
                print('[Bridge] Graceful shutdown initiated...')
                self._stop_event.set()
            if self._thread and self._thread.is_alive():
                print('[Bridge] Waiting for bridge thread to finish...')
                self._thread.join(timeout=5)
                if self._thread.is_alive():
                    print('[Bridge] WARNING: Bridge thread did not stop cleanly (will be terminated)')
            print('[Bridge] Shutdown complete')

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._watch_forever())
        finally:
            loop.close()

    async def _watch_forever(self):
        """Production-grade endless monitoring loop with auto-recovery."""
        import random
        while not self._stop_event.is_set():
            self._cycle_count += 1
            cycle_delay = 1.0
            try:
                # Run sync cycle with comprehensive error handling
                try:
                    await asyncio.to_thread(self.run_cycle, False)
                    # Reset failure counter on success
                    self._consecutive_failures = 0
                    self._error_backoff_multiplier = 1
                    self._last_success_time = timezone.localtime()
                except ConnectionError as e:
                    # Connection errors are recoverable - don't escalate
                    self._consecutive_failures += 1
                    self._last_failure_time = timezone.localtime()
                    print(f'[Bridge] Connection error (attempt {self._consecutive_failures}): {e}')
                    # Exponential backoff with jitter: 1s → 1.5s → 2.25s... capped at 15s
                    self._error_backoff_multiplier = min(1.0 + (self._consecutive_failures * 0.15), 15.0)
                    cycle_delay = 1.0 * self._error_backoff_multiplier + (random.random() * 0.5)
                except TimeoutError as e:
                    # Timeout during read/write - also recoverable
                    self._consecutive_failures += 1
                    self._last_failure_time = timezone.localtime()
                    print(f'[Bridge] Timeout error (attempt {self._consecutive_failures}): {e}')
                    self._error_backoff_multiplier = min(1.0 + (self._consecutive_failures * 0.1), 10.0)
                    cycle_delay = 1.0 * self._error_backoff_multiplier
                except Exception as exc:
                    # Catch-all for unexpected errors - log but don't crash
                    self._consecutive_failures += 1
                    self._last_failure_time = timezone.localtime()
                    error_type = type(exc).__name__
                    print(f'[Bridge] {error_type} (attempt {self._consecutive_failures}): {exc}')
                    # More aggressive backoff for unexpected errors
                    self._error_backoff_multiplier = min(1.0 + (self._consecutive_failures * 0.2), 20.0)
                    cycle_delay = 1.0 * self._error_backoff_multiplier
                
                # Periodic health diagnostics
                if self._cycle_count % self._health_check_interval == 0:
                    await self._perform_health_check()
                
                # Sleep with interruption support
                try:
                    await asyncio.sleep(cycle_delay)
                except asyncio.CancelledError:
                    print('[Bridge] Received cancellation signal, gracefully shutting down...')
                    break
            except Exception as outer_exc:
                # Absolute crash prevention - log and continue
                print(f'[Bridge] CRITICAL ERROR in watch loop (will recover): {outer_exc}')
                try:
                    await asyncio.sleep(2.0)
                except:
                    pass
    
    async def _perform_health_check(self):
        """Periodic health check to detect dead connections and reset if needed."""
        try:
            payload = self.store.read()
            bridge = payload.get('bridge', {})
            if not bridge.get('active'):
                return
            
            # Check if we're stuck in repeated failures
            if self._consecutive_failures > self._max_consecutive_failures:
                print(f'[Bridge Health] Resetting after {self._consecutive_failures} failures')
                # Force a clean reconnection on next cycle
                self._consecutive_failures = int(self._max_consecutive_failures * 0.5)
                self._error_backoff_multiplier = 2.0
                # Clear old error history
                self._plc_connection_errors.clear()
                self._opcua_connection_errors.clear()
        except Exception as e:
            print(f'[Bridge Health] Check failed (non-critical): {e}')

    def run_cycle(self, force_plc_to_scada=False):
        if self._stop_event.is_set():
            return
        return self.store.update(lambda payload: self._sync_payload(payload, force_plc_to_scada=force_plc_to_scada))

    def _sync_payload(self, payload, force_plc_to_scada=False):
        bridge = payload.setdefault('bridge', {})
        bridge.setdefault('mode', 'json_async_bridge')
        bridge.setdefault('active', False)
        bridge.setdefault('poll_interval_seconds', 1.0)
        bridge.setdefault('last_sync_status_map', {})
        bridge.setdefault('last_sync_snapshot', {})
        # Increment when we transition from "not connected" -> "connected" (reconnect detection).
        bridge.setdefault('opcua_connected', False)
        bridge.setdefault('opcua_reconnect_epoch', 0)

        if not bridge.get('active'):
            return

        plc_path = str((payload.get('plc') or {}).get('connection_path', '') or '').strip()
        plc_brand = str((payload.get('plc') or {}).get('brand', 'allen_bradley') or 'allen_bradley').strip().lower()
        is_modbus = plc_brand in {'pymodbus', 'modbus'}
        is_siemens = plc_brand == 'siemens_snap7'
        endpoint_input = str((payload.get('scada') or {}).get('endpoint', '') or '').strip()
        endpoint = _opcua_service.normalize_endpoint(endpoint_input)
        address_map = bridge.get('node_address_map') or {}
        data_type_map = bridge.get('data_type_map') or {}
        pairs = [(node_id, plc_tag) for node_id, plc_tag in address_map.items() if node_id and plc_tag]
        status_map = {node_id: 'Waiting: address is empty.' for node_id, plc_tag in address_map.items() if node_id and not plc_tag}
        snapshot_map = bridge.get('last_sync_snapshot') or {}
        # Treat malformed/empty snapshot as disabled for restore decisions.
        if not isinstance(snapshot_map, dict):
            snapshot_map = {}

        if not endpoint:
            bridge['active'] = False
            bridge['last_sync_result'] = 'Bridge stopped: OPC UA endpoint is missing or invalid.'
            bridge['last_sync_status_map'] = {}
            bridge['last_sync_at'] = timezone.localtime().isoformat()
            return

        internal_path, display_path = self.logix_service.normalize_path(plc_path)
        if is_modbus:
            host, display_path = PymodbusService._parse_host(plc_path)
            if not host:
                bridge['active'] = False
                bridge['last_sync_result'] = 'Bridge stopped: Modbus PLC address is missing or invalid.'
                bridge['last_sync_status_map'] = {}
                bridge['last_sync_at'] = timezone.localtime().isoformat()
                return
            internal_path = host
        elif is_siemens:
            host, display_path = Snap7Service._parse_host(plc_path)
            if not host:
                bridge['active'] = False
                bridge['last_sync_result'] = 'Bridge stopped: Siemens PLC address is missing or invalid.'
                bridge['last_sync_status_map'] = {}
                bridge['last_sync_at'] = timezone.localtime().isoformat()
                return
            internal_path = host
        elif not internal_path:
            bridge['active'] = False
            bridge['last_sync_result'] = 'Bridge stopped: PLC address is missing or invalid.'
            bridge['last_sync_status_map'] = {}
            bridge['last_sync_at'] = timezone.localtime().isoformat()
            return

        if not pairs:
            bridge['active'] = False
            bridge['last_sync_result'] = 'Bridge stopped: no valid PLC-to-NodeID mappings were configured.'
            bridge['last_sync_status_map'] = {}
            bridge['last_sync_at'] = timezone.localtime().isoformat()
            return

        opcua_client = OpcUaClient(endpoint, timeout=4)
        success_count = 0
        aligned_count = 0
        conflict_count = 0
        failed_count = 0
        normalized_pairs = [(node_id, plc_tag, plc_tag, plc_tag) for node_id, plc_tag in pairs]

        try:
            # Attempt connection with timeout and error handling
            was_connected = bool(bridge.get('opcua_connected', False))
            try:
                opcua_client.connect()
            except Exception as e:
                bridge['opcua_connected'] = False
                raise ConnectionError(f'OPC UA connection failed to {endpoint}: {e}') from e

            # Verify connection is responsive
            try:
                opcua_client.get_root_node()
            except Exception as e:
                bridge['opcua_connected'] = False
                raise ConnectionError(f'OPC UA connection is not responsive: {e}') from e

            now_connected = True
            if now_connected and not was_connected:
                bridge['opcua_reconnect_epoch'] = int(bridge.get('opcua_reconnect_epoch', 0)) + 1
            bridge['opcua_connected'] = True

            if is_siemens:
                _snap7_service = globals().get('_snap7_service')
                if _snap7_service is None:
                    _snap7_service = Snap7Service()
                    globals()['_snap7_service'] = _snap7_service
                host, _ = Snap7Service._parse_host(plc_path)
                if not host:
                    raise RuntimeError(f'Invalid Siemens PLC address: {plc_path}')
                if _Snap7Client is None:
                    raise RuntimeError('python-snap7 library could not be loaded.')
                s7_client = _Snap7Client()
                try:
                    s7_client.connect(host, 0, 0)
                    if not s7_client.get_connected():
                        raise ConnectionError(f'Siemens PLC connection failed at {host}')
                except Exception as e:
                    raise ConnectionError(f'Siemens connection error: {e}') from e
                try:
                    for node_id, normalized_tag, original_tag, address_label in normalized_pairs:
                        try:
                            parsed = _snap7_service._parse_address(normalized_tag)
                            if parsed is None:
                                status_map[node_id] = f'Invalid Siemens address: {address_label}'
                                failed_count += 1
                                continue
                            plc_value = _snap7_service._read_area(s7_client, parsed)
                            node = opcua_client.get_node(node_id)
                            variant_type = node.get_data_type_as_variant_type()
                            scada_value = node.get_value()
                            initial_scada_value = scada_value

                            snapshot = snapshot_map.get(node_id, {}) if isinstance(snapshot_map, dict) else {}
                            has_snapshot = isinstance(snapshot, dict) and 'plc_value' in snapshot and 'scada_value' in snapshot

                            # Reconnect-safe restore:
                            # OPC UA nodes may report reconnect-initial/default values (e.g. 0) immediately
                            # after a reconnect. In that case we must NOT poison bridge['last_sync_snapshot']
                            # with those reconnect-default SCADA values.
                            cached_scada_value = snapshot.get('scada_value') if has_snapshot else None
                            scada_is_cached_default = (
                                cached_scada_value is not None
                                and self._values_match(initial_scada_value, cached_scada_value)
                            )

                            # NOTE: we intentionally avoid hardcoding "0.0"/"0" heuristics for PLC.
                            # The only safe decision we make here is to protect the cached SCADA value.
                            # Prevent infinite restore-loop (Snap7 parity with Modbus path)
                            restore_consumed = bool(snapshot.get('opcua_reconnect_restore_consumed', False)) if isinstance(snapshot, dict) else False

                            if (
                                force_plc_to_scada is False
                                and has_snapshot
                                and scada_is_cached_default
                                and not restore_consumed
                            ):
                                # SCADA appears to be the reconnect-initial/default state -> restore SCADA from cache.
                                plc_value = snapshot.get('plc_value')
                                scada_value = cached_scada_value
                                typed_value = self._coerce_for_variant(scada_value, variant_type)
                                try:
                                    node.set_value(typed_value, variant_type)
                                    success_count += 1
                                except Exception:
                                    pass
                                current_epoch = int(bridge.get('opcua_reconnect_epoch', 0))
                                status_map[node_id] = f'Restored SCADA from cache after OPC UA reconnect for {address_label} (epoch {current_epoch}).'
                                snapshot_map[node_id] = {
                                    'plc_tag': original_tag,
                                    'plc_value': plc_value,
                                    'scada_value': scada_value,
                                    'synced_at': timezone.localtime().isoformat(),
                                    'opcua_reconnect_restore_consumed': True,
                                    'opcua_reconnect_restore_epoch': current_epoch,
                                }
                                continue
                            if force_plc_to_scada or not has_snapshot:
                                # If SCADA restarted after being disturbed, it may temporarily contain
                                # initial/default values. When bridge cache has a last snapshot for this node,
                                # restore SCADA from the cached PLC-known value.
                                if not has_snapshot and isinstance(snapshot, dict) and 'plc_value' in snapshot:
                                    try:
                                        cached_plc_value = snapshot.get('plc_value')
                                        if cached_plc_value is not None and not self._values_match(cached_plc_value, scada_value):
                                            typed_value = self._coerce_for_variant(cached_plc_value, variant_type)
                                            node.set_value(typed_value, variant_type)
                                            scada_value = typed_value
                                            success_count += 1
                                            status_map[node_id] = f'Restored from cache: PLC {address_label} -> NodeID.'
                                        else:
                                            aligned_count += 1
                                            status_map[node_id] = f'Value: {plc_value}'
                                        snapshot_map[node_id] = {
                                            'plc_tag': original_tag,
                                            'plc_value': plc_value,
                                            'scada_value': scada_value,
                                            'synced_at': timezone.localtime().isoformat(),
                                        }
                                        continue
                                    except Exception as exc:
                                        status_map[node_id] = f'Snapshot restore failed for {address_label}: {exc}'
                                        failed_count += 1
                                        continue

                                if not self._values_match(plc_value, scada_value):
                                    typed_value = self._coerce_for_variant(plc_value, variant_type)
                                    node.set_value(typed_value, variant_type)
                                    scada_value = typed_value
                                    success_count += 1
                                    status_map[node_id] = f'Connected: copied PLC {address_label} to NodeID.'
                                else:
                                    aligned_count += 1
                                    status_map[node_id] = f'Value: {plc_value}'

                            else:
                                plc_changed = not self._values_match(plc_value, snapshot.get('plc_value'))
                                scada_changed = not self._values_match(scada_value, snapshot.get('scada_value'))
                                if plc_changed and not scada_changed:
                                    typed_value = self._coerce_for_variant(plc_value, variant_type)
                                    node.set_value(typed_value, variant_type)
                                    scada_value = typed_value
                                    success_count += 1
                                    status_map[node_id] = f'PLC changed: updated NodeID from {address_label}.'
                                elif scada_changed and not plc_changed:
                                    _snap7_service._write_area(s7_client, parsed, scada_value)
                                    plc_value = scada_value
                                    success_count += 1
                                    status_map[node_id] = f'SCADA changed: updated PLC tag {address_label}.'
                                elif plc_changed and scada_changed:
                                    if self._values_match(plc_value, scada_value):
                                        aligned_count += 1
                                        status_map[node_id] = f'Both sides changed but values match.'
                                    else:
                                        _snap7_service._write_area(s7_client, parsed, scada_value)
                                        plc_value = scada_value
                                        success_count += 1
                                        status_map[node_id] = f'Conflict resolved: SCADA written to PLC {address_label}.'
                                else:
                                    aligned_count += 1
                                    status_map[node_id] = f'Value: {plc_value}'
                            snapshot_map[node_id] = {'plc_tag': original_tag, 'plc_value': plc_value, 'scada_value': scada_value, 'synced_at': timezone.localtime().isoformat()}
                        except Exception as exc:
                            status_map[node_id] = f'Sync failed: {exc}'
                            failed_count += 1
                finally:
                    s7_client.disconnect()
            elif is_modbus:
                host = PymodbusService._parse_host(plc_path)[0]
                if not host:
                    raise RuntimeError(f'Invalid Modbus PLC address: {plc_path}')
                plc_port = int((payload.get('plc') or {}).get('port') or 502)
                modbus_client = ModbusTcpClient(host, port=plc_port, timeout=5)
                try:
                    if not modbus_client.connect():
                        raise ConnectionError(f'Unable to connect to Modbus PLC at {plc_path}:502')
                except Exception as e:
                    raise ConnectionError(f'Modbus connection error: {e}') from e
                try:
                    read_map = {}
                    for node_id, normalized_tag, original_tag, address_label in normalized_pairs:
                        try:
                            func, addr = self._parse_modbus_tag(original_tag)
                            addr, label = PymodbusService._to_modbus_offset(func, addr)
                            tag_dtype = data_type_map.get(node_id, 'uint16')
                            count = PymodbusService._register_count(tag_dtype)

                            if func in ('holding', 'reg', 'register'):
                                result = modbus_client.read_holding_registers(addr, count=count, device_id=1)

                            elif func == 'coil':
                                result = modbus_client.read_coils(addr, count=1, device_id=1)
                            elif func in ('input', 'input_register', 'input_registers'):
                                result = modbus_client.read_input_registers(addr, count=count, device_id=1)
                            elif func in ('discrete_input', 'discrete', 'input_discrete'):
                                result = modbus_client.read_discrete_inputs(addr, count=1, device_id=1)
                            else:
                                result = modbus_client.read_holding_registers(addr, count=count, device_id=1)
                            if PymodbusService._is_error_response(result):
                                read_map[node_id] = None
                            else:
                                regs = getattr(result, 'registers', None)
                                bits = getattr(result, 'bits', None)
                                if regs is not None:
                                    word_swapped = str(payload.get('plc', {}).get('word_swapped', 'false') or 'false').strip().lower() in ('1','true','yes','y','on')
                                    read_map[node_id] = PymodbusService._decode_registers(regs, tag_dtype, word_swapped=word_swapped)

                                elif bits is not None:
                                    read_map[node_id] = bits[0] if bits else None
                                else:
                                    read_map[node_id] = None
                        except Exception:
                            read_map[node_id] = None

                    for node_id, normalized_tag, original_tag, address_label in normalized_pairs:
                        try:
                            plc_value = read_map.get(node_id)
                            if plc_value is None:
                                status_map[node_id] = f'PLC read failed for {address_label}'
                                failed_count += 1
                                continue
                            node = opcua_client.get_node(node_id)
                            variant_type = node.get_data_type_as_variant_type()
                            scada_value = node.get_value()
                            snapshot = snapshot_map.get(node_id, {}) if isinstance(snapshot_map, dict) else {}
                            has_snapshot = isinstance(snapshot, dict) and 'plc_value' in snapshot and 'scada_value' in snapshot
                            # Reconnect-safe restore for Modbus:
                            # If OPC UA just reconnected and reports cached-default SCADA values,
                            # do not overwrite snapshot with PLC values.
                            cached_scada_value = snapshot.get('scada_value') if has_snapshot else None
                            scada_is_cached_default = (
                                has_snapshot
                                and cached_scada_value is not None
                                and self._values_match(scada_value, cached_scada_value)
                            )

                            # Reconnect-safe restore: only restore once per OPC UA reconnect epoch.
                            current_epoch = int(bridge.get('opcua_reconnect_epoch', 0))
                            snapshot_epoch = None
                            if isinstance(snapshot, dict):
                                snapshot_epoch = snapshot.get('opcua_reconnect_restore_epoch')
                            restore_already_for_epoch = (snapshot_epoch is not None and int(snapshot_epoch) == current_epoch)

                            if (
                                has_snapshot
                                and (force_plc_to_scada is False)
                                and scada_is_cached_default
                                and (not restore_already_for_epoch)
                            ):

                                # Debug log (one line per decision) for reconnect restore logic
                                try:
                                    _log_path = Path(settings.BASE_DIR) / 'logs' / 'bridge_debug.jsonl'
                                    _log_path.parent.mkdir(parents=True, exist_ok=True)
                                    with _log_path.open('a', encoding='utf-8') as _f:
                                        _f.write(json.dumps({
                                            'at': timezone.localtime().isoformat(),
                                            'node_id': node_id,
                                            'address_label': address_label,
                                            'event': 'restore_from_cache_modbus',
                                            'opcua_reconnect_epoch': current_epoch,
                                            'opcua_reconnect_restore_epoch_before': snapshot_epoch,
                                            'scada_is_cached_default': scada_is_cached_default,
                                            'force_plc_to_scada': force_plc_to_scada,
                                            'plc_value_before': plc_value,
                                            'scada_value_before': scada_value,
                                        }, ensure_ascii=False) + '\n')
                                except Exception:
                                    pass

                                # Restore SCADA from cache

                                plc_value = snapshot.get('plc_value')
                                scada_value = cached_scada_value
                                typed_value = self._coerce_for_variant(scada_value, variant_type)
                                try:
                                    node.set_value(typed_value, variant_type)
                                    success_count += 1
                                except Exception:
                                    pass
                                status_map[node_id] = f'Restored SCADA from cache after OPC UA reconnect for {address_label}.'
                                snapshot_map[node_id] = {
                                    'plc_tag': original_tag,
                                    'plc_value': plc_value,
                                    'scada_value': scada_value,
                                    'synced_at': timezone.localtime().isoformat(),
                                    'opcua_reconnect_restore_consumed': True,
                                    'opcua_reconnect_restore_epoch': current_epoch,
                                }
                                continue

                            if force_plc_to_scada or not has_snapshot:
                                if not self._values_match(plc_value, scada_value):
                                    typed_value = self._coerce_for_variant(plc_value, variant_type)
                                    node.set_value(typed_value, variant_type)
                                    scada_value = typed_value
                                    success_count += 1
                                    status_map[node_id] = f'Connected: copied PLC {address_label} to NodeID.'
                                else:
                                    aligned_count += 1
                                    status_map[node_id] = f'Value: {plc_value}'
                            else:
                                plc_changed = not self._values_match(plc_value, snapshot.get('plc_value'))
                                scada_changed = not self._values_match(scada_value, snapshot.get('scada_value'))
                                if plc_changed and not scada_changed:
                                    typed_value = self._coerce_for_variant(plc_value, variant_type)
                                    node.set_value(typed_value, variant_type)
                                    scada_value = typed_value
                                    success_count += 1
                                    status_map[node_id] = f'PLC changed: updated NodeID from {address_label}.'
                                elif scada_changed and not plc_changed:
                                    try:
                                        func, addr = self._parse_modbus_tag(original_tag)
                                        addr, _ = PymodbusService._to_modbus_offset(func, addr)
                                        tag_dtype = data_type_map.get(node_id, 'uint16')
                                        word_swapped = str(payload.get('plc', {}).get('word_swapped', 'false') or 'false').strip().lower() in ('1','true','yes','y','on')
                                        registers = PymodbusService._encode_registers(scada_value, tag_dtype)

                                        # Respect Modbus word swapping for float types during SCADA -> PLC writes.
                                        word_swapped = str(payload.get('plc', {}).get('word_swapped', 'false') or 'false').strip().lower() in ('1','true','yes','y','on')
                                        if word_swapped:
                                            if tag_dtype == 'float32' and len(registers) >= 2:
                                                registers[0], registers[1] = registers[1], registers[0]
                                            elif tag_dtype == 'float64' and len(registers) >= 4:
                                                # float64 is 4 registers: swap pairs (w0,w1,w2,w3 -> w1,w0,w3,w2)
                                                registers[0], registers[1], registers[2], registers[3] = registers[1], registers[0], registers[3], registers[2]

                                        if func == 'coil':
                                            modbus_client.write_coil(addr, bool(scada_value), device_id=1)
                                        elif len(registers) == 1:
                                            modbus_client.write_register(addr, registers[0], device_id=1)
                                        else:
                                            modbus_client.write_registers(addr, registers, device_id=1)
                                        plc_value = scada_value
                                        success_count += 1
                                        status_map[node_id] = f'SCADA changed: updated PLC tag {address_label}.'
                                    except Exception as exc:
                                        status_map[node_id] = f'PLC write failed for {address_label}: {exc}'
                                        failed_count += 1
                                        continue
                                elif plc_changed and scada_changed:
                                    if self._values_match(plc_value, scada_value):
                                        aligned_count += 1
                                        status_map[node_id] = f'Both sides changed but values match.'
                                    else:
                                        try:
                                            func, addr = self._parse_modbus_tag(original_tag)
                                            addr, _ = PymodbusService._to_modbus_offset(func, addr)
                                            tag_dtype = data_type_map.get(node_id, 'uint16')
                                            registers = PymodbusService._encode_registers(scada_value, tag_dtype)

                                            # Respect Modbus word swapping for float types during SCADA -> PLC writes.
                                            word_swapped = str(payload.get('plc', {}).get('word_swapped', 'false') or 'false').strip().lower() in ('1','true','yes','y','on')
                                            if word_swapped:
                                                if tag_dtype == 'float32' and len(registers) >= 2:
                                                    registers[0], registers[1] = registers[1], registers[0]
                                                elif tag_dtype == 'float64' and len(registers) >= 4:
                                                    # float64 is 4 registers: swap pairs (w0,w1,w2,w3 -> w1,w0,w3,w2)
                                                    registers[0], registers[1], registers[2], registers[3] = registers[1], registers[0], registers[3], registers[2]

                                            if func == 'coil':
                                                modbus_client.write_coil(addr, bool(scada_value), device_id=1)
                                            elif len(registers) == 1:
                                                modbus_client.write_register(addr, registers[0], device_id=1)
                                            else:
                                                modbus_client.write_registers(addr, registers, device_id=1)
                                            plc_value = scada_value
                                            success_count += 1
                                            status_map[node_id] = f'Conflict resolved: SCADA value written to PLC {address_label}.'
                                        except Exception as exc:
                                            status_map[node_id] = f'Conflict write failed for {address_label}: {exc}'
                                            failed_count += 1
                                            continue
                                else:
                                    aligned_count += 1
                                    status_map[node_id] = f'Value: {plc_value}'

                            # Preserve reconnect-restore markers so the epoch guard survives subsequent cycles.
                            restore_epoch = snapshot.get('opcua_reconnect_restore_epoch') if isinstance(snapshot, dict) else None
                            restore_consumed = snapshot.get('opcua_reconnect_restore_consumed') if isinstance(snapshot, dict) else None
                            snapshot_map[node_id] = {
                                'plc_tag': original_tag,
                                'plc_value': plc_value,
                                'scada_value': scada_value,
                                'synced_at': timezone.localtime().isoformat(),
                                **({
                                    'opcua_reconnect_restore_epoch': restore_epoch
                                } if restore_epoch is not None else {}),
                                **({
                                    'opcua_reconnect_restore_consumed': restore_consumed
                                } if restore_consumed is not None else {}),
                            }
                        except Exception as exc:
                            status_map[node_id] = f'Sync failed: {exc}'
                            failed_count += 1
                finally:
                    modbus_client.close()
            else:
                plc = self.pool.get(internal_path)
                try:
                    if not plc.connected:
                        raise ConnectionError(f'Unable to connect to PLC at {display_path}')
                except Exception as e:
                    raise ConnectionError(f'PLC connection error: {e}') from e
                plc_tags = [normalized_tag for _, normalized_tag, _, _ in normalized_pairs]
                read_results = plc.read(*plc_tags) if plc_tags else []
                if plc_tags and not isinstance(read_results, list):
                    read_results = [read_results]
                read_map = {tag_name: result for tag_name, result in zip(plc_tags, read_results)}
                for node_id, normalized_tag, original_tag, address_label in normalized_pairs:
                    try:
                        read_result = read_map.get(normalized_tag)
                        plc_value, read_status = self.logix_service.parse_result(read_result)
                        if 'failed' in read_status.lower() or plc_value is None:
                            status_map[node_id] = f'PLC read failed for {address_label}: {read_status}'
                            failed_count += 1
                            continue
                        node = opcua_client.get_node(node_id)
                        variant_type = node.get_data_type_as_variant_type()
                        scada_value = node.get_value()
                        snapshot = snapshot_map.get(node_id, {}) if isinstance(snapshot_map, dict) else {}
                        has_snapshot = isinstance(snapshot, dict) and 'plc_value' in snapshot and 'scada_value' in snapshot
                        if force_plc_to_scada or not has_snapshot:
                            if not self._values_match(plc_value, scada_value):
                                typed_value = self._coerce_for_variant(plc_value, variant_type)
                                node.set_value(typed_value, variant_type)
                                scada_value = typed_value
                                success_count += 1
                                status_map[node_id] = f'Connected: copied PLC {address_label} to NodeID.'
                            else:
                                aligned_count += 1
                                status_map[node_id] = f'Value: {plc_value}'
                        else:
                            plc_changed = not self._values_match(plc_value, snapshot.get('plc_value'))
                            scada_changed = not self._values_match(scada_value, snapshot.get('scada_value'))
                            if plc_changed and not scada_changed:
                                typed_value = self._coerce_for_variant(plc_value, variant_type)
                                node.set_value(typed_value, variant_type)
                                scada_value = typed_value
                                success_count += 1
                                status_map[node_id] = f'PLC changed: updated NodeID from {address_label}.'
                            elif scada_changed and not plc_changed:
                                typed_value = self._coerce_for_plc_value(scada_value, plc_value)
                                write_result = plc.write((normalized_tag, typed_value))
                                _, write_status = self.logix_service.parse_result(write_result)
                                if 'failed' in write_status.lower():
                                    status_map[node_id] = f'PLC write failed for {address_label}: {write_status}'
                                    failed_count += 1
                                    continue
                                plc_value = typed_value
                                success_count += 1
                                status_map[node_id] = f'SCADA changed: updated PLC tag {address_label}.'
                            elif plc_changed and scada_changed:
                                if self._values_match(plc_value, scada_value):
                                    aligned_count += 1
                                    status_map[node_id] = f'Both sides changed but values match.'
                                else:
                                    typed_value = self._coerce_for_plc_value(scada_value, plc_value)
                                    write_result = plc.write((normalized_tag, typed_value))
                                    _, write_status = self.logix_service.parse_result(write_result)
                                    if 'failed' in write_status.lower():
                                        status_map[node_id] = f'Conflict write failed for {address_label}: {write_status}'
                                        failed_count += 1
                                        continue
                                    plc_value = typed_value
                                    success_count += 1
                                    status_map[node_id] = f'Conflict resolved: SCADA value written to PLC {address_label}.'
                            else:
                                aligned_count += 1
                                status_map[node_id] = f'Value: {plc_value}'
                        snapshot_map[node_id] = {'plc_tag': original_tag, 'plc_value': plc_value, 'scada_value': scada_value, 'synced_at': timezone.localtime().isoformat()}
                    except Exception as exc:
                        status_map[node_id] = f'Sync failed: {exc}'
                        failed_count += 1

            bridge['synced_count'] = success_count
            bridge['last_sync_at'] = timezone.localtime().isoformat()
            bridge['last_sync_result'] = (f'Bridge cycle: {success_count} updates, {aligned_count} aligned, {conflict_count} conflicts, {failed_count} failures.')
            bridge['last_sync_status_map'] = status_map
            bridge['last_sync_snapshot'] = snapshot_map
            payload['runtime'] = {'last_action': 'bridge_monitor_cycle', 'last_message': bridge['last_sync_result']}
        except Exception as exc:
            error_type = type(exc).__name__
            # Categorize errors for better recovery
            if 'Connection' in error_type or 'Timeout' in error_type or 'refused' in str(exc).lower():
                # Connection errors are transient - mark for retry
                bridge['last_sync_result'] = f'Connection issue (will retry): {error_type}: {exc}'
                raise ConnectionError(str(exc)) from exc
            elif 'Memory' in error_type or 'Resource' in error_type:
                # Resource errors - graceful degradation
                bridge['last_sync_result'] = f'Resource constraint (reducing scope): {error_type}'
                print(f'[Bridge] Resource warning: {exc}')
            else:
                # Unknown errors - log but try to recover
                bridge['last_sync_result'] = f'Sync cycle error: {error_type}: {exc}'
            
            # Always update status to track attempts
            bridge['last_sync_at'] = timezone.localtime().isoformat()
            bridge['last_sync_status_map'] = status_map if status_map else {node_id: f'Cycle error: {error_type}' for node_id, _, _, _ in normalized_pairs}
            payload['runtime'] = {'last_action': 'bridge_monitor_cycle', 'last_message': bridge['last_sync_result']}
            
            # Re-raise connection errors for backoff handling, swallow others
            if 'Connection' in error_type or 'Timeout' in error_type:
                raise
        finally:
            # Comprehensive resource cleanup to prevent leaks
            try:
                if opcua_client:
                    opcua_client.disconnect()
            except Exception as e:
                print(f'[Bridge] OPC UA cleanup warning: {e}')
            
            # Clean up PLC/Modbus/Siemens connections if created locally
            try:
                if is_siemens and 's7_client' in locals() and s7_client:
                    try:
                        s7_client.disconnect()
                    except:
                        pass
            except Exception as e:
                print(f'[Bridge] Siemens cleanup warning: {e}')
            
            try:
                if is_modbus and 'modbus_client' in locals() and modbus_client:
                    try:
                        modbus_client.close()
                    except:
                        pass
            except Exception as e:
                print(f'[Bridge] Modbus cleanup warning: {e}')


# Initialize global instances
_landing_store = LandingDataStore(_LANDING_DATA_FILE, _LANDING_BACKUP_DIR)
_landing_store.read()

_async_bridge = AsyncBridgeService(
    store=_landing_store,
    logix_service=_logix_service,
    pool=_logix_pool,
    opcua_service=_opcua_service,
)

_init_payload = _landing_store.read()
if _init_payload.get('bridge', {}).get('active'):
    _async_bridge.ensure_running()
del _init_payload


@require_GET
def bridge_status_api(request):
    try:
        payload = _landing_store.read()
        bridge = payload.get('bridge', {})
        address_map = bridge.get('node_address_map') or {}
        status_map = dict(bridge.get('last_sync_status_map') or {})
        for node_id, plc_address in address_map.items():
            if node_id and not str(plc_address or '').strip():
                status_map[node_id] = 'Waiting: address is empty.'
        return JsonResponse({
            'active': bool(bridge.get('active', False)),
            'result': bridge.get('last_sync_result', ''),
            'sync_at': bridge.get('last_sync_at', ''),
            'status_map': status_map,
        })
    except Exception as exc:
        return JsonResponse({'active': False, 'error': str(exc)}, status=500)


class CombinedPageView(View):
    template_name = 'index.html'
    BRIDGE_SNAPSHOT_KEY = 'bridge_last_sync_snapshot'
    BRIDGE_STATUS_KEY = 'opcua_sync_status_map'
    BRIDGE_MAP_KEY = 'opcua_plc_address_map'
    BRIDGE_DTYPE_KEY = 'opcua_plc_data_type_map'

    @classmethod
    def _prune_bridge_snapshot(cls, session, address_map):
        snapshot = session.get(cls.BRIDGE_SNAPSHOT_KEY, {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        valid = {node_id: snapshot.get(node_id, {}) for node_id in address_map if node_id in snapshot}
        session[cls.BRIDGE_SNAPSHOT_KEY] = valid
        return valid

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
            raw = raw[len('opc.tcp://'):]
        raw = raw.rstrip('/')
        if ':' not in raw:
            return raw, 4840
        host, port = raw.rsplit(':', 1)
        try:
            return host, int(port)
        except Exception:
            return host, 4840

    @staticmethod
    def _build_common_rows(request):
        rows = []
        opcua_tags = {tag['node_id']: tag for tag in request.session.get('opcua_last_tags', []) if 'node_id' in tag}
        if not opcua_tags:
            return rows
        address_map = request.session.get(CombinedPageView.BRIDGE_MAP_KEY, {})
        status_map = request.session.get(CombinedPageView.BRIDGE_STATUS_KEY, {})
        dtype_map = request.session.get(CombinedPageView.BRIDGE_DTYPE_KEY, {})
        for node_id, plc_address in sorted(address_map.items()):
            plc_address = str(plc_address or '').strip()
            status = 'Waiting: address is empty.' if not plc_address else status_map.get(node_id, '')
            tag_name = opcua_tags.get(node_id, {}).get('tag_name', node_id)
            rows.append({
                'node_id': node_id,
                'tag_name': tag_name,
                'plc_address': plc_address,
                'data_type': dtype_map.get(node_id, 'uint16'),
                'status': status,
            })
        return rows

    @staticmethod
    def _ensure_config_dir():
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        return _CONFIG_DIR

    @classmethod
    def _config_file_choices(cls):
        cfg_dir = cls._ensure_config_dir()
        files = sorted([p.name for p in cfg_dir.iterdir() if p.is_file() and p.suffix.lower() in ('.yaml', '.yml')], key=lambda name: name.lower())
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
        node_address_map = request.session.get(self.BRIDGE_MAP_KEY, {})
        data_type_map = request.session.get(self.BRIDGE_DTYPE_KEY, {})
        return {
            'version': 1,
            'saved_at': timezone.localtime().isoformat(),
            'plc': {
                'brand': request.session.get('last_plc_brand', 'allen_bradley'),
                'ip_address': ip_address, 'slot': slot,
                'tag': request.session.get('last_plc_tag', ''),
                'port': request.session.get('last_plc_port'),
                'word_swapped': request.session.get('last_plc_word_swapped', 'false'),
            },
            'opcua': {'endpoint': request.session.get('last_opcua_endpoint', '')},
            'mappings': {
                'node_address_map': node_address_map,
                'data_type_map': data_type_map,
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
        if brand not in {'allen_bradley', 'pymodbus', 'siemens_snap7'}:
            brand = 'allen_bradley'
        tag = str(plc.get('tag', '') or '').strip()
        opcua_endpoint = str(opcua.get('endpoint', '') or '').strip()
        node_address_map = mappings.get('node_address_map') or {}
        data_type_map = mappings.get('data_type_map') or {}
        if not isinstance(node_address_map, dict):
            raise ValueError('node_address_map must be a key-value object.')
        cleaned_map = {str(k or '').strip(): str(v or '').strip() for k, v in node_address_map.items() if str(k or '').strip()}
        cleaned_dtype_map = {str(k or '').strip(): str(v or '').strip() for k, v in data_type_map.items() if str(k or '').strip()}
        port_raw = plc.get('port')
        port = int(port_raw) if port_raw is not None else None
        word_swapped = str(plc.get('word_swapped', 'false') or 'false').strip().lower()
        if ip_address:
            request.session['last_plc_ip'] = f'{ip_address}/{slot}'
        else:
            request.session['last_plc_ip'] = ''
        request.session['last_plc_brand'] = brand
        request.session['last_plc_tag'] = tag
        if port is not None:
            request.session['last_plc_port'] = port
        request.session['last_plc_word_swapped'] = word_swapped
        request.session['last_opcua_endpoint'] = opcua_endpoint
        request.session[cls.BRIDGE_MAP_KEY] = cleaned_map
        request.session[cls.BRIDGE_DTYPE_KEY] = cleaned_dtype_map
        request.session[cls.BRIDGE_STATUS_KEY] = {}
        cls._prune_bridge_snapshot(request.session, cleaned_map)

    @staticmethod
    def _clear_plc_config_session(request):
        request.session['last_plc_ip'] = ''
        request.session['last_plc_brand'] = 'allen_bradley'
        request.session['last_plc_tag'] = ''
        request.session['last_opcua_endpoint'] = ''
        request.session[CombinedPageView.BRIDGE_MAP_KEY] = {}
        request.session[CombinedPageView.BRIDGE_DTYPE_KEY] = {}
        request.session[CombinedPageView.BRIDGE_STATUS_KEY] = {}
        request.session[CombinedPageView.BRIDGE_SNAPSHOT_KEY] = {}
        request.session.modified = True

    def _build_context(self, request, plc_state, opcua_state, plc_form, opcua_form,
                       plc_config_save_form=None, plc_config_load_form=None, plc_config_import_form=None, show_tag_popup=False):
        plc_state.history = _plc_history.get(request.session)
        opcua_state.history = _opcua_history.get(request.session)
        has_discovered_tags = bool(request.session.get('opcua_last_tags', []))
        has_opcua_tags = bool(request.session.get(self.BRIDGE_MAP_KEY, {})) and has_discovered_tags
        plc_config_save_form = plc_config_save_form or PlcConfigSaveForm()
        plc_config_load_form = plc_config_load_form or PlcConfigLoadForm(file_choices=self._config_file_choices())
        plc_config_import_form = plc_config_import_form or PlcConfigImportForm()
        return {
            'plc_state': plc_state, 'opcua_state': opcua_state,
            'plc_form': plc_form, 'opcua_form': opcua_form,
            'plc_config_save_form': plc_config_save_form,
            'plc_config_load_form': plc_config_load_form,
            'plc_config_import_form': plc_config_import_form,
            'common_rows': self._build_common_rows(request),
            'has_opcua_tags': has_opcua_tags,
            'has_discovered_tags': has_discovered_tags,
            'show_tag_popup': show_tag_popup and has_discovered_tags,
            'bridge_active': self._get_bridge_active(),
            'bridge_result': self._get_bridge_result(),
            'app_version': APP_VERSION,
        }

    @staticmethod
    def _get_bridge_active():
        try:
            payload = _landing_store.read()
            return bool(payload.get('bridge', {}).get('active', False))
        except Exception:
            return False

    @staticmethod
    def _get_bridge_result():
        try:
            payload = _landing_store.read()
            return payload.get('bridge', {}).get('last_sync_result', '')
        except Exception:
            return ''

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
        saved_port = request.session.get('last_plc_port')
        if saved_port is None:
            try:
                saved_port = _landing_store.read().get('plc', {}).get('port')
            except Exception:
                pass
        plc_form = PlcReadForm(initial={
            'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
            'plc_ip_address': ip_address, 'plc_slot': slot,
            'plc_tag': request.session.get('last_plc_tag', ''), 'plc_port': saved_port,
        })
        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
        plc_state = ViewState()
        opcua_state = self._cached_opcua_state(request.session)
        try:
            payload = _landing_store.read()
            plc_data = payload.get('plc') or {}
            scada_data = payload.get('scada') or {}
            bridge = payload.get('bridge', {})

            # Auto-restore PLC settings from cache if session is missing them
            if not request.session.get('last_plc_ip') and plc_data.get('connection_path'):
                request.session['last_plc_ip'] = plc_data.get('connection_path', '')
                request.session['last_plc_brand'] = plc_data.get('brand', 'allen_bradley')
                if plc_data.get('port') is not None:
                    request.session['last_plc_port'] = plc_data['port']
                if plc_data.get('word_swapped'):
                    request.session['last_plc_word_swapped'] = plc_data['word_swapped']

            # Auto-restore OPC UA endpoint and discovered tags from cache
            if not request.session.get('last_opcua_endpoint') and scada_data.get('endpoint'):
                request.session['last_opcua_endpoint'] = scada_data['endpoint']
            if not request.session.get('opcua_last_tags') and scada_data.get('discovered_tags'):
                request.session['opcua_last_tags'] = scada_data['discovered_tags']
                request.session['opcua_last_endpoint'] = scada_data.get('endpoint', '')

            # Auto-restore mapping from bridge config
            if not request.session.get(self.BRIDGE_MAP_KEY) and bridge.get('node_address_map'):
                request.session[self.BRIDGE_MAP_KEY] = bridge.get('node_address_map', {})
                request.session[self.BRIDGE_DTYPE_KEY] = bridge.get('data_type_map', {})

            if bridge.get('active', False):
                address_map = bridge.get('node_address_map', {})
                status_map = bridge.get('last_sync_status_map', {})
                snapshot_map = bridge.get('last_sync_snapshot', {})
                request.session[self.BRIDGE_MAP_KEY] = address_map
                request.session[self.BRIDGE_DTYPE_KEY] = bridge.get('data_type_map', {})
                request.session[self.BRIDGE_STATUS_KEY] = status_map
                request.session[self.BRIDGE_SNAPSHOT_KEY] = snapshot_map
                request.session['landing_last_synced_count'] = bridge.get('synced_count', 0)
                request.session['landing_last_sync_at'] = bridge.get('last_sync_at')
                request.session['landing_last_sync_result'] = bridge.get('last_sync_result', '')
                global _async_bridge
                if _async_bridge:
                    _async_bridge.ensure_running()
        except Exception:
            pass

        # Rebuild form initial values after restoring from cache
        last_plc_ip = request.session.get('last_plc_ip', '')
        ip_address, slot = self._split_plc_path(last_plc_ip)
        saved_port = request.session.get('last_plc_port')
        if saved_port is None:
            try:
                saved_port = _landing_store.read().get('plc', {}).get('port')
            except Exception:
                pass
        plc_form = PlcReadForm(initial={
            'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
            'plc_ip_address': ip_address, 'plc_slot': slot,
            'plc_tag': request.session.get('last_plc_tag', ''),
            'plc_port': saved_port,
            'word_swapped': request.session.get('last_plc_word_swapped', 'false'),
        })
        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
        opcua_state = self._cached_opcua_state(request.session)
        return render(request, self.template_name, self._build_context(request, plc_state, opcua_state, plc_form, opcua_form))

    def post(self, request):
        global _async_bridge
        action = request.POST.get('action', '').strip().lower()
        show_tag_popup = False
        plc_state = ViewState()
        opcua_state = self._cached_opcua_state(request.session)
        last_plc_ip = request.session.get('last_plc_ip', '')
        ip_address, slot = self._split_plc_path(last_plc_ip)
        plc_form = PlcReadForm(initial={
            'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
            'plc_ip_address': ip_address, 'plc_slot': slot,
            'plc_tag': request.session.get('last_plc_tag', ''), 'plc_port': request.session.get('last_plc_port'),
        })
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
                if plc_brand == 'pymodbus':
                    _pymodbus_service = globals().get('_pymodbus_service') or PymodbusService()
                    globals()['_pymodbus_service'] = _pymodbus_service
                    port = plc_form.cleaned_data.get('plc_port')
                    function = plc_form.cleaned_data.get('modbus_function', 'holding')
                    operation = plc_form.cleaned_data.get('modbus_operation', 'read')
                    data_type = plc_form.cleaned_data.get('modbus_data_type', 'uint16')
                    write_value = plc_form.cleaned_data.get('modbus_write_value', '')
                    plc_state, display_path = _pymodbus_service.connect_and_read(plc_ip, plc_tag, port=port, unit=1, count=1, function=function, operation=operation, data_type=data_type, write_value=write_value)
                elif plc_brand == 'siemens_snap7':
                    _snap7_service = globals().get('_snap7_service') or Snap7Service()
                    globals()['_snap7_service'] = _snap7_service
                    operation = plc_form.cleaned_data.get('siemens_operation', 'read')
                    write_value = plc_form.cleaned_data.get('siemens_write_value', '')
                    ip_address = plc_form.cleaned_data.get('plc_ip_address', '').strip()
                    plc_state, display_path = _snap7_service.connect_and_read(ip_address, plc_tag, operation=operation, write_value=write_value)
                else:
                    plc_state, display_path = _logix_service.connect_and_read(plc_ip, plc_tag)
                request.session['last_plc_ip'] = plc_ip
                request.session['last_plc_brand'] = plc_brand
                request.session['last_plc_tag'] = plc_tag
                request.session['last_plc_word_swapped'] = plc_form.cleaned_data.get('word_swapped', 'false')
                if plc_brand == 'pymodbus':
                    request.session['last_plc_port'] = port
                elif plc_brand == 'siemens_snap7':
                    request.session['last_plc_ip'] = ip_address
                _plc_history.add(request.session, {'path': display_path, 'tag': plc_tag, 'message': plc_state.message, 'tag_status': plc_state.tag_status, 'when': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')})
                if plc_state.message.startswith(('Connected to PLC', 'Connected to Modbus PLC', 'Connected to Siemens PLC')):
                    messages.success(request, plc_state.message)
                else:
                    messages.error(request, plc_state.message)
                request.session['landing_last_plc_value'] = plc_state.tag_value
                request.session['landing_last_plc_status'] = plc_state.tag_status
                _landing_store.sync_from_session(request, action='plc_read', message=plc_state.message)
            else:
                messages.error(request, 'Please correct the PLC input errors and try again.')

        elif action == 'opcua_fetch':
            opcua_form = OpcUaFetchForm(request.POST)
            if opcua_form.is_valid():
                endpoint_input = opcua_form.cleaned_data['opcua_endpoint']
                max_tags = opcua_form.cleaned_data.get('max_tags', 1000)
                opcua_state, raw_endpoint = _opcua_service.connect_and_fetch(endpoint_input, max_tags=max_tags)
                request.session['last_opcua_endpoint'] = endpoint_input
                if opcua_state.tag_name == 'Discovered tags' and isinstance(opcua_state.tag_value, list):
                    request.session['opcua_last_tags'] = opcua_state.tag_value
                    request.session['opcua_last_endpoint'] = opcua_state.connection_path or raw_endpoint
                    request.session[self.BRIDGE_STATUS_KEY] = {}
                    show_tag_popup = True
                _opcua_history.add(request.session, {'path': opcua_state.connection_path or raw_endpoint, 'tag': opcua_state.tag_name, 'message': opcua_state.message, 'tag_status': opcua_state.tag_status, 'when': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')})
                if opcua_state.message.startswith('Connected to OPC UA'):
                    messages.success(request, opcua_state.message)
                else:
                    messages.error(request, opcua_state.message)
                request.session['landing_last_opcua_status'] = opcua_state.tag_status
                _landing_store.sync_from_session(request, action='opcua_fetch', message=opcua_state.message)
            else:
                messages.error(request, 'Please correct the OPC UA input errors and try again.')

        elif action == 'add_selected_tags':
            selected_node_ids = request.POST.getlist('selected_node_ids')
            address_map = request.session.get(self.BRIDGE_MAP_KEY, {})
            status_map = request.session.get(self.BRIDGE_STATUS_KEY, {})
            dtype_map = request.session.get(self.BRIDGE_DTYPE_KEY, {})
            opcua_tags = {tag['node_id']: tag for tag in request.session.get('opcua_last_tags', []) if 'node_id' in tag}

            def normalize_opcua_dtype(dt):
                dt = str(dt or '').lower()
                if dt.startswith('varianttype.'):
                    dt = dt.split('.', 1)[1]
                mapping = {
                    'uint16': 'uint16', 'int16': 'int16', 'uint32': 'uint32', 'int32': 'int32',
                    'uint64': 'uint64', 'int64': 'int64', 'float': 'float32', 'float32': 'float32',
                    'double': 'float64', 'float64': 'float64', 'boolean': 'bool', 'bool': 'bool', 'string': 'string',
                }
                return mapping.get(dt, 'uint16')

            added_count = 0
            for node_id in selected_node_ids:
                if node_id not in address_map:
                    address_map[node_id] = ''
                    status_map[node_id] = ''
                    raw_dt = opcua_tags.get(node_id, {}).get('data_type', '')
                    dtype_map[node_id] = normalize_opcua_dtype(raw_dt)
                    added_count += 1
            request.session[self.BRIDGE_MAP_KEY] = address_map
            request.session[self.BRIDGE_STATUS_KEY] = status_map
            request.session[self.BRIDGE_DTYPE_KEY] = dtype_map
            self._prune_bridge_snapshot(request.session, address_map)
            if added_count > 0:
                messages.success(request, f'Added {added_count} tags to mapping.')
            else:
                messages.info(request, 'No new tags added (already in mapping).')
            _landing_store.sync_from_session(request, action='add_selected_tags', message=f'Added {added_count} OPC UA tags to PLC/SCADA mapping.')

        elif action == 'remove_tag':
            node_id = request.POST.get('remove_node_id', '').strip()
            address_map = request.session.get(self.BRIDGE_MAP_KEY, {})
            dtype_map = request.session.get(self.BRIDGE_DTYPE_KEY, {})
            status_map = request.session.get(self.BRIDGE_STATUS_KEY, {})
            removed = node_id and node_id in address_map
            address_map.pop(node_id, None)
            dtype_map.pop(node_id, None)
            status_map.pop(node_id, None)
            self._prune_bridge_snapshot(request.session, address_map)
            request.session[self.BRIDGE_MAP_KEY] = address_map
            request.session[self.BRIDGE_DTYPE_KEY] = dtype_map
            request.session[self.BRIDGE_STATUS_KEY] = status_map
            messages.success(request, 'Removed tag from mapping.')
            _landing_store.sync_from_session(request, action='remove_tag', message=f'Removed tag {node_id} from mapping.' if removed else f'Tag {node_id} not found.')

        elif action == 'remove_selected_tags':
            selected_node_ids = request.POST.getlist('selected_node_ids')
            address_map = request.session.get(self.BRIDGE_MAP_KEY, {})
            dtype_map = request.session.get(self.BRIDGE_DTYPE_KEY, {})
            status_map = request.session.get(self.BRIDGE_STATUS_KEY, {})
            removed_count = 0
            for node_id in selected_node_ids:
                if node_id in address_map:
                    removed_count += 1
                address_map.pop(node_id, None)
                dtype_map.pop(node_id, None)
                status_map.pop(node_id, None)
            self._prune_bridge_snapshot(request.session, address_map)
            request.session[self.BRIDGE_MAP_KEY] = address_map
            request.session[self.BRIDGE_DTYPE_KEY] = dtype_map
            request.session[self.BRIDGE_STATUS_KEY] = status_map
            messages.success(request, f'Removed {removed_count} tag(s) from mapping.')

        elif action == 'connect_bridge':
            endpoint = request.session.get('last_opcua_endpoint', '').strip()
            plc_ip = request.session.get('last_plc_ip', '').strip()
            node_ids = request.POST.getlist('node_ids')
            plc_addresses = request.POST.getlist('plc_addresses')
            data_types = request.POST.getlist('data_types')
            address_map = {}
            dtype_map = {}
            for idx, node_id in enumerate(node_ids):
                node_id = node_id.strip()
                plc_tag = plc_addresses[idx].strip() if idx < len(plc_addresses) else ''
                dt = data_types[idx].strip() if idx < len(data_types) else 'uint16'
                if node_id:
                    address_map[node_id] = plc_tag
                    dtype_map[node_id] = dt
            request.session[self.BRIDGE_MAP_KEY] = address_map
            request.session[self.BRIDGE_DTYPE_KEY] = dtype_map
            snapshot_map = self._prune_bridge_snapshot(request.session, address_map)
            pairs = [(node_id, plc_tag) for node_id, plc_tag in address_map.items() if node_id and plc_tag]
            status_map = {node_id: 'Waiting: address is empty.' for node_id, plc_tag in address_map.items() if not plc_tag}
            if not endpoint:
                messages.error(request, 'Fetch OPC UA tags first before connecting.')
            elif not plc_ip:
                messages.error(request, 'Enter a PLC address first.')
            elif not pairs:
                messages.error(request, 'Map at least one PLC address before connecting.')
            elif OpcUaClient is None:
                messages.error(request, 'OPC UA client library is not installed. Install it with: pip install opcua')
            else:
                plc_brand = request.session.get('last_plc_brand', 'allen_bradley')
                is_modbus = plc_brand in {'pymodbus', 'modbus'}
                is_siemens = plc_brand == 'siemens_snap7'
                if is_modbus:
                    host, display_path = PymodbusService._parse_host(plc_ip)
                    internal_path = host
                elif is_siemens:
                    host, display_path = Snap7Service._parse_host(plc_ip)
                    internal_path = host
                else:
                    internal_path, display_path = _logix_service.normalize_path(plc_ip)
                if not internal_path:
                    messages.error(request, 'PLC address is missing or invalid.')
                else:

                    def enable_bridge(payload):
                        bridge = payload.setdefault('bridge', {})
                        bridge['active'] = True
                        bridge['node_address_map'] = address_map
                        bridge['data_type_map'] = dtype_map
                        bridge['mapped_count'] = len(address_map)
                        bridge['last_sync_status_map'] = status_map
                        # IMPORTANT:
                        # Keep the last cached PLC/scada snapshot across OPC-UA disconnects.
                        # If we overwrite with an empty snapshot during (re)activation,
                        # the bridge will not be able to restore the last PLC-known value
                        # and may briefly push initial/default (0) values.
                        existing_snapshot = bridge.get('last_sync_snapshot') or {}
                        if snapshot_map:
                            bridge['last_sync_snapshot'] = snapshot_map
                        else:
                            bridge['last_sync_snapshot'] = existing_snapshot
                        payload['runtime'] = {
                            'last_action': 'bridge_connected',
                            'last_message': f'Bridge activated for {len(pairs)} mapped address(es).',
                        }

                    _landing_store.update(enable_bridge, backup_reason='bridge_connected')
                    if _async_bridge:
                        _async_bridge.ensure_running()
                        messages.success(
                            request,
                            f'Continuous sync started! Monitoring {len(pairs)} mapped address(es).',
                        )
                    else:
                        messages.error(request, 'Bridge service initialization failed.')
                    request.session[self.BRIDGE_STATUS_KEY] = status_map
                    request.session[self.BRIDGE_SNAPSHOT_KEY] = snapshot_map or {}
                    request.session['landing_last_synced_count'] = 0
                    request.session['landing_last_sync_at'] = timezone.localtime().isoformat()
                    request.session['landing_last_sync_result'] = f'Bridge activated for {len(pairs)} mappings.'


        elif action == 'refresh_bridge':
            if _async_bridge:
                try:
                    _async_bridge.run_cycle(force_plc_to_scada=False)
                except Exception as exc:
                    messages.error(request, f'Refresh failed: {exc}')
            try:
                payload = _landing_store.read()
                bridge = payload.get('bridge', {})
                address_map = bridge.get('node_address_map', {})
                dtype_map = bridge.get('data_type_map', {})
                status_map = bridge.get('last_sync_status_map', {})
                snapshot_map = bridge.get('last_sync_snapshot', {})
                request.session[self.BRIDGE_MAP_KEY] = address_map
                request.session[self.BRIDGE_DTYPE_KEY] = dtype_map
                request.session[self.BRIDGE_STATUS_KEY] = status_map
                request.session[self.BRIDGE_SNAPSHOT_KEY] = snapshot_map
                messages.success(request, f'Bridge refreshed. {bridge.get("last_sync_result", "")}')
            except Exception as exc:
                messages.error(request, f'Failed to read bridge state: {exc}')

        elif action == 'clear_table_addresses':
            request.session[self.BRIDGE_MAP_KEY] = {}
            request.session[self.BRIDGE_DTYPE_KEY] = {}
            request.session[self.BRIDGE_STATUS_KEY] = {}
            request.session[self.BRIDGE_SNAPSHOT_KEY] = {}
            messages.info(request, 'All table address inputs have been cleared.')
            _landing_store.sync_from_session(request, action='clear_table_addresses', message='All PLC address mappings were cleared.')

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
                        _landing_store.sync_from_session(request, action='save_plc_config', message=f'Configuration saved to YAML: {config_path.name}', backup_reason='save_plc_config')
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

                        plc_form = PlcReadForm(initial={
                            'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
                            'plc_ip_address': ip_address,
                            'plc_slot': slot,
                            'plc_tag': request.session.get('last_plc_tag', ''),
                            'plc_port': request.session.get('last_plc_port'),
                            'word_swapped': request.session.get('last_plc_word_swapped', 'false'),
                        })

                        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))
                        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
                        _landing_store.sync_from_session(
                            request,
                            action='load_plc_config',
                            message=f'Configuration loaded from YAML: {config_path.name}',
                        )

                        # Auto-activate bridge if all required settings present
                        opcua_endpoint = request.session.get('last_opcua_endpoint', '').strip()
                        address_map = request.session.get(self.BRIDGE_MAP_KEY, {})
                        if opcua_endpoint and address_map:
                            def auto_enable_bridge(data):
                                bridge = data.setdefault('bridge', {})
                                bridge['active'] = True
                                bridge['node_address_map'] = address_map
                                bridge['data_type_map'] = request.session.get(self.BRIDGE_DTYPE_KEY, {})
                                bridge['mapped_count'] = len(address_map)
                                bridge['last_sync_status_map'] = {}
                                bridge['last_sync_snapshot'] = {}
                                data['runtime'] = {
                                    'last_action': 'auto_bridge_from_load',
                                    'last_message': f'Bridge auto-activated for {len(address_map)} mapped address(es).',
                                }

                            _landing_store.update(auto_enable_bridge, backup_reason='auto_bridge_from_load')
                            if _async_bridge:
                                _async_bridge.ensure_running()
                                messages.success(request, f'Continuous sync auto-started! Monitoring {len(address_map)} mapped address(es).')
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

                        plc_form = PlcReadForm(initial={
                            'plc_brand': request.session.get('last_plc_brand', 'allen_bradley'),
                            'plc_ip_address': ip_address,
                            'plc_slot': slot,
                            'plc_tag': request.session.get('last_plc_tag', ''),
                            'plc_port': request.session.get('last_plc_port'),
                            'word_swapped': request.session.get('last_plc_word_swapped', 'false'),
                        })

                        opcua_host, opcua_port = self._split_opcua_endpoint(request.session.get('last_opcua_endpoint', ''))

                        opcua_form = OpcUaFetchForm(initial={'opcua_host': opcua_host, 'opcua_port': opcua_port})
                        _landing_store.sync_from_session(request, action='import_plc_config', message=f'Configuration imported from YAML: {config_path.name}', backup_reason='import_plc_config')
                    except UnicodeDecodeError:
                        messages.error(request, 'Import failed: file must be UTF-8 encoded text.')
                    except Exception as exc:
                        messages.error(request, f'Import failed: {exc}')

        elif action == 'clear_plc_config':
            self._clear_plc_config_session(request)
            messages.info(request, 'PLC configuration and address mappings have been cleared from the current session.')
            plc_form = PlcReadForm(initial={'plc_brand': 'allen_bradley', 'plc_ip_address': '', 'plc_slot': 0, 'plc_tag': ''})
            opcua_form = OpcUaFetchForm(initial={'opcua_host': '', 'opcua_port': 4840})
            request.session['landing_last_plc_value'] = None
            request.session['landing_last_plc_status'] = ''
            request.session['landing_last_opcua_status'] = ''
            request.session['landing_last_synced_count'] = 0
            request.session['landing_last_sync_at'] = None
            request.session['landing_last_sync_result'] = ''
            _landing_store.sync_from_session(request, action='clear_plc_config', message='Current PLC/SCADA session values were cleared.')

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

        return render(request, self.template_name, self._build_context(
            request, plc_state, opcua_state, plc_form, opcua_form,
            plc_config_save_form=plc_config_save_form,
            plc_config_load_form=plc_config_load_form,
            plc_config_import_form=plc_config_import_form,
            show_tag_popup=show_tag_popup,
        ))
