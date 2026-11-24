import json
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'plc_connect.settings')

import django

django.setup()

from connectapp.views import AsyncBridgeService


class DataExchangeLoggingTests(unittest.TestCase):
    def make_bridge(self, sql_logger):
        bridge = AsyncBridgeService.__new__(AsyncBridgeService)
        bridge.sql_logger = sql_logger
        bridge.bridge_slot = 2
        return bridge

    def test_synced_values_are_logged_even_when_sql_logging_is_unavailable(self):
        bridge = self.make_bridge(None)
        snapshot_map = {
            'ns=2;s=Temperature': {
                'synced_at': '2026-11-24T11:00:00+05:30',
                'plc_tag': 'TankTemperature',
                'plc_value': 21.5,
                'scada_value': 21.5,
            },
            'ns=2;s=Pressure': {
                'synced_at': '2026-11-24T10:59:00+05:30',
                'plc_tag': 'TankPressure',
                'plc_value': 4.2,
                'scada_value': 4.2,
            },
        }

        with patch('builtins.print') as print_mock:
            bridge._log_cycle_values(
                {
                    'plc': {'brand': 'test-plc', 'connection_path': '192.0.2.1'},
                    'bridge': {'data_type_map': {'ns=2;s=Temperature': 'REAL'}},
                },
                snapshot_map,
                {'ns=2;s=Pressure': '2026-11-24T10:59:00+05:30'},
            )

        print_mock.assert_called_once()
        message = print_mock.call_args.args[0]
        self.assertTrue(message.startswith('[Data Exchange] '))
        prefix = '[Data Exchange] '
        logged_record = json.loads(message[len(prefix):])
        self.assertEqual(logged_record['plc_value'], 21.5)
        self.assertEqual(logged_record['scada_value'], 21.5)
        self.assertEqual(logged_record['tag_address'], 'TankTemperature')
        self.assertEqual(logged_record['opcua_node_id'], 'ns=2;s=Temperature')
        self.assertEqual(logged_record['data_type'], 'REAL')
        self.assertEqual(logged_record['bridge_slot'], 2)

    def test_synced_values_are_still_enqueued_for_sql_logging(self):
        sql_logger = Mock()
        bridge = self.make_bridge(sql_logger)
        snapshot_map = {
            'ns=2;s=Temperature': {
                'synced_at': '2026-11-24T11:00:00+05:30',
                'plc_tag': 'TankTemperature',
                'plc_value': 21.5,
                'scada_value': 21.5,
            },
        }

        with patch('builtins.print'):
            bridge._log_cycle_values(
                {'plc': {}, 'bridge': {}},
                snapshot_map,
                {},
            )

        sql_logger.enqueue.assert_called_once()
        self.assertEqual(len(sql_logger.enqueue.call_args.args[0]), 1)


if __name__ == '__main__':
    unittest.main()
