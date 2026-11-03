"""Tests for the course colour palette (services/palette.py)."""

import unittest

from services import palette


class PaletteTests(unittest.TestCase):
    def test_slots_pass_through(self):
        self.assertEqual(palette.to_slot(3), "3")
        self.assertEqual(palette.to_slot("12"), "12")
        self.assertIsNone(palette.to_slot(""))
        self.assertIsNone(palette.to_slot(None))

    def test_hex_maps_to_the_nearest_slot(self):
        for slot, hex_color in palette.REFERENCE.items():
            self.assertEqual(palette.to_slot(hex_color), str(slot))
        self.assertEqual(palette.to_slot("#6ba8fe"), "1")  # almost slot 1

    def test_bad_values_are_rejected(self):
        for bad in ("0", "13", "red", "#12", 2.5, True):
            with self.assertRaises(ValueError):
                palette.to_slot(bad)

    def test_next_free_fills_gaps_then_cycles(self):
        self.assertEqual(palette.next_free([]), "1")
        self.assertEqual(palette.next_free(["1", "3", None]), "2")
        self.assertEqual(palette.next_free([str(n) for n in range(1, 13)]), "1")


if __name__ == "__main__":
    unittest.main()
