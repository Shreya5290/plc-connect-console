import logging
from io import StringIO
import unittest
from unittest.mock import Mock, call

from server_entrypoint import _LogStream


class LogStreamTests(unittest.TestCase):
    def test_write_and_flush_send_complete_lines_to_logger(self):
        logger = Mock()
        console = StringIO()
        stream = _LogStream(logger, logging.INFO, console)

        message = "first line\npartial"
        self.assertEqual(stream.write(message), len(message))
        stream.flush()

        self.assertEqual(console.getvalue(), message)
        self.assertEqual(
            logger.log.call_args_list,
            [
                call(logging.INFO, "first line"),
                call(logging.INFO, "partial"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
