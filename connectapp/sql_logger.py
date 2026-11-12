import json
import queue
import re
import threading


_TABLE_NAME_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class SqlServerDataLogger:
    def __init__(self, config=None):
        config = dict(config or {})
        self._config_lock = threading.Lock()
        self.enabled = False
        self.connection_string = ''
        self.table_name = 'PlcDataLog'
        self.batch_size = max(1, int(config.get('batch_size') or 250))
        queue_size = max(self.batch_size, int(config.get('queue_size') or 10000))
        self._queue = queue.Queue(maxsize=queue_size)
        self._thread = None
        self._guard = threading.Lock()
        self._retry_event = threading.Event()
        self._dropped_count = 0
        self._configuration_error = None
        self.configure(config)

    @staticmethod
    def _is_enabled(value):
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)

    @staticmethod
    def _odbc_value(value):
        return '{' + str(value or '').replace('}', '}}') + '}'

    @classmethod
    def build_connection_string(cls, config):
        existing = str(config.get('connection_string') or '').strip()
        if existing:
            return existing
        driver = str(config.get('driver') or 'ODBC Driver 18 for SQL Server').strip().strip('{}')
        server = str(config.get('server') or '').strip()
        database = str(config.get('database') or '').strip()
        authentication = str(config.get('authentication') or 'windows').strip().lower()
        parts = [
            f'DRIVER={cls._odbc_value(driver)}',
            f'SERVER={cls._odbc_value(server)}',
            f'DATABASE={cls._odbc_value(database)}',
        ]
        if authentication == 'sql':
            parts.extend([
                f'UID={cls._odbc_value(config.get("username"))}',
                f'PWD={cls._odbc_value(config.get("password"))}',
            ])
        else:
            parts.append('Trusted_Connection=yes')
        parts.extend([
            f'Encrypt={"yes" if cls._is_enabled(config.get("encrypt", True)) else "no"}',
            f'TrustServerCertificate={"yes" if cls._is_enabled(config.get("trust_server_certificate")) else "no"}',
        ])
        return ';'.join(parts)

    @classmethod
    def validate_config(cls, config):
        if not cls._is_enabled(config.get('enabled')):
            return None
        table_name = str(config.get('table_name') or 'PlcDataLog').strip()
        if not _TABLE_NAME_PATTERN.fullmatch(table_name):
            return 'Table name may contain only letters, numbers, and underscores, and cannot start with a number.'
        if not str(config.get('connection_string') or '').strip():
            if not str(config.get('server') or '').strip():
                return 'SQL Server name or address is required.'
            if not str(config.get('database') or '').strip():
                return 'SQL database name is required.'
            if str(config.get('authentication') or 'windows').strip().lower() == 'sql':
                if not str(config.get('username') or '').strip():
                    return 'SQL username is required for SQL Server authentication.'
                if not str(config.get('password') or ''):
                    return 'SQL password is required for SQL Server authentication.'
        return None

    def configure(self, config):
        config = dict(config or {})
        enabled = self._is_enabled(config.get('enabled'))
        connection_string = self.build_connection_string(config)
        table_name = str(config.get('table_name') or 'PlcDataLog').strip()
        configuration_error = self.validate_config(config)
        with self._config_lock:
            self.enabled = enabled
            self.connection_string = connection_string
            self.table_name = table_name
            self._configuration_error = configuration_error
        return configuration_error

    def enqueue(self, records):
        if not self.enabled or self._configuration_error or not records:
            return
        self._ensure_running()
        for record in records:
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._dropped_count += 1
                if self._dropped_count == 1 or self._dropped_count % 1000 == 0:
                    print(f'[SQL Logger] Queue full; dropped {self._dropped_count} records.')

    def _ensure_running(self):
        with self._guard:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._worker,
                name='sql-server-data-logger',
                daemon=True,
            )
            self._thread.start()

    def _connect(self, connection_string=None):
        try:
            import pyodbc
        except ImportError as exc:
            raise RuntimeError('SQL logging requires the pyodbc package.') from exc
        return pyodbc.connect(connection_string or self.connection_string, timeout=5)

    def test_configuration(self, config):
        error = self.validate_config(config)
        if error:
            raise ValueError(error)
        connection_string = self.build_connection_string(config)
        table_name = str(config.get('table_name') or 'PlcDataLog').strip()
        connection = self._connect(connection_string)
        try:
            cursor = connection.cursor()
            cursor.execute(self._create_table_sql(table_name))
            cursor.execute('SELECT 1')
            connection.commit()
        finally:
            connection.close()

    def _worker(self):
        pending = []
        while True:
            try:
                pending.append(self._queue.get(timeout=1.0))
                while len(pending) < self.batch_size:
                    try:
                        pending.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                self._write_batch(pending)
                pending.clear()
            except queue.Empty:
                if pending:
                    self._write_batch(pending)
                    pending.clear()
            except Exception as exc:
                print(f'[SQL Logger] Write failed; retrying while bridge processing continues: {exc}')
                self._retry_event.wait(2.0)

    def _write_batch(self, records):
        with self._config_lock:
            connection_string = self.connection_string
            table_name = self.table_name
        connection = self._connect(connection_string)
        try:
            cursor = connection.cursor()
            cursor.execute(self._create_table_sql(table_name))
            insert_sql = f'''
                INSERT INTO [dbo].[{table_name}]
                    ([RecordedAt], [BridgeSlot], [PlcBrand], [PlcConnection],
                     [TagAddress], [OpcUaNodeId], [Value], [ScadaValue], [DataType])
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''
            rows = [
                (
                    record.get('recorded_at'),
                    record.get('bridge_slot'),
                    record.get('plc_brand'),
                    record.get('plc_connection'),
                    record.get('tag_address'),
                    record.get('opcua_node_id'),
                    self._serialize_value(record.get('value')),
                    self._serialize_value(record.get('scada_value')),
                    record.get('data_type'),
                )
                for record in records
            ]
            cursor.fast_executemany = True
            cursor.executemany(insert_sql, rows)
            connection.commit()
        finally:
            connection.close()

    def _create_table_sql(self, table_name=None):
        table_name = table_name or self.table_name
        return f'''
            IF OBJECT_ID(N'[dbo].[{table_name}]', N'U') IS NULL
            BEGIN
                CREATE TABLE [dbo].[{table_name}] (
                    [Id] BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    [RecordedAt] DATETIMEOFFSET(7) NOT NULL,
                    [BridgeSlot] INT NOT NULL,
                    [PlcBrand] NVARCHAR(50) NOT NULL,
                    [PlcConnection] NVARCHAR(255) NULL,
                    [TagAddress] NVARCHAR(500) NOT NULL,
                    [OpcUaNodeId] NVARCHAR(500) NULL,
                    [Value] NVARCHAR(MAX) NULL,
                    [ScadaValue] NVARCHAR(MAX) NULL,
                    [DataType] NVARCHAR(50) NULL
                );
                CREATE INDEX [IX_{table_name}_RecordedAt]
                    ON [dbo].[{table_name}] ([RecordedAt]);
            END
        '''

    @staticmethod
    def _serialize_value(value):
        if value is None:
            return None
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, default=str, separators=(',', ':'))
        return str(value)