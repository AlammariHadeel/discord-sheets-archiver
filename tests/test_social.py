import unittest
from unittest.mock import Mock, patch
from social_content import CaptionParser, read_social


class SocialTests(unittest.TestCase):
    def test_caption_excludes_scripts_and_page_chrome(self):
        p = CaptionParser()
        p.feed('<script>login</script><div class="Caption">Author<br>Text &amp; details<div>nested</div></div><div>Login</div>')
        self.assertEqual(''.join(p.parts), 'Author\nText & detailsnested')

    def test_wrong_tweet_is_rejected(self):
        response = Mock()
        response.json.return_value = {'code': 200, 'tweet': {'id': '456', 'text': 'wrong'}}
        with patch('social_content.requests.get', return_value=response):
            self.assertIsNone(read_social('https://x.com/a/status/123'))

    def test_untrusted_host_is_not_requested(self):
        with patch('social_content.requests.get') as get:
            self.assertIsNone(read_social('https://x.com.evil.test/a/status/123'))
            get.assert_not_called()

    def test_login_page_is_not_content(self):
        response = Mock(text='<html><p>Log in to Instagram</p></html>')
        with patch('social_content.requests.get', return_value=response):
            self.assertIsNone(read_social('https://www.instagram.com/p/abc/'))
