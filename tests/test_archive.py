import copy
import time
import unittest
from unittest.mock import Mock, patch

from archive import ArchiveError, Discord, Sheet, archive_channel, cells, message_row


def message(number, bot=False):
    return {"id": str(number), "author": {"username": "tester", "bot": bot},
            "timestamp": "2026-10-30T12:00:00+03:00", "content": "hello"}


class MemorySheet:
    def __init__(self):
        self.state = {}
        self.rows = []

    def commit(self, channel, last_id, rows):
        self.rows.extend(rows)
        self.state[channel] = (1, last_id)


class FakeDiscord:
    def __init__(self, messages):
        self.messages = messages
        self.cursors = []

    def get(self, path, params=None):
        if params is None:
            return {"guild_id": "99"}
        self.cursors.append(params["after"])
        page = [m for m in self.messages if int(m["id"]) > int(params["after"])]
        return list(reversed(page[:params["limit"]]))


class ArchiveTests(unittest.TestCase):
    def test_backfill_pagination_and_restart(self):
        discord = FakeDiscord([message(i, i % 10 == 0) for i in range(1, 252)])
        sheet = MemorySheet()
        self.assertEqual(archive_channel(discord, sheet, "1", time.monotonic() + 30), 226)
        self.assertEqual(discord.cursors, ["0", "100", "200", "251"])
        self.assertEqual(sheet.state["1"][1], "251")
        self.assertEqual(archive_channel(discord, sheet, "1", time.monotonic() + 30), 0)
        discord.messages.append(message(252))
        self.assertEqual(archive_channel(discord, sheet, "1", time.monotonic() + 30), 1)
        self.assertEqual(len(sheet.rows), 227)

    def test_bot_only_page_advances_and_channels_are_independent(self):
        sheet = MemorySheet()
        discord = FakeDiscord([message(1, True), message(2, True)])
        archive_channel(discord, sheet, "1", time.monotonic() + 30)
        self.assertEqual(sheet.state["1"][1], "2")
        archive_channel(FakeDiscord([message(1)]), sheet, "2", time.monotonic() + 30)
        self.assertEqual(len(sheet.rows), 1)
        self.assertEqual(sheet.rows[0][1], "2")

    def test_text_urls_attachments_utc_and_formula_safety(self):
        msg = message(42)
        msg["content"] = '=1+1 [رابط](https://example.com/a_(b)) https://example.org/?a=1&b=2'
        msg["attachments"] = [{"url": "https://cdn.example/file", "filename": "صورة.png"}]
        row = message_row(msg, "123456789012345678", "99")
        self.assertEqual(row[0], "2026-10-30T09:00:00Z")
        self.assertEqual(row[4], "https://example.com/a_(b), https://example.org/?a=1&b=2")
        self.assertEqual(row[6], "صورة.png")
        values = cells(0, 1, [row])["updateCells"]["rows"][0]["values"]
        self.assertEqual(values[3]["userEnteredValue"], {"stringValue": msg["content"]})
        self.assertEqual(values[1]["userEnteredValue"]["stringValue"], "123456789012345678")

    @patch("archive.time.sleep")
    def test_discord_rate_limit_then_transient_error(self, sleep):
        discord = Discord("test-token")
        limited = Mock(status_code=429)
        limited.json.return_value = {"retry_after": 2.5}
        success = Mock(status_code=200)
        success.json.return_value = []
        discord.session.get = Mock(side_effect=[limited, Mock(status_code=503), success])
        self.assertEqual(discord.get("/channels/1/messages"), [])
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], 2.5)
        self.assertEqual(discord.session.get.call_count, 3)

    def test_discord_permission_error_is_not_retried_or_leaked(self):
        discord = Discord("test-token")
        discord.session.get = Mock(return_value=Mock(status_code=403, text="secret"))
        with self.assertRaises(ArchiveError) as caught:
            discord.get("/channels/1")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(discord.session.get.call_count, 1)

    @patch("archive.time.sleep")
    def test_lost_response_replays_same_cells_and_state_atomically(self, sleep):
        sheet = object.__new__(Sheet)
        sheet.id = "test"
        sheet.api = Mock()
        sheet.tabs = {"archive": {"sheetId": 1, "gridProperties": {"rowCount": 1}},
                      "state": {"sheetId": 2, "gridProperties": {"rowCount": 1}}}
        sheet.next_row = sheet.next_state = 1
        sheet.state = {}
        stored = {}
        bodies = []

        def apply(**kwargs):
            body = copy.deepcopy(kwargs["body"])
            bodies.append(body)

            def execute():
                for request in body["requests"]:
                    if "updateCells" in request:
                        update = request["updateCells"]
                        start = update["start"]
                        for offset, row in enumerate(update["rows"]):
                            stored[(start["sheetId"], start["rowIndex"] + offset)] = row
                if len(bodies) == 1:
                    raise OSError("response lost after commit")
                return {}
            return Mock(execute=execute)

        sheet.api.batchUpdate.side_effect = apply
        sheet.commit("1", "42", [message_row(message(42), "1", "99")])
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(len(stored), 2)  # One archive row plus one state row.
        self.assertEqual(sheet.next_row, 2)
        self.assertEqual(sheet.state["1"], (1, "42"))
        self.assertEqual(sheet.tabs["archive"]["gridProperties"]["rowCount"], 102)

    def test_failed_write_does_not_advance_local_cursor(self):
        sheet = object.__new__(Sheet)
        sheet.tabs = {"archive": {"sheetId": 1, "gridProperties": {"rowCount": 100}},
                      "state": {"sheetId": 2, "gridProperties": {"rowCount": 100}}}
        sheet.next_row = sheet.next_state = 1
        sheet.state = {}
        sheet.write = Mock(side_effect=ArchiveError("Google API unavailable"))
        with self.assertRaises(ArchiveError):
            sheet.commit("1", "42", [["row"]])
        self.assertEqual(sheet.state, {})
        self.assertEqual(sheet.next_row, 1)


if __name__ == "__main__":
    unittest.main()
