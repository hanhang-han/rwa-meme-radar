import unittest
from fastapi import HTTPException

from app.api.misc import _decode_event_cursor, _encode_event_cursor, _normalize_event


class EventsApiTest(unittest.TestCase):
    def test_event_cursor_is_opaque_and_round_trips(self):
        cursor = _encode_event_cursor({"t": 123, "id": "196:event:1"})
        self.assertFalse(cursor.startswith("{"))
        self.assertEqual(_decode_event_cursor(cursor), {"t": 123, "id": "196:event:1"})

    def test_event_cursor_accepts_previous_json_shape_during_rollout(self):
        self.assertEqual(_decode_event_cursor('{"t":123,"id":"old"}'), {"t": 123, "id": "old"})
        with self.assertRaises(HTTPException) as exc:
            _decode_event_cursor("invalid!")
        self.assertEqual(exc.exception.status_code, 400)

    def test_event_api_removes_storage_scope_from_asset_address(self):
        row = _normalize_event({"id": "196:event", "asset": "196:196:0xAbC", "t": 123}, "196")
        self.assertEqual(row["chainId"], "196")
        self.assertEqual(row["asset"], "0xabc")
        self.assertEqual(row["id"], "196:event")


if __name__ == "__main__":
    unittest.main()
