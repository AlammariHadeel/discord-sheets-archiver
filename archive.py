"""Single-writer Discord REST archiver. Credentials are read only from env."""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from http.client import HTTPException

import httplib2
import requests
from google.auth.exceptions import TransportError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HEADERS = ["timestamp", "channel_id", "author_name", "message_text",
           "extracted_urls", "attachment_urls", "attachment_filenames", "message_link"]
STATE_HEADERS = ["channel_id", "last_message_id"]


class ArchiveError(Exception):
    """Safe, fixed diagnostic: never include upstream response bodies."""


def retry(operation):
    for attempt in range(8):
        try:
            return operation()
        except HttpError as exc:
            if exc.resp.status not in (429, 500, 502, 503, 504):
                raise ArchiveError("Google API rejected request; check sharing, API and sheet structure.") from None
        except (OSError, requests.RequestException, HTTPException, httplib2.HttpLib2Error, TransportError):
            pass
        if attempt < 7:
            time.sleep(min(60, 2 ** attempt) + random.random())
    raise ArchiveError("Google API unavailable after retries; next run will resume.")


class Discord:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bot {token}",
                                     "User-Agent": "DiscordSheetsArchiver/1.0"})

    def get(self, path, params=None):
        for attempt in range(8):
            delay = min(60, 2 ** attempt) + random.random()
            try:
                response = self.session.get("https://discord.com/api/v10" + path,
                                            params=params, timeout=45)
                if response.status_code == 429:
                    delay = max(0, float(response.json()["retry_after"])) + 0.1
                elif response.status_code >= 500:
                    pass
                elif response.status_code != 200:
                    raise ArchiveError("Discord rejected request; check token, channel ID and permissions.")
                else:
                    return response.json()
            except requests.RequestException:
                pass
            if attempt < 7:
                time.sleep(delay)
        raise ArchiveError("Discord unavailable after retries; next run will resume.")


def cells(sheet_id, row, values):
    return {"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": row,
                                      "columnIndex": 0},
                            "rows": [{"values": [{"userEnteredValue": {"stringValue": str(v)}}
                                                   for v in line]} for line in values],
                            "fields": "userEnteredValue"}}


def message_row(message, channel, guild):
    content = message.get("content", "")
    urls = re.findall(r'https?://[^\s<>"\x00-\x1f]+', content)
    # Remove Markdown link delimiters while retaining balanced URL parentheses.
    cleaned = []
    for url in urls:
        url = url.rstrip(".,;!؟،")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        cleaned.append(url)
    attachments = message.get("attachments", [])
    timestamp = datetime.fromisoformat(message["timestamp"].replace("Z", "+00:00"))
    return [timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            channel, message["author"]["username"], content, ", ".join(cleaned),
            "\n".join(a["url"] for a in attachments),
            "\n".join(a["filename"] for a in attachments),
            f"https://discord.com/channels/{guild}/{channel}/{message['id']}"]


class Sheet:
    def __init__(self, service, spreadsheet_id):
        self.api = service.spreadsheets()
        self.id = spreadsheet_id
        self.tabs = {}
        # Re-read metadata after uncertain addSheet responses instead of adding twice.
        for title in ("archive", "state"):
            for attempt in range(8):
                metadata = retry(lambda: self.api.get(spreadsheetId=self.id).execute())
                self.tabs = {s["properties"]["title"]: s["properties"] for s in metadata["sheets"]}
                if title in self.tabs:
                    break
                try:
                    self.api.batchUpdate(spreadsheetId=self.id, body={"requests": [
                        {"addSheet": {"properties": {"title": title}}}]}).execute()
                except (HttpError, OSError, HTTPException, httplib2.HttpLib2Error, TransportError):
                    time.sleep(min(60, 2 ** attempt))
            else:
                raise ArchiveError("Could not create archive/state tabs.")
        for title, header in (("archive", HEADERS), ("state", STATE_HEADERS)):
            rows = self.read(title)
            if not rows:
                self.write([cells(self.tabs[title]["sheetId"], 0, [header])])
            elif rows[0] != header:
                raise ArchiveError("Unexpected archive/state headers; use a fresh sheet or restore headers.")
        self.next_row = len(self.read("archive"))
        state = self.read("state")
        self.state = {}
        for index, row in enumerate(state[1:], 1):
            if len(row) != 2 or not all(v.isdigit() for v in row) or row[0] in self.state:
                raise ArchiveError("Invalid state tab; restore it before continuing.")
            self.state[row[0]] = (index, row[1])
        self.next_state = len(state)

    def read(self, title):
        result = retry(lambda: self.api.values().get(spreadsheetId=self.id,
                       range=f"'{title}'!A:H").execute())
        return result.get("values", [])

    def write(self, requests_body):
        # Fixed cell addresses make replay safe even when a response is lost.
        retry(lambda: self.api.batchUpdate(spreadsheetId=self.id,
              body={"requests": requests_body}).execute())

    def commit(self, channel, last_id, rows):
        state_row = self.state.get(channel, (self.next_state, "0"))[0]
        requests_body = []
        for title, end in (("archive", self.next_row + len(rows)), ("state", state_row + 1)):
            tab = self.tabs[title]
            if end > tab["gridProperties"]["rowCount"]:
                requests_body.append({"updateSheetProperties": {"properties": {
                    "sheetId": tab["sheetId"], "gridProperties": {"rowCount": end + 100}},
                    "fields": "gridProperties.rowCount"}})
        if rows:
            requests_body.append(cells(self.tabs["archive"]["sheetId"], self.next_row, rows))
        requests_body.append(cells(self.tabs["state"]["sheetId"], state_row, [[channel, last_id]]))
        self.write(requests_body)  # Rows and cursor succeed or fail together.
        for request in requests_body:
            if "updateSheetProperties" in request:
                props = request["updateSheetProperties"]["properties"]
                for tab in self.tabs.values():
                    if tab["sheetId"] == props["sheetId"]:
                        tab["gridProperties"].update(props["gridProperties"])
        self.next_row += len(rows)
        if channel not in self.state:
            self.next_state += 1
        self.state[channel] = (state_row, last_id)


def archive_channel(discord, sheet, channel, deadline):
    guild = discord.get(f"/channels/{channel}").get("guild_id")
    if not guild:
        raise ArchiveError("Only server text channels/threads are supported.")
    cursor = sheet.state.get(channel, (0, "0"))[1]
    count = 0
    while time.monotonic() < deadline:
        page = discord.get(f"/channels/{channel}/messages", {"after": cursor, "limit": 100})
        page = sorted((m for m in page if int(m["id"]) > int(cursor)), key=lambda m: int(m["id"]))
        if not page:
            break
        rows = [message_row(m, channel, guild) for m in page if not m["author"].get("bot", False)]
        cursor = page[-1]["id"]
        sheet.commit(channel, cursor, rows)
        count += len(rows)
    return count


def main():
    names = ("DISCORD_TOKEN", "CHANNEL_IDS", "GOOGLE_SA_JSON", "SHEET_ID")
    if any(not os.environ.get(name, "").strip() for name in names):
        raise ArchiveError("Missing required environment variables; check the four GitHub Secrets.")
    channels = list(dict.fromkeys(c.strip() for c in os.environ["CHANNEL_IDS"].split(",")))
    if not all(c.isdigit() and int(c) > 0 for c in channels):
        raise ArchiveError("CHANNEL_IDS must contain comma-separated numeric IDs.")
    credentials = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_SA_JSON"]),
                       scopes=["https://www.googleapis.com/auth/spreadsheets"])
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    sheet = Sheet(service, os.environ["SHEET_ID"].strip())
    discord = Discord(os.environ["DISCORD_TOKEN"].strip())
    # Bound each channel fairly; large backfills resume on the next run.
    seconds_per_channel = 600 / len(channels)
    failures = 0
    for channel in channels:
        try:
            count = archive_channel(discord, sheet, channel, time.monotonic() + seconds_per_channel)
            print(f"Channel completed: {count} new human messages.")
        except ArchiveError as exc:
            # A failed Sheets write may have committed. Stop; reload state next run.
            if str(exc).startswith("Google"):
                raise
            print(str(exc), file=sys.stderr)
            failures += 1
    if failures:
        raise ArchiveError("One or more channels failed; check bot access and rerun.")


if __name__ == "__main__":
    try:
        main()
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # SDK exceptions can contain credentials or message data. Do not log them.
        print("Archive failed; check credentials, API access, state and configuration.", file=sys.stderr)
        sys.exit(1)
