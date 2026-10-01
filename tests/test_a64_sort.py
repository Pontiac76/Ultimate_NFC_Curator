#!/usr/bin/env python3
import sqlite3
import unittest

from u2_curate_tui import A64_INBOX_ORDER_BY


class A64SortTests(unittest.TestCase):
    def test_converted_image_sorts_next_to_source(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE rows(
                title TEXT,
                result_title TEXT DEFAULT '',
                a64_name TEXT DEFAULT '',
                file_type TEXT,
                a64_id TEXT,
                a64_category INTEGER DEFAULT 0,
                entry_index INTEGER,
                entries_count INTEGER DEFAULT 1
            )
        """)
        conn.executemany(
            "INSERT INTO rows(title, file_type, a64_id, entry_index) VALUES (?, ?, ?, ?)",
            [
                ("beach_head", "d64", "249351", 0),
                ("BeachHead3-AF", "d64", "139900", 0),
                ("beach_head - Converted [to D71]", "d71", "242048-local-242048_0_0_beach_head.converted", 0),
                ("Beach Head 2", "d64", "1281", 0),
                ("beach_head", "d64", "242048", 0),
                ("beach_head", "d64", "255249", 0),
            ],
        )
        got = conn.execute(f"SELECT title, file_type, a64_id FROM rows {A64_INBOX_ORDER_BY}").fetchall()
        self.assertEqual(
            got,
            [
                ("Beach Head 2", "d64", "1281"),
                ("beach_head", "d64", "242048"),
                ("beach_head - Converted [to D71]", "d71", "242048-local-242048_0_0_beach_head.converted"),
                ("beach_head", "d64", "249351"),
                ("beach_head", "d64", "255249"),
                ("BeachHead3-AF", "d64", "139900"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
