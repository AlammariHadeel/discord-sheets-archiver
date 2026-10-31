"""Read public social post text without account cookies or credentials."""
import re
from urllib.parse import urlsplit
from html.parser import HTMLParser

import requests


class CaptionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'div':
            if self.depth:
                self.depth += 1
            elif 'Caption' in dict(attrs).get('class', '').split():
                self.depth = 1
        if self.depth and tag == 'br':
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag == 'div' and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def read_social(url):
    """Return (source label, text), or None when not public/readable."""
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    headers = {'User-Agent': 'DiscordSheetsArchiver/1.0 (weekly public post summaries)'}
    try:
        if host in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'):
            match = re.search(r'/status/(\d+)(?:/|$)', parsed.path)
            if not match:
                return None
            response = requests.get('https://api.fxtwitter.com/status/' + match[1], headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            tweet = payload.get('tweet') or {}
            if payload.get('code') != 200 or str(tweet.get('id')) != match[1]:
                return None
            text = tweet.get('text', '')
            quote = tweet.get('quote') or {}
            if quote.get('text'):
                text += '\nQuoted post:\n' + quote['text']
            return ('نص منشور X عبر FxEmbed', text[:24000]) if text.strip() else None
        if host in ('instagram.com', 'www.instagram.com'):
            match = re.fullmatch(r'/(p|reel|reels)/([A-Za-z0-9_-]+)/?', parsed.path)
            if not match:
                return None
            response = requests.get(f'https://www.instagram.com/p/{match[2]}/embed/captioned/', headers=headers, timeout=30)
            response.raise_for_status()
            parser = CaptionParser()
            parser.feed(response.text)
            text = ''.join(parser.parts).strip()
            return ('وصف منشور إنستغرام من صفحة التضمين', text[:24000]) if len(text) > 30 else None
    except (requests.RequestException, ValueError, TypeError):
        return None
    return None
