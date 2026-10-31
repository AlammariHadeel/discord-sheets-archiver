import unittest
from datetime import datetime, timezone
from weekly_summary import week_window,split_text,snowflake
class WeeklyTests(unittest.TestCase):
 def test_friday_boundary(self):
  before=week_window(datetime(2026,11,6,17,59,tzinfo=timezone.utc))
  after=week_window(datetime(2026,11,6,18,0,tzinfo=timezone.utc))
  self.assertEqual(before[1].day,30)
  self.assertEqual(after[1].day,6)
  self.assertEqual((after[1]-after[0]).days,7)
 def test_delayed_run(self):
  self.assertEqual(week_window(datetime(2026,11,7,2,tzinfo=timezone.utc))[1].isoformat(),'2026-11-06T18:00:00+00:00')
 def test_parts_fit_discord(self):
  parts=split_text('a'*5000+'\n'+'b'*3000)
  self.assertTrue(all(len(p)<1700 for p in parts))
  self.assertEqual(''.join(parts),'a'*5000+'b'*3000)
 def test_snowflake_order(self):
  self.assertLess(int(snowflake(datetime(2026,10,30,tzinfo=timezone.utc))),int(snowflake(datetime(2026,11,6,tzinfo=timezone.utc))))
